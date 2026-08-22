from flask import Flask, render_template, request, jsonify, session, send_from_directory
from flask_cors import CORS
import mysql.connector
import psycopg2
import psycopg2.extras
import re
import socket
from datetime import datetime, timedelta, date, time
import jwt
import bcrypt
from functools import wraps
import sqlite3
import json
import google.oauth2.id_token
from google.auth.transport import requests as google_requests
import os
import threading
from nl2sql import generate_sql, fallback_sql, load_models, get_encoder, correct_sql_identifiers

# Backend snapshot of the connected database: remembers the actual values
# stored in every table so NL predictions can be corrected to match the real
# data (e.g. 'Dharan' is normalised to the exact spelling stored in the DB).
# Structure: {table: {column: {lowercase_value: real_value}}}.
_data_values = {}

# Row count of every table ({table: row_count}). Used to break ties when two
# tables match an instruction equally (e.g. STUDENT vs student) - the table
# holding more data (rows * columns) wins. Refreshed with _data_values.
_table_sizes = {}


def _quote_ident(name):
    if getattr(db_connector, 'db_type', None) == 'mysql':
        return '`' + name.replace('`', '``') + '`'
    return '"' + name.replace('"', '""') + '"'


def _json_safe(value):
    """Convert a raw database value into a JSON-serializable form.

    MySQL returns ``bytes``/``bytearray`` for BLOB/BIT columns and ``Decimal``,
    ``datetime``, ``date``, ``time`` for typed columns; Flask's ``jsonify``
    cannot handle ``bytes``/``bytearray`` and would 500 the whole response.
    Everything else (numbers, strings, bools, None) is passed through.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.hex() if len(value) else ''
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    try:
        return str(value)
    except Exception:
        return None


# Friendly messages for common MySQL errors. The raw text MySQL returns is
# technical ("1008 (HY000): Can't drop database ..."); users only need to
# understand what went wrong in plain language.
_MYSQL_FRIENDLY_ERRORS = {
    1008: "The database you tried to drop does not exist.",
    1009: "The database could not be dropped.",
    1044: "You don't have permission to access this database.",
    1045: "Access denied. Check your username and password.",
    1046: "No database selected. Please select a database first.",
    1049: "The database does not exist.",
    1050: "A table with that name already exists.",
    1051: "The table does not exist.",
    1054: "That column does not exist in the table.",
    1062: "A record with the same unique value already exists.",
    1064: "The SQL statement is not valid. Please check the syntax.",
    1146: "The table does not exist.",
    1305: "The stored procedure or function does not exist.",
    1364: "A required field is missing a value.",
    1452: "The record references a row that does not exist.",
    2002: "Could not reach the database server.",
    2003: "Could not connect to the database server.",
}


def friendly_error(error):
    """Convert a raw DB error into a clean, human-friendly message.

    MySQL errors arrive like "MySQL Error: 1008 (HY000): Can't drop database
    'x'; database doesn't exist". The connector prefix and the "1008 (HY000):"
    code are stripped, and a friendly message is used for known error codes.
    """
    if not error:
        return error
    if isinstance(error, Exception):
        error = str(error)
    error = str(error)

    m = re.search(r'MySQL Error:\s*(\d+)\s*\(', error)
    if m:
        friendly = _MYSQL_FRIENDLY_ERRORS.get(int(m.group(1)))
        if friendly:
            return friendly

    text = re.sub(r'^(MySQL|PostgreSQL)\s+Error:\s*', '', error)
    text = re.sub(r'^\d+\s*\([^)]*\):\s*', '', text)
    text = re.sub(r'^(Connection|Error|Execution)\s*Error?:\s*', '', text)
    return text


def refresh_data_values():
    """Read distinct values per table/column and remember them in the backend.

    Called on connect and after every data-changing statement so the snapshot
    always reflects the current database. Only string-ish columns are kept
    (they are the ones the model is most likely to get wrong); values are
    capped to keep large tables cheap.
    """
    global _data_values, _table_sizes
    _data_values = {}
    _table_sizes = {}
    if not db_connector or not getattr(db_connector, 'connection', None):
        return _data_values

    schema, err = db_connector.get_schema()
    if err or not schema:
        return _data_values

    skip_types = {
        'int', 'bigint', 'tinyint', 'smallint', 'mediumint', 'float', 'double',
        'real', 'decimal', 'numeric', 'bool', 'boolean', 'bit', 'year', 'date',
        'datetime', 'timestamp', 'time', 'blob', 'longblob', 'mediumblob',
        'tinyblob', 'text', 'mediumtext', 'longtext', 'tinytext', 'json',
        'jsonb', 'bytea', 'money', 'interval', 'uuid',
    }
    for table, cols in schema.items():
        _data_values[table] = {}
        try:
            cursor = db_connector.connection.cursor()
            cursor.execute(f'SELECT COUNT(*) FROM {_quote_ident(table)}')
            count = cursor.fetchone()
            cursor.close()
            _table_sizes[table] = count[0] if count else 0
        except Exception:
            _table_sizes[table] = 0
        for col in cols:
            ctype = (col.get('type') or '').lower().split('(')[0].strip()
            if ctype in skip_types or not ctype:
                continue
            try:
                cursor = db_connector.connection.cursor()
                cursor.execute(
                    f'SELECT DISTINCT {_quote_ident(col["name"])} '
                    f'FROM {_quote_ident(table)} LIMIT 200')
                rows = cursor.fetchall()
                cursor.close()
            except Exception:
                continue
            distinct = {}
            for (value,) in rows:
                if value is None:
                    continue
                key = str(value).lower()
                distinct.setdefault(key, str(value))
            if distinct:
                _data_values[table][col['name']] = distinct
    return _data_values

# Matches inputs that are already SQL (run directly) vs natural-language
SQL_START_RE = re.compile(r'^(\w+)\b', re.IGNORECASE)
SQL_HARD_START = {'select', 'insert', 'update', 'delete', 'drop', 'alter',
                  'create', 'truncate', 'with', 'set', 'grant', 'revoke',
                  'rename', 'commit', 'rollback', 'savepoint', 'release',
                  'explain', 'describe', 'desc'}


def is_sql_query(text):
    stripped = text.strip()
    m = SQL_START_RE.match(stripped)
    if not m:
        return False
    word = m.group(1).lower()
    if word in SQL_HARD_START:
        # "select * from a STUDENT where ..." is natural language: the word
        # between FROM and the table is an English article, not a valid alias.
        if re.search(
            r'\bfrom\s+(a|an|the)\s+'
            r'(?!where\b|from\b|join\b|group\b|order\b|limit\b|having\b|'
            r'on\b|left\b|right\b|inner\b|outer\b|as\b|set\b|values\b)'
            r'[a-zA-Z0-9_]+',
            stripped, re.IGNORECASE):
            return False
        return True
    if word == 'show':
        # SQL only when followed by a schema keyword, otherwise it is likely
        # natural language like "show all students"
        return bool(re.match(
            r'^show\s+(databases|tables|columns|index|indexes|create|'
            r'full\s+tables|variables|status|warnings|grants|processlist|'
            r'table\s+status|engines)\b',
            stripped, re.IGNORECASE))
    if word == 'use':
        return bool(re.match(
            r'^use\s+[`]?[a-zA-Z0-9_]+[`]?\s*;?$', stripped, re.IGNORECASE))
    return False

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'
app.permanent_session_lifetime = timedelta(hours=24)
CORS(app)

# JWT Configuration
JWT_SECRET_KEY = 'your-jwt-secret-key-change-in-production'
JWT_EXPIRATION_HOURS = 24

# Google OAuth Configuration
GOOGLE_CLIENT_ID = '949076025985-v7eju8to2nvb2cimlrlmgbi88khpdei3.apps.googleusercontent.com'

# Initialize user database
def init_user_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT,
            google_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_user_db()

# Helper functions for authentication
def hash_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password, hash):
    return bcrypt.checkpw(password.encode('utf-8'), hash.encode('utf-8'))

def generate_token(user_id, email):
    payload = {
        'user_id': user_id,
        'email': email,
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm='HS256')

def verify_token(token):
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'Token is missing'}), 401
        
        token = token.replace('Bearer ', '')
        payload = verify_token(token)
        if not payload:
            return jsonify({'error': 'Invalid or expired token'}), 401
        
        request.user_id = payload['user_id']
        request.user_email = payload['email']
        return f(*args, **kwargs)
    return decorated

# Database Connector Class
class DatabaseConnector:
    def __init__(self):
        self.connection = None
        self.db_type = None
        self.current_database = None
        self.connection_params = None
    
    def connect(self, db_type, host, username, password, port=None, database=None):
        try:
            self.connection_params = {
                'db_type': db_type,
                'host': host,
                'username': username,
                'password': password,
                'port': port,
                'database': database
            }
            
            if db_type.lower() == 'mysql':
                self.connection = mysql.connector.connect(
                    host=host,
                    user=username,
                    password=password,
                    port=int(port) if port else 3306,
                    database=database if database else None,
                    connect_timeout=10,
                    autocommit=True
                )
                self.db_type = 'mysql'
                self.current_database = database
                
                cursor = self.connection.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute("SET SESSION sql_mode = ''")
                cursor.close()
                
            elif db_type.lower() == 'postgresql':
                self.connection = psycopg2.connect(
                    host=host,
                    user=username,
                    password=password,
                    port=int(port) if port else 5432,
                    database=database if database else 'postgres',
                    connect_timeout=10
                )
                self.db_type = 'postgresql'
                self.current_database = database
                
                cursor = self.connection.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                
            else:
                return False, "Unsupported database type"
            
            return True, "Connected successfully"
        except mysql.connector.Error as e:
            return False, friendly_error(f"MySQL Error: {str(e)}")
        except psycopg2.Error as e:
            return False, friendly_error(f"PostgreSQL Error: {str(e)}")
        except Exception as e:
            return False, friendly_error(f"Connection Error: {str(e)}")
    
    def reconnect(self):
        if not self.connection_params:
            return False, "No connection parameters saved"
        
        params = self.connection_params
        return self.connect(
            params['db_type'],
            params['host'],
            params['username'],
            params['password'],
            params['port'],
            self.current_database if self.current_database else params['database']
        )
    
    def check_connection(self):
        if not self.connection:
            return False
        
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return True
        except:
            return False
    
    def execute_query(self, query):
        if not self.connection:
            return None, "No database connection"
        
        if not self.check_connection():
            success, message = self.reconnect()
            if not success:
                return None, f"Connection lost and reconnection failed: {message}"
        
        try:
            cursor = self.connection.cursor()
            
            if query.strip().upper().startswith('USE'):
                db_name = query.strip().split()[1].strip(';')
                if self.db_type == 'mysql':
                    cursor.execute(f"USE {db_name}")
                    self.current_database = db_name
                    cursor.close()
                    return {"message": f"Switched to database: {db_name}"}, None
                else:
                    self.connection.close()
                    params = self.connection_params
                    self.connection = psycopg2.connect(
                        host=params['host'],
                        user=params['username'],
                        password=params['password'],
                        port=int(params['port']) if params['port'] else 5432,
                        database=db_name
                    )
                    self.current_database = db_name
                    cursor.close()
                    return {"message": f"Switched to database: {db_name}"}, None
            
            cursor.execute(query)
            
            if cursor.description:
                results = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                results = [tuple(_json_safe(v) for v in row) for row in results]
                cursor.close()
                return {"columns": columns, "rows": results, "row_count": len(results)}, None
            else:
                self.connection.commit()
                row_count = cursor.rowcount
                cursor.close()
                
                if row_count >= 0:
                    return {"message": f"Query executed successfully. {row_count} rows affected."}, None
                else:
                    return {"message": "Query executed successfully."}, None
                
        except mysql.connector.Error as e:
            return None, friendly_error(f"MySQL Error: {str(e)}")
        except psycopg2.Error as e:
            return None, friendly_error(f"PostgreSQL Error: {str(e)}")
        except Exception as e:
            return None, friendly_error(f"Error: {str(e)}")
    
    def get_databases(self):
        if not self.connection:
            return None, "No database connection"
        
        if not self.check_connection():
            success, message = self.reconnect()
            if not success:
                return None, message
        
        try:
            cursor = self.connection.cursor()
            
            if self.db_type == 'mysql':
                cursor.execute("SHOW DATABASES")
                results = cursor.fetchall()
                databases = [row[0] for row in results]
            else:
                cursor.execute("""
                    SELECT datname FROM pg_database 
                    WHERE datistemplate = false 
                    ORDER BY datname
                """)
                results = cursor.fetchall()
                databases = [row[0] for row in results]
            
            cursor.close()
            return databases, None
        except Exception as e:
            return None, friendly_error(str(e))
    
    def get_tables(self, database=None):
        if not self.connection:
            return None, "No database connection"
        
        if not self.check_connection():
            success, message = self.reconnect()
            if not success:
                return None, message
        
        try:
            cursor = self.connection.cursor()
            
            if database and database != self.current_database:
                if self.db_type == 'mysql':
                    cursor.execute(f"USE {database}")
                    self.current_database = database
            
            if self.db_type == 'mysql':
                cursor.execute("SHOW TABLES")
                results = cursor.fetchall()
                tables = [row[0] for row in results]
            else:
                cursor.execute("""
                    SELECT table_name FROM information_schema.tables 
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
                results = cursor.fetchall()
                tables = [row[0] for row in results]
            
            cursor.close()
            return tables, None
        except Exception as e:
            return None, friendly_error(str(e))
    
    def get_schema(self, table=None):
        if not self.connection:
            return None, "No database connection"
        
        if not self.check_connection():
            success, message = self.reconnect()
            if not success:
                return None, message
        
        try:
            cursor = self.connection.cursor()
            
            if self.db_type == 'mysql':
                if table:
                    cursor.execute(f"DESCRIBE {table}")
                else:
                    cursor.execute("""
                        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE 
                        FROM INFORMATION_SCHEMA.COLUMNS 
                        WHERE TABLE_SCHEMA = DATABASE()
                        ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """)
            else:
                if table:
                    cursor.execute(f"""
                        SELECT column_name, data_type 
                        FROM information_schema.columns 
                        WHERE table_name = '{table}' 
                        AND table_schema = 'public'
                    """)
                else:
                    cursor.execute("""
                        SELECT table_name, column_name, data_type 
                        FROM information_schema.columns 
                        WHERE table_schema = 'public'
                        ORDER BY table_name, ordinal_position
                    """)
            
            results = cursor.fetchall()
            cursor.close()
            
            if table:
                columns = [{"name": row[0], "type": row[1]} for row in results]
                return columns, None
            else:
                schema = {}
                for row in results:
                    table_name = row[0]
                    if table_name not in schema:
                        schema[table_name] = []
                    schema[table_name].append({"name": row[1], "type": row[2]})
                return schema, None
                
        except Exception as e:
            return None, friendly_error(str(e))
    
    def get_tables_with_data(self):
        """Get all tables with sample data"""
        if not self.connection:
            return None, "No database connection"
        
        tables, error = self.get_tables()
        if error:
            return None, error
        
        table_data = {}
        for table in tables:
            try:
                qtable = _quote_ident(table)
                cursor = self.connection.cursor()
                query = f"SELECT * FROM {qtable} LIMIT 5"
                cursor.execute(query)
                
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = [tuple(_json_safe(v) for v in row) for row in cursor.fetchall()]
                else:
                    columns, rows = [], []
                cursor.close()
                
                row_count = len(rows)
                try:
                    cursor = self.connection.cursor()
                    cursor.execute(f"SELECT COUNT(*) FROM {qtable}")
                    count = cursor.fetchone()
                    if count:
                        row_count = count[0]
                    cursor.close()
                except Exception:
                    pass
                
                table_data[table] = {
                    'columns': columns,
                    'sample_data': rows,
                    'row_count': row_count
                }
            except Exception as e:
                continue
        
        return table_data, None

    def get_table_data(self, table):
        """Return all columns and ALL rows of a single table (for the detail view)."""
        if not self.connection:
            return None, "No database connection"

        if not self.check_connection():
            success, message = self.reconnect()
            if not success:
                return None, message

        try:
            qtable = _quote_ident(table)
            cursor = self.connection.cursor()
            cursor.execute(f"SELECT * FROM {qtable}")
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = [tuple(_json_safe(v) for v in row) for row in cursor.fetchall()]
            cursor.close()
            return {'columns': columns, 'rows': rows, 'row_count': len(rows)}, None
        except mysql.connector.Error as e:
            return None, friendly_error(f"MySQL Error: {str(e)}")
        except psycopg2.Error as e:
            return None, friendly_error(f"PostgreSQL Error: {str(e)}")
        except Exception as e:
            return None, friendly_error(f"Error: {str(e)}")
    
    def close(self):
        if self.connection:
            try:
                self.connection.close()
            except:
                pass
            self.connection = None
            self.current_database = None
            self.connection_params = None

# Global connector instance
db_connector = DatabaseConnector()

# ============================================
# MODEL PRELOAD
# ============================================
# The NL2SQL models are loaded in the background as soon as the server starts,
# so they are ready before the user logs in - the first instruction never has
# to wait for model loading.
_model_load_state = {'loading': False, 'ready': False, 'error': None}
_model_load_lock = threading.Lock()


def _preload_models():
    with _model_load_lock:
        if _model_load_state['ready'] or _model_load_state['loading']:
            return
        _model_load_state['loading'] = True
    try:
        load_models()
        get_encoder()
        with _model_load_lock:
            _model_load_state['ready'] = True
            _model_load_state['error'] = None
    except Exception as e:
        with _model_load_lock:
            _model_load_state['error'] = str(e)
    finally:
        with _model_load_lock:
            _model_load_state['loading'] = False


threading.Thread(target=_preload_models, daemon=True, name='model-preload').start()


@app.route('/model-status', methods=['GET'])
def model_status():
    with _model_load_lock:
        return jsonify({
            'ready': _model_load_state['ready'],
            'loading': _model_load_state['loading'],
            'error': _model_load_state['error']
        })

# ============================================
# GOOGLE CLIENT ID ROUTE - FIXED
# ============================================
@app.route('/google-client-id', methods=['GET'])
def get_google_client_id():
    """Return the Google Client ID for the frontend"""
    return jsonify({
        'client_id': GOOGLE_CLIENT_ID
    })

# ============================================
# AUTHENTICATION ROUTES
# ============================================

@app.route('/signup', methods=['POST'])
def signup():
    data = request.json
    email = data.get('email', '').strip().lower()
    full_name = data.get('full_name', '').strip()
    password = data.get('password', '')
    
    if not email or not full_name or not password:
        return jsonify({'message': 'All fields are required'}), 400
    
    if len(password) < 8:
        return jsonify({'message': 'Password must be at least 8 characters'}), 400
    
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        
        c.execute('SELECT id FROM users WHERE email = ?', (email,))
        if c.fetchone():
            conn.close()
            return jsonify({'message': 'Email already registered'}), 400
        
        password_hash = hash_password(password)
        c.execute('''
            INSERT INTO users (email, full_name, password_hash)
            VALUES (?, ?, ?)
        ''', (email, full_name, password_hash))
        conn.commit()
        conn.close()
        
        return jsonify({'message': 'Account created successfully'}), 201
    
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    
    if not email or not password:
        return jsonify({'message': 'Email and password are required'}), 400
    
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        
        c.execute('SELECT id, email, full_name, password_hash FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        conn.close()
        
        if not user:
            return jsonify({'message': 'Invalid credentials'}), 401
        
        user_id, user_email, full_name, password_hash = user
        
        if not verify_password(password, password_hash):
            return jsonify({'message': 'Invalid credentials'}), 401
        
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        token = generate_token(user_id, user_email)
        
        session.permanent = True
        session['logged_in'] = True
        session['user_id'] = user_id
        session['email'] = user_email
        
        return jsonify({
            'token': token,
            'user': {
                'id': user_id,
                'email': user_email,
                'full_name': full_name
            }
        }), 200
    
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500

@app.route('/auth/google', methods=['POST'])
def auth_google():
    credential = request.json.get('credential')
    if not credential:
        return jsonify({'success': False, 'message': 'Missing credential'}), 400
    
    try:
        request_adapter = google_requests.Request()
        idinfo = google.oauth2.id_token.verify_oauth2_token(
            credential, request_adapter, GOOGLE_CLIENT_ID)
        
        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            return jsonify({'success': False, 'message': 'Invalid token issuer'}), 400
        
        email = idinfo['email'].lower()
        full_name = idinfo.get('name', email.split('@')[0])
        google_id = idinfo['sub']
        
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        
        c.execute('SELECT id, email, full_name FROM users WHERE email = ? OR google_id = ?', (email, google_id))
        user = c.fetchone()
        
        if user:
            user_id, user_email, user_name = user
            c.execute('UPDATE users SET google_id = ?, last_login = CURRENT_TIMESTAMP WHERE id = ?', (google_id, user_id))
            conn.commit()
        else:
            c.execute('''
                INSERT INTO users (email, full_name, google_id, last_login)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (email, full_name, google_id))
            conn.commit()
            user_id = c.lastrowid
            user_email = email
            user_name = full_name
        
        conn.close()
        
        token = generate_token(user_id, user_email)
        
        session.permanent = True
        session['logged_in'] = True
        session['user_id'] = user_id
        session['email'] = user_email
        
        return jsonify({
            'success': True,
            'token': token,
            'user': {
                'id': user_id,
                'email': user_email,
                'full_name': user_name
            }
        }), 200
        
    except ValueError as e:
        return jsonify({'success': False, 'message': f'Invalid token: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/verify-token', methods=['GET'])
def verify_token_route():
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({'valid': False}), 401
    
    token = token.replace('Bearer ', '')
    payload = verify_token(token)
    
    if payload:
        return jsonify({'valid': True, 'user': {'id': payload['user_id'], 'email': payload['email']}}), 200
    else:
        return jsonify({'valid': False}), 401

@app.route('/user-profile', methods=['GET'])
@token_required
def get_user_profile():
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        
        c.execute('SELECT id, email, full_name, created_at, last_login, google_id FROM users WHERE id = ?', 
                  (request.user_id,))
        user = c.fetchone()
        conn.close()
        
        if not user:
            return jsonify({'message': 'User not found'}), 404
        
        return jsonify({
            'id': user[0],
            'email': user[1],
            'full_name': user[2],
            'created_at': user[3],
            'last_login': user[4],
            'is_google_account': user[5] is not None
        }), 200
    
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500

@app.route('/change-password', methods=['POST'])
@token_required
def change_password():
    data = request.json
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    
    if not current_password or not new_password:
        return jsonify({'message': 'Current password and new password are required'}), 400
    
    if len(new_password) < 8:
        return jsonify({'message': 'New password must be at least 8 characters'}), 400
    
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        
        c.execute('SELECT password_hash FROM users WHERE id = ?', (request.user_id,))
        result = c.fetchone()
        
        if not result or not result[0]:
            conn.close()
            return jsonify({'message': 'Cannot change password for Google-authenticated accounts'}), 400
        
        password_hash = result[0]
        
        if not verify_password(current_password, password_hash):
            conn.close()
            return jsonify({'message': 'Current password is incorrect'}), 401
        
        new_password_hash = hash_password(new_password)
        c.execute('UPDATE users SET password_hash = ? WHERE id = ?', (new_password_hash, request.user_id))
        conn.commit()
        conn.close()
        
        return jsonify({'message': 'Password changed successfully'}), 200
    
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500

# ============================================
# MAIN ROUTES
# ============================================

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    token = request.args.get('token')
    if not token and request.headers.get('Authorization'):
        token = request.headers.get('Authorization').replace('Bearer ', '')
    if session.get('logged_in') or (token and verify_token(token)):
        return render_template('index.html')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    session.pop('user_id', None)
    session.pop('email', None)
    return jsonify({'success': True, 'message': 'Logged out successfully'}), 200

# ============================================
# DATABASE API ROUTES
# ============================================

@app.route('/connect', methods=['POST'])
def connect():
    data = request.json
    db_type = data.get('db_type')
    host = data.get('host')
    username = data.get('username')
    password = data.get('password')
    port = data.get('port')
    database = data.get('database')
    
    db_connector.close()
    
    success, message = db_connector.connect(
        db_type, host, username, password, port, database
    )
    
    if success:
        refresh_data_values()
        session.permanent = True
        session['connected'] = True
        session['db_type'] = db_type
        session['database'] = db_connector.current_database
        return jsonify({
            'success': True,
            'message': message,
            'db_type': db_type,
            'database': db_connector.current_database or 'None'
        })
    else:
        return jsonify({
            'success': False,
            'message': message
        })

@app.route('/disconnect', methods=['POST'])
def disconnect():
    db_connector.close()
    global _data_values, _table_sizes
    _data_values = {}
    _table_sizes = {}
    session.clear()
    return jsonify({'success': True, 'message': 'Disconnected successfully'})

def nl_pipeline(instruction):
    """Run the NL2SQL pipeline against the current database.

    Returns (result, error, meta). meta carries the generated SQL, the mapped
    table/columns and the detected operation. The pipeline now covers the full
    SQL domain:
      - SELECT keeps the schema-mapped fallback behaviour.
      - DDL (CREATE/DROP/ALTER/TRUNCATE/RENAME) does not require the table to
        exist yet.
      - DML (INSERT/UPDATE/DELETE) and DCL (GRANT/REVOKE) require an existing
        table.
      - TCL (COMMIT/ROLLBACK/SAVEPOINT) needs no table at all.
    """
    schema_raw, schema_err = db_connector.get_schema()
    if schema_err:
        return None, schema_err, None

    schema = {t: [c['name'] for c in cols] for t, cols in (schema_raw or {}).items()}

    try:
        nl = generate_sql(instruction, schema_raw if schema_raw else schema, _data_values, _table_sizes)
    except Exception:
        nl = {'table': None, 'suggested_table': None, 'columns': [],
              'sql': None, 'operation': None}

    table = nl.get('table')
    columns = nl.get('columns') or []
    generated_sql = nl.get('sql')
    operation = nl.get('operation')
    fallback_used = False

    # Operations that need no pre-existing table (TCL, DDL like CREATE, Utilities like SHOW TABLES/DATABASES)
    if not table and generated_sql:
        result, error = db_connector.execute_query(generated_sql)
        if not error and operation in (
                'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP',
                'TRUNCATE', 'RENAME'):
            refresh_data_values()
        if error:
            return None, error, None
        return result, None, {
            'generated_sql': generated_sql,
            'table': None,
            'columns': [],
            'fallback': False,
            'operation': operation,
        }

    # The model identified a table name that does not exist in this database.
    if not table and nl.get('suggested_table'):
        return None, 'table_not_found', {
            'not_found': True,
            'suggested_table': nl['suggested_table'],
            'message': ("The table '" + str(nl['suggested_table']) +
                        "' does not exist in this database.")
        }

    # Table name could not be extracted / mapped -> ask the user.
    if not table:
        return None, 'no_table', {
            'clarification': True,
            'message': ("I couldn't identify the table from your instruction. "
                        "Please mention the table name you want to query, "
                        "e.g. 'show all students' or 'list data from the "
                        "teacher table'.")
        }

    if not columns and operation == 'SELECT':
        columns = schema.get(table, [])[:1] if schema.get(table) else []

    if generated_sql:
        result, error = db_connector.execute_query(generated_sql)
        if not error and operation in (
                'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP',
                'TRUNCATE', 'RENAME'):
            # Data changed -> refresh the remembered values so later
            # predictions keep matching the current database.
            refresh_data_values()
        if error and operation == 'SELECT':
            # A failed SELECT can still fall back - keep the error only for
            # DDL/DML/DCL where the real reason is more useful than a retry.
            generated_sql = None
        elif error:
            return None, error, {
                'generated_sql': generated_sql,
                'table': table,
                'columns': columns,
                'fallback': False,
                'operation': operation,
            }

    # Non-SELECT statements cannot fall back to a SELECT - tell the user.
    if not generated_sql and operation != 'SELECT':
        return None, 'generation_failed', {
            'clarification': True,
            'message': ("I couldn't generate the " + str(operation or '') +
                        " statement for table '" + str(table) +
                        "'. Please rephrase your instruction.")
        }

    # Model failed / SQL failed to run -> always show output with a safe
    # SELECT <columns> FROM <table>; built from the mapped real names.
    if not generated_sql:
        generated_sql = fallback_sql(table, columns)
        fallback_used = True

    if generated_sql:
        result, error = db_connector.execute_query(generated_sql)
        if error:
            return None, error, None
    else:
        return None, 'no_columns', {
            'clarification': True,
            'message': ("I couldn't identify any column for table '" +
                        str(table) + "'. Please rephrase your instruction.")
        }

    return result, None, {
        'generated_sql': generated_sql,
        'table': table,
        'columns': columns,
        'fallback': fallback_used,
        'operation': operation,
    }


@app.route('/execute', methods=['POST'])
def execute_query():
    if not db_connector.connection:
        return jsonify({
            'success': False,
            'error': 'No active database connection. Please connect first.'
        })
    
    data = request.json
    query = data.get('query', '').strip()

    if not query:
        return jsonify({
            'success': False,
            'error': 'Please enter a SQL query.'
        })

    # Natural-language instruction -> run through the NL2SQL pipeline.
    if not is_sql_query(query):
        result, error, meta = nl_pipeline(query)
        if error:
            if meta and meta.get('not_found'):
                return jsonify({
                    'success': False,
                    'not_found': True,
                    'suggested_table': meta.get('suggested_table'),
                    'error': meta['message']
                })
            if meta and meta.get('clarification'):
                return jsonify({
                    'success': False,
                    'clarification': True,
                    'error': meta['message']
                })
            return jsonify({'success': False, 'error': error})
        return jsonify({
            'success': True,
            'result': result,
            'generated_sql': meta['generated_sql'],
            'table': meta['table'],
            'columns': meta['columns'],
            'fallback': meta['fallback']
        })

    # Raw SQL with slightly wrong table/column names (case, plurals, typos like
    # "SELECT * FROM BOOKINGS" or "WHERE NAM = 'x'") -> correct against the real
    # schema before it runs, so the user never sees an "unknown column/table"
    # error for a name they got almost right.
    try:
        schema_raw, schema_err = db_connector.get_schema()
        if not schema_err and schema_raw:
            schema_map = {t: [c['name'] for c in cols] for t, cols in schema_raw.items()}
            corrected = correct_sql_identifiers(query, schema_map)
            if corrected and corrected != query:
                query = corrected
    except Exception:
        pass

    result, error = db_connector.execute_query(query)

    # Raw SQL that changed data -> refresh the remembered values so later NL
    # predictions keep matching the current database.
    if not error and re.match(
            r'^\s*(insert|update|delete|create|alter|drop|truncate|rename)\b',
            query, re.IGNORECASE):
        refresh_data_values()

    # Input looked like SQL but failed to run (e.g. "select * from a STUDENT
    # where ...") -> retry through the NL2SQL pipeline so output is always
    # shown instead of a raw DB error.
    if error and re.match(r'^\s*select\b', query, re.IGNORECASE):
        nl_result, nl_error, nl_meta = nl_pipeline(query)
        if not nl_error:
            return jsonify({
                'success': True,
                'result': nl_result,
                'generated_sql': nl_meta['generated_sql'],
                'table': nl_meta['table'],
                'columns': nl_meta['columns'],
                'fallback': nl_meta['fallback'],
                'retried_from_error': error
            })

    if error:
        return jsonify({
            'success': False,
            'error': error
        })
    
    return jsonify({
        'success': True,
        'result': result
    })

@app.route('/use-database', methods=['POST'])
def use_database():
    """Switch the active database without a full /execute round-trip.

    The browser uses this when a database name is clicked, so the switch is a
    single reliable call (no NL2SQL misrouting for unusual database names).
    """
    if not db_connector.connection:
        return jsonify({'success': False, 'error': 'No database connection'})

    database = (request.json or {}).get('database', '').strip().strip('`" ')
    if not database:
        return jsonify({'success': False, 'error': 'Database name is required'})

    try:
        if db_connector.db_type == 'mysql':
            cursor = db_connector.connection.cursor()
            cursor.execute('USE `' + database.replace('`', '``') + '`')
            db_connector.current_database = database
            cursor.close()
        else:
            db_connector.connection.close()
            params = db_connector.connection_params
            db_connector.connection = psycopg2.connect(
                host=params['host'],
                user=params['username'],
                password=params['password'],
                port=int(params['port']) if params.get('port') else 5432,
                database=database
            )
            db_connector.current_database = database
        return jsonify({
            'success': True,
            'message': f'Switched to database: {database}',
            'database': database
        })
    except mysql.connector.Error as e:
        return jsonify({'success': False, 'error': friendly_error(f"MySQL Error: {str(e)}")})
    except psycopg2.Error as e:
        return jsonify({'success': False, 'error': friendly_error(f"PostgreSQL Error: {str(e)}")})
    except Exception as e:
        return jsonify({'success': False, 'error': friendly_error(f"Error: {str(e)}")})


@app.route('/get-databases', methods=['GET'])
def get_databases():
    if not db_connector.connection:
        return jsonify({'success': False, 'error': 'No database connection'})
    
    databases, error = db_connector.get_databases()
    
    if error:
        return jsonify({'success': False, 'error': error})
    
    return jsonify({
        'success': True,
        'databases': databases,
        'current': db_connector.current_database
    })

@app.route('/get-tables', methods=['GET'])
def get_tables():
    if not db_connector.connection:
        return jsonify({'success': False, 'error': 'No database connection'})
    
    database = request.args.get('database')
    tables, error = db_connector.get_tables(database)
    
    if error:
        return jsonify({'success': False, 'error': error})
    
    return jsonify({
        'success': True,
        'tables': tables,
        'current_database': db_connector.current_database
    })

@app.route('/get-tables-with-data', methods=['GET'])
def get_tables_with_data():
    if not db_connector.connection:
        return jsonify({'success': False, 'error': 'No database connection'})
    
    table_data, error = db_connector.get_tables_with_data()
    
    if error:
        return jsonify({'success': False, 'error': error})
    
    return jsonify({
        'success': True,
        'tables': table_data,
        'database': db_connector.current_database
    })

@app.route('/get-table-data', methods=['GET'])
def get_table_data():
    if not db_connector.connection:
        return jsonify({'success': False, 'error': 'No database connection'})
    
    table = request.args.get('table')
    if not table:
        return jsonify({'success': False, 'error': 'Table name is required'})
    
    data, error = db_connector.get_table_data(table)
    
    if error:
        return jsonify({'success': False, 'error': error})
    
    return jsonify({
        'success': True,
        'table': data,
        'database': db_connector.current_database
    })

@app.route('/get-schema', methods=['GET'])
def get_schema():
    if not db_connector.connection:
        return jsonify({'success': False, 'error': 'No database connection'})
    
    table = request.args.get('table')
    schema, error = db_connector.get_schema(table)
    
    if error:
        return jsonify({'success': False, 'error': error})
    
    return jsonify({
        'success': True,
        'schema': schema
    })

@app.route('/check-session', methods=['GET'])
def check_session():
    if db_connector.connection:
        if db_connector.check_connection():
            return jsonify({
                'connected': True,
                'database': db_connector.current_database,
                'db_type': db_connector.db_type
            })
        else:
            success, message = db_connector.reconnect()
            if success:
                return jsonify({
                    'connected': True,
                    'database': db_connector.current_database,
                    'db_type': db_connector.db_type,
                    'reconnected': True
                })
            else:
                return jsonify({
                    'connected': False,
                    'error': message
                })
    return jsonify({'connected': False})

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

if __name__ == '__main__':
    port = 5001
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('0.0.0.0', port))
            s.close()
    except OSError:
        port = find_free_port()
        print(f"⚠️ Port 5001 is in use. Using port {port} instead.")

    print(f"\n{'='*50}")
    print(f"🚀 Starting NL2SQL Interface with Authentication...")
    print(f"📱 Open your browser at: http://localhost:{port}")
    print(f"🔐 Login or Sign up to access the SQL interface")
    print(f"💾 Session will be preserved across page refreshes")
    print(f"🔄 Press Ctrl+C to stop the server")
    print(f"{'='*50}\n")
    app.run(debug=True, port=port, host='0.0.0.0', use_reloader=False)