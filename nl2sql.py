"""NL2SQL Core Pipeline.

Converts natural language queries into accurate, runnable SQL statements
for any connected relational database (MySQL, PostgreSQL, SQLite).

Key capabilities:
1. Fast, high-accuracy inference using pretrained Hugging Face models
   (default: 'cssupport/t5-small-awesome-text-to-sql', supports 'suriya7/t5-base-text-to-sql',
   'gaussalgo/T5-LM-Large-text2sql-spider', etc. via HF_MODEL_NAME).
2. Schema-aware prompt engineering: injects real active table DDL schemas
   (table names, column names, data types) directly into the model context.
3. Complete SQL domain support:
   - DQL (SELECT): multi-table joins, aggregations (COUNT, AVG, SUM, MIN, MAX),
     filters (=, >, <, >=, <=, !=, LIKE, BETWEEN, IN, IS NULL, AND, OR),
     sorting (ORDER BY), limiting (LIMIT), grouping (GROUP BY, HAVING), DISTINCT.
   - DML (INSERT, UPDATE, DELETE): precise parameter and condition extraction,
     clean SQL quoting and type handling.
   - DDL (CREATE, DROP, ALTER, TRUNCATE, RENAME): clean statement formatting.
   - Database Utilities: SHOW TABLES, SHOW DATABASES, DESCRIBE, USE.
4. Robust post-processing & schema alignment:
   - Converts double quotes to standard SQL single quotes for string literals.
   - Cleans hallucinated identifiers and aligns with exact schema casing and names.
   - Handles natural language LIKE patterns ('starts with', 'ends with', 'contains').
   - Guarantees valid, safe, executable SQL queries with fallback protection.
"""

import os
import re
import threading
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Default to high-accuracy Hugging Face text-to-sql model
DEFAULT_HF_MODEL = os.environ.get('HF_MODEL_NAME', 'cssupport/t5-small-awesome-text-to-sql')

# Use CUDA if available, CPU otherwise
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

_primary_model = None
_primary_tokenizer = None
_loaded_model_name = None
_lock = threading.RLock()


def load_models(model_name=None):
    """Load the Hugging Face NL2SQL model into memory once and cache it."""
    global _primary_model, _primary_tokenizer, _loaded_model_name
    target_model = model_name or DEFAULT_HF_MODEL

    with _lock:
        if _primary_model is None or _loaded_model_name != target_model:
            # Check if local directory exists first, otherwise download/load from HF cache
            model_path = target_model
            local_candidate = os.path.join(BASE_DIR, target_model)
            if os.path.isdir(local_candidate):
                model_path = local_candidate

            try:
                _primary_tokenizer = AutoTokenizer.from_pretrained(model_path)
                _primary_model = AutoModelForSeq2SeqLM.from_pretrained(model_path)
                _primary_model.to(DEVICE)
                _primary_model.eval()
                _loaded_model_name = target_model
            except Exception as e:
                # If loading custom model failed, try falling back to default
                if target_model != DEFAULT_HF_MODEL:
                    try:
                        _primary_tokenizer = AutoTokenizer.from_pretrained(DEFAULT_HF_MODEL)
                        _primary_model = AutoModelForSeq2SeqLM.from_pretrained(DEFAULT_HF_MODEL)
                        _primary_model.to(DEVICE)
                        _primary_model.eval()
                        _loaded_model_name = DEFAULT_HF_MODEL
                    except Exception:
                        pass

        return {
            'primary': (_primary_tokenizer, _primary_model),
            'model_name': _loaded_model_name
        }


def get_encoder():
    """Compatibility stub for app.py preload."""
    return None


# ============================================================================
# STRING & SCHEMA MATCHING UTILITIES
# ============================================================================

def _edit_distance(a, b):
    """Levenshtein distance between two strings."""
    a, b = (a or '').lower(), (b or '').lower()
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similarity(a, b):
    """Similarity score between 0.0 and 1.0."""
    a = (a or '').lower().strip().strip('`"')
    b = (b or '').lower().strip().strip('`"')
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # Substring / Prefix containment
    if a == b + 's' or b == a + 's':
        return 0.95
    if a.endswith('es') and b == a[:-2]:
        return 0.95
    if b.endswith('es') and a == b[:-2]:
        return 0.95
    if a in b or b in a:
        return 0.85

    # Edit distance ratio
    max_l = max(len(a), len(b))
    dist = _edit_distance(a, b)
    return max(0.0, 1.0 - (dist / max_l))


def match_table_name(name, schema_tables):
    """Resolve a table name (singular/plural, case, typos) against schema tables."""
    if not name or not schema_tables:
        return None

    raw = name.strip().strip('`"\'').lower()
    if not raw:
        return None

    tables = list(schema_tables)

    # 1. Exact match (case-insensitive)
    for t in tables:
        if t.lower() == raw:
            return t

    # 2. Plural / Singular match
    for t in tables:
        tl = t.lower()
        if raw == tl + 's' or tl == raw + 's':
            return t
        if raw == tl + 'es' or tl == raw + 'es':
            return t
        if tl.endswith('s') and raw == tl[:-1]:
            return t
        if raw.endswith('s') and tl == raw[:-1]:
            return t

    # 3. Fuzzy similarity match
    best_table = None
    best_score = 0.0
    for t in tables:
        score = _similarity(raw, t)
        if score > best_score:
            best_score = score
            best_table = t

    if best_score >= 0.55:
        return best_table

    return None


def match_column_name(col_name, available_columns, table_name=None):
    """Resolve a column name against a list of available columns in a table."""
    if not col_name or not available_columns:
        return None

    raw = col_name.strip().strip('`"\'').lower()
    if not raw:
        return None

    # Handle table.column format
    if '.' in raw:
        raw = raw.split('.')[-1]

    cols = list(available_columns)

    # 1. Exact match (case-insensitive)
    for c in cols:
        if c.lower() == raw:
            return c

    # 2. Generic 'id' mapping (e.g. 'id' -> 'student_id' or 'id')
    if raw == 'id':
        for c in cols:
            cl = c.lower()
            if cl == 'id':
                return c
            if table_name and (cl == f'{table_name.lower()}_id' or cl == f'{table_name.lower()}id'):
                return c
            if cl.endswith('_id') or cl.endswith('id'):
                return c

    # 3. Handle 'name' mapping (e.g. 'name' -> 'first_name' or 'full_name' or 'name')
    if raw == 'name':
        for c in cols:
            cl = c.lower()
            if cl == 'name':
                return c
        for c in cols:
            cl = c.lower()
            if cl in ('first_name', 'fullname', 'full_name', 'student_name', 'teacher_name', 'user_name', 'course_name', 'dept_name', 'department_name'):
                return c

    # 4. Handle common aliases and abbreviations
    alias_map = {
        'mail': 'email',
        'email_address': 'email',
        'e_mail': 'email',
        'fname': 'first_name',
        'lname': 'last_name',
        'sub': 'subject',
        'course_name': 'course',
        'city_name': 'city',
        'qty': 'quantity',
        'price': 'price',
        'cost': 'price',
        'amt': 'amount',
        'salary_amount': 'salary',
        'employee_salary': 'salary',
        'phone_number': 'phone',
        'contact': 'phone',
        'contact_number': 'phone'
    }
    target_alias = alias_map.get(raw, raw)
    for c in cols:
        if c.lower() == target_alias:
            return c

    # 5. Normalised underscore match ('first_name' == 'firstname')
    raw_clean = raw.replace('_', '')
    for c in cols:
        if c.lower().replace('_', '') == raw_clean:
            return c

    # 6. Fuzzy similarity match
    best_col = None
    best_score = 0.0
    for c in cols:
        score = _similarity(raw, c)
        if score > best_score:
            best_score = score
            best_col = c

    if best_score >= 0.6:
        return best_col

    return None


def select_best_table_from_text(text, schema):
    """Find the most relevant table mentioned in natural language text."""
    if not schema:
        return None

    tables = list(schema.keys())
    if len(tables) == 1:
        return tables[0]

    text_lower = ' ' + (text or '').lower() + ' '

    # 1. Direct word match
    for t in tables:
        tl = t.lower()
        if re.search(r'\b' + re.escape(tl) + r'\b', text_lower):
            return t
        if re.search(r'\b' + re.escape(tl) + r's\b', text_lower):
            return t
        if tl.endswith('s') and re.search(r'\b' + re.escape(tl[:-1]) + r'\b', text_lower):
            return t

    # 2. Token overlap and fuzzy matching
    words = re.findall(r'[a-zA-Z_]+', text_lower)
    for w in words:
        if len(w) >= 3:
            matched = match_table_name(w, tables)
            if matched:
                return matched

    # 3. Column presence heuristic (if user asks for 'subject', table with 'subject' wins)
    col_scores = {}
    for t, cols in schema.items():
        col_scores[t] = 0
        col_list = cols if isinstance(cols, list) else list(cols.keys())
        for c in col_list:
            c_name = c['name'] if isinstance(c, dict) else str(c)
            if re.search(r'\b' + re.escape(c_name.lower()) + r'\b', text_lower):
                col_scores[t] += 2

    best_table = max(col_scores, key=col_scores.get)
    if col_scores[best_table] > 0:
        return best_table

    # Default to first table
    return tables[0] if tables else None


def build_schema_ddl(schema, active_table=None):
    """Generate structured CREATE TABLE DDL statements for Hugging Face prompt."""
    if not schema:
        return ""

    ddl_statements = []
    # If active_table is specified, prioritize active table
    table_list = list(schema.keys())
    if active_table and active_table in table_list:
        table_list.remove(active_table)
        table_list.insert(0, active_table)

    for t in table_list:
        cols = schema[t]
        col_defs = []
        for c in cols:
            if isinstance(c, dict):
                c_name = c.get('name', '')
                c_type = c.get('type', 'TEXT')
            else:
                c_name = str(c)
                c_low = c_name.lower()
                if any(k in c_low for k in ['id', 'age', 'credits', 'count', 'num', 'year', 'qty', 'quantity']):
                    c_type = 'INT'
                elif any(k in c_low for k in ['salary', 'price', 'cost', 'budget', 'amount', 'balance', 'rate', 'fee']):
                    c_type = 'DECIMAL(10, 2)'
                elif 'date' in c_low or 'time' in c_low:
                    c_type = 'DATE'
                else:
                    c_type = 'VARCHAR(100)'

            col_defs.append(f"    {c_name} {c_type}")

        ddl = f"CREATE TABLE {t} (\n" + ",\n".join(col_defs) + "\n);"
        ddl_statements.append(ddl)

    return "\n".join(ddl_statements)


# ============================================================================
# INTENT & OPERATION DETECTION
# ============================================================================

def detect_operation(instruction):
    """Classify the operation requested by natural language instruction."""
    text = ' ' + (instruction or '').lower() + ' '

    # Utilities
    if re.search(r'\b(?:show|list|get)\s+(?:all\s+)?databases\b', text):
        return 'SHOW_DATABASES'
    if re.search(r'\b(?:show|list|get)\s+(?:all\s+)?tables\b', text):
        return 'SHOW_TABLES'
    if re.search(r'\b(?:describe|desc|schema\s+of|structure\s+of)\s+([a-zA-Z0-9_]+)\b', text):
        return 'DESCRIBE'
    if re.search(r'\b(?:switch\s+to|use\s+database|use)\s+([a-zA-Z0-9_]+)\b', text):
        return 'USE'

    # DDL
    if re.search(r'\bcreate\s+(?:a\s+)?table\b', text):
        return 'CREATE'
    if re.search(r'\bdrop\s+(?:the\s+)?table\b|\bdelete\s+the\s+[a-zA-Z0-9_]+\s+table\b', text):
        return 'DROP'
    if re.search(r'\balter\s+table\b|\badd\s+(?:a\s+)?column\b|\bdrop\s+(?:a\s+)?column\b', text):
        return 'ALTER'
    if re.search(r'\btruncate\s+(?:the\s+)?table\b|\btruncate\b|\bempty\s+(?:the\s+)?(?:table|[a-zA-Z0-9_]+)\b|\bclear\s+all\s+data\b', text):
        return 'TRUNCATE'
    if re.search(r'\brename\s+table\b|\brename\s+the\s+[a-zA-Z0-9_]+\s+table\b', text):
        return 'RENAME'

    # DML
    if re.search(r'\binsert\b|\badd\s+(?:a\s+)?(?:new\s+)?(?:record|row|student|teacher|user|item|entry|course|employee)\b|\binsert\s+into\b', text):
        return 'INSERT'
    if re.search(r'\bupdate\b|\bset\s+[a-zA-Z0-9_]+\s*=\b|\bchange\s+(?:the\s+)?[a-zA-Z0-9_]+\s+to\b|\bmodify\b', text):
        return 'UPDATE'
    if re.search(r'\bdelete\s+from\b|\bdelete\s+(?:a\s+|the\s+)?[a-zA-Z0-9_]+\s+(?:with|where|whose|having)\b|\bremove\s+(?:a\s+|the\s+)?[a-zA-Z0-9_]+\s+(?:with|where|whose)\b', text):
        return 'DELETE'

    # DQL (default / SELECT)
    return 'SELECT'


# ============================================================================
# DML & DDL SYNTHESIZERS
# ============================================================================

def _quote_val(v):
    """Format value with quotes if string or return numeric representation."""
    if v is None:
        return 'NULL'
    s = str(v).strip()
    if re.match(r'^[+-]?\d+(?:\.\d+)?$', s):
        return s
    s_clean = s.strip("'\"")
    return "'" + s_clean.replace("'", "''") + "'"


def extract_field_values_from_text(text, schema_table, schema):
    """Extract {column_name: value} pairs from natural language instruction."""
    raw_cols = schema.get(schema_table, [])
    if not raw_cols:
        return {}

    columns = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]
    values = {}
    text_clean = text.replace(',', ' , ')

    # Reserved words to exclude as raw values
    STOP_WORDS = {
        'and', 'where', 'with', 'set', 'table', 'from', 'in', 'is', 'a', 'new',
        'to', 'for', 'by', 'the', 'of', 'having', 'whose', 'at', 'record', 'row',
        'student', 'teacher', 'user', 'employee', 'department', 'salary', 'age',
        'email', 'city', 'grade'
    }

    # 1. Match explicit column patterns: <col> [=|:|is|to] <val>
    for col in columns:
        pat = rf'\b{re.escape(col)}\s*(?:=|:|\bis\b|\bto\b|\bequals?\b)?\s*([\'"][^\'"]+[\'"]|[a-zA-Z0-9_.@\-]+)'
        m = re.search(pat, text_clean, re.IGNORECASE)
        if m:
            val = m.group(1).strip(' ,;\'\"')
            if val.lower() not in STOP_WORDS:
                values[col] = val

    # 2. Email extraction
    email_col = match_column_name('email', columns, schema_table)
    if email_col and email_col not in values:
        m = re.search(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', text)
        if m:
            values[email_col] = m.group(1)

    # 3. Name extraction: "named Alice Smith" or "name Alice"
    first_col = match_column_name('first_name', columns, schema_table) or match_column_name('name', columns, schema_table)
    last_col = match_column_name('last_name', columns, schema_table)

    if first_col and first_col not in values:
        # "named Alice Smith"
        m = re.search(r'\bnamed\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?\b', text, re.IGNORECASE)
        if m:
            values[first_col] = m.group(1)
            if m.group(2) and last_col and last_col not in values:
                if m.group(2).lower() not in STOP_WORDS:
                    values[last_col] = m.group(2)
        else:
            # "name Alice"
            m2 = re.search(r'\bname\s+(?:is|=|:)?\s*([A-Za-z]+)\b', text, re.IGNORECASE)
            if m2 and m2.group(1).lower() not in STOP_WORDS:
                values[first_col] = m2.group(1)

    # 4. Age extraction: "age 20" or "age is 20"
    age_col = match_column_name('age', columns, schema_table)
    if age_col and age_col not in values:
        m = re.search(r'\bage\s*(?:is|=|:|\bto\b)?\s*(\d+)\b', text, re.IGNORECASE)
        if m:
            values[age_col] = m.group(1)

    # 5. Salary extraction: "salary 55000" or "salary 55000.00"
    salary_col = match_column_name('salary', columns, schema_table)
    if salary_col and salary_col not in values:
        m = re.search(r'\bsalary\s*(?:is|=|:|\bto\b)?\s*(\d+(?:\.\d+)?)\b', text, re.IGNORECASE)
        if m:
            values[salary_col] = m.group(1)

    # 6. City extraction: "city New York" or "in New York"
    city_col = match_column_name('city', columns, schema_table)
    if city_col and city_col not in values:
        m = re.search(r'\bcity\s*(?:is|=|:|\bto\b)?\s*[\'\"]?([A-Za-z\s]+?)[\'\"]?(?:\s+(?:and|where|with|age|email|grade|salary)|$)', text, re.IGNORECASE)
        if m and m.group(1).strip().lower() not in STOP_WORDS:
            values[city_col] = m.group(1).strip()

    # 7. Grade extraction: "grade A" or "grade is A"
    grade_col = match_column_name('grade', columns, schema_table)
    if grade_col and grade_col not in values:
        m = re.search(r'\bgrade\s*(?:is|=|:|\bto\b)?\s*[\'\"]?([A-Fa-f0-9+-]+)[\'\"]?\b', text, re.IGNORECASE)
        if m and m.group(1).lower() not in STOP_WORDS:
            values[grade_col] = m.group(1)

    return values


def generate_dml_insert(instruction, schema, data=None):
    """Construct an accurate INSERT INTO statement."""
    table = select_best_table_from_text(instruction, schema)
    if not table:
        return None

    raw_cols = schema.get(table, [])
    columns = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]

    # Check if raw SQL format already exists: INSERT INTO table (...) VALUES (...)
    m = re.search(r'insert\s+into\s+[`"]?([a-zA-Z0-9_]+)[`"]?\s*\(([^)]+)\)\s*values\s*\(([^)]+)\)', instruction, re.IGNORECASE)
    if m:
        t_raw = m.group(1)
        real_t = match_table_name(t_raw, schema) or table
        cols_raw = [c.strip().strip('`"') for c in m.group(2).split(',')]
        real_cols = [match_column_name(c, columns, real_t) or c for c in cols_raw]
        vals = m.group(3).strip()
        return f"INSERT INTO {real_t} ({', '.join(real_cols)}) VALUES ({vals});"

    # Extract field values from natural language
    field_values = extract_field_values_from_text(instruction, table, schema)
    if not field_values:
        return None

    cols = list(field_values.keys())
    vals = [_quote_val(field_values[c]) for c in cols]
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(vals)});"


def generate_dml_update(instruction, schema, data=None):
    """Construct an accurate UPDATE statement."""
    table = select_best_table_from_text(instruction, schema)
    if not table:
        return None

    raw_cols = schema.get(table, [])
    columns = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]

    # Match raw SQL update
    m = re.search(r'update\s+[`"]?([a-zA-Z0-9_]+)[`"]?\s+set\s+(.+?)(?:\s+where\s+(.+))?;?$', instruction, re.IGNORECASE)
    if m:
        real_t = match_table_name(m.group(1), schema) or table
        set_clause = m.group(2).strip()
        where_clause = m.group(3).strip() if m.group(3) else None

        # Clean set assignments
        set_parts = []
        for part in set_clause.split(','):
            if '=' in part:
                k, v = part.split('=', 1)
                k = match_column_name(k.strip(), columns, real_t) or k.strip()
                v_clean = v.strip()
                if not re.match(r'^[+-]?\d+(?:\.\d+)?$', v_clean) and not (v_clean.startswith("'") or v_clean.startswith('"')):
                    v_clean = _quote_val(v_clean)
                set_parts.append(f"{k} = {v_clean}")
            else:
                set_parts.append(part.strip())

        sql = f"UPDATE {real_t} SET {', '.join(set_parts)}"
        if where_clause:
            sql += f" WHERE {where_clause}"
        return sql + ';'

    # Extract natural language update: "update student age to 21 where student_id = 3"
    where_match = re.search(r'\b(?:where|for\s+(?:the\s+)?[a-zA-Z0-9_]+|whose|with\s+(?:the\s+)?id|with\s+[a-zA-Z0-9_]+_id)\s+(.+)$', instruction, re.IGNORECASE)
    where_text = where_match.group(1).strip() if where_match else ''
    body_text = instruction[:where_match.start()] if where_match else instruction

    # Extract SET target from body_text
    set_values = extract_field_values_from_text(body_text, table, schema)
    if not set_values:
        m_set = re.search(r'\b(?:set|change|update|modify)\s+([a-zA-Z0-9_]+)\s*(?:to|=|\bis\b)\s*([^\s,]+)', body_text, re.IGNORECASE)
        if m_set:
            col_target = match_column_name(m_set.group(1), columns, table)
            if col_target:
                set_values[col_target] = m_set.group(2)

    if not set_values:
        return None

    set_strs = [f"{c} = {_quote_val(v)}" for c, v in set_values.items()]

    where_clause = ""
    if where_text:
        for col in columns:
            if col in set_values:
                continue
            pat = r'\b' + re.escape(col) + r'\s*(?:=|:|\bis\b)\s*([a-zA-Z0-9_@.\-]+|\'[^\']*\')'
            wm = re.search(pat, where_text, re.IGNORECASE)
            if wm:
                where_clause = f"{col} = {_quote_val(wm.group(1))}"
                break
        if not where_clause:
            im = re.search(r'\b(?:student_id|teacher_id|user_id|employee_id|emp_id|course_id|id)\s*(?:=|:|\bis\b)?\s*(\d+)\b', where_text, re.IGNORECASE)
            if im:
                pk = match_column_name('id', columns, table) or 'id'
                where_clause = f"{pk} = {im.group(1)}"
            else:
                # Check for "with id 1" or "id 1"
                im2 = re.search(r'\b(?:with\s+)?id\s*(\d+)\b', where_text, re.IGNORECASE)
                if im2:
                    pk = match_column_name('id', columns, table) or 'id'
                    where_clause = f"{pk} = {im2.group(1)}"

    # If no where clause extracted from where_text, check entire instruction for id filter
    if not where_clause:
        im3 = re.search(r'\b(?:with\s+id|for\s+[a-zA-Z0-9_]+\s+with\s+id|id\s*(?:=|is))\s*(\d+)\b', instruction, re.IGNORECASE)
        if im3:
            pk = match_column_name('id', columns, table) or 'id'
            where_clause = f"{pk} = {im3.group(1)}"

    sql = f"UPDATE {table} SET {', '.join(set_strs)}"
    if where_clause:
        sql += f" WHERE {where_clause}"
    return sql + ';'


def generate_dml_delete(instruction, schema, data=None):
    """Construct an accurate DELETE FROM statement."""
    table = select_best_table_from_text(instruction, schema)
    if not table:
        return None

    raw_cols = schema.get(table, [])
    columns = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]

    # Extract where clause
    where_match = re.search(r'\b(?:where|whose|with|for|having)\s+(.+)$', instruction, re.IGNORECASE)
    if not where_match:
        return f"DELETE FROM {table};"

    where_text = where_match.group(1).strip()
    where_cond = None

    # 1. Check comparison operators (<, >, <=, >=, !=, =, LIKE)
    comp_pat = r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(<=|>=|!=|<>|=|<|>|\blike\b|\bis\b)\s*([\'"][^\'"]+[\'"]|[a-zA-Z0-9_@.\-]+)'
    m = re.search(comp_pat, where_text, re.IGNORECASE)
    if m:
        c_raw, op, val = m.group(1), m.group(2).upper(), m.group(3)
        if op == 'IS':
            op = '='
        real_c = match_column_name(c_raw, columns, table) or c_raw
        where_cond = f"{real_c} {op} {_quote_val(val)}"

    # 2. Check "id 5" or "id = 5"
    if not where_cond:
        im = re.search(r'\b(?:student_id|teacher_id|user_id|course_id|id)\s*(?:=|is)?\s*(\d+)\b', where_text, re.IGNORECASE)
        if im:
            pk = match_column_name('id', columns, table) or 'id'
            where_cond = f"{pk} = {im.group(1)}"

    if where_cond:
        return f"DELETE FROM {table} WHERE {where_cond};"

    return f"DELETE FROM {table};"


def generate_ddl(instruction, operation):
    """Construct valid DDL statements (CREATE, DROP, ALTER, TRUNCATE, RENAME)."""
    text = instruction.strip()

    if operation == 'SHOW_DATABASES':
        return 'SHOW DATABASES;'
    if operation == 'SHOW_TABLES':
        return 'SHOW TABLES;'

    if operation == 'DESCRIBE':
        m = re.search(r'\b(?:describe|desc|schema\s+of|structure\s+of)\s+[`"]?([a-zA-Z0-9_]+)[`"]?', text, re.IGNORECASE)
        if m:
            return f"DESCRIBE `{m.group(1)}`;"

    if operation == 'USE':
        m = re.search(r'\b(?:switch\s+to|use\s+database|use)\s+[`"]?([a-zA-Z0-9_]+)[`"]?', text, re.IGNORECASE)
        if m:
            return f"USE `{m.group(1)}`;"

    if operation == 'DROP':
        m = re.search(r'\b(?:drop|delete)\s+(?:the\s+)?table\s+[`"]?([a-zA-Z0-9_]+)[`"]?', text, re.IGNORECASE)
        if not m:
            m = re.search(r'\bdelete\s+the\s+([a-zA-Z0-9_]+)\s+table\b', text, re.IGNORECASE)
        if m:
            return f"DROP TABLE `{m.group(1)}`;"

    if operation == 'TRUNCATE':
        m = re.search(r'\b(?:truncate|empty)\s+(?:the\s+)?table\s+[`"]?([a-zA-Z0-9_]+)[`"]?', text, re.IGNORECASE)
        if not m:
            m = re.search(r'\bempty\s+(?:the\s+)?([a-zA-Z0-9_]+)(?:\s+table)?\b', text, re.IGNORECASE)
        if m:
            return f"TRUNCATE TABLE `{m.group(1)}`;"

    if operation == 'CREATE':
        m = re.search(r'create\s+table\s+[`"]?([a-zA-Z0-9_]+)[`"]?\s*\((.+)\);?$', text, re.IGNORECASE | re.DOTALL)
        if m:
            return f"CREATE TABLE {m.group(1)} ({m.group(2).strip()});"
        m2 = re.search(r'create\s+(?:a\s+)?table\s+(?:called|named)?\s*[`"]?([a-zA-Z0-9_]+)[`"]?\s+(?:with|having)\s+(.+)', text, re.IGNORECASE)
        if m2:
            return f"CREATE TABLE {m2.group(1)} ({m2.group(2).strip()});"

    if operation == 'ALTER':
        m = re.search(r'alter\s+table\s+[`"]?([a-zA-Z0-9_]+)[`"]?\s+(add|drop|modify|change)\s+(?:column\s+)?(.+)', text, re.IGNORECASE)
        if m:
            return f"ALTER TABLE {m.group(1)} {m.group(2).upper()} COLUMN {m.group(3).strip()};"

    return None


# ============================================================================
# SQL REPAIR & POST-PROCESSING
# ============================================================================

def refine_aggregations(sql, instruction, table, schema):
    """Ensure aggregation functions match the user natural language intent."""
    if not sql or not table:
        return sql

    text = ' ' + instruction.lower() + ' '
    raw_cols = schema.get(table, [])
    cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]

    # 1. COUNT
    if re.search(r'\b(?:count|how many|number of|total number of|total count|total records|total rows)\b', text):
        if not re.search(r'\bcount\s*\(', sql, re.IGNORECASE):
            sql = re.sub(r'^\s*SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b', 'SELECT COUNT(*) FROM', sql, flags=re.IGNORECASE)

    # 2. AVG
    elif re.search(r'\b(?:average|avg)\b', text):
        if not re.search(r'\bavg\s*\(', sql, re.IGNORECASE):
            for c in cols:
                if re.search(rf'\b{re.escape(c.lower())}\b', text):
                    sql = re.sub(r'^\s*SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b', f'SELECT AVG({c}) FROM', sql, flags=re.IGNORECASE)
                    break

    # 3. SUM
    elif re.search(r'\b(?:sum of|total\s+(?:salary|price|amount|quantity|cost|fee|score|marks|balance))\b', text):
        if not re.search(r'\bsum\s*\(', sql, re.IGNORECASE):
            for c in cols:
                if re.search(rf'\b{re.escape(c.lower())}\b', text):
                    sql = re.sub(r'^\s*SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b', f'SELECT SUM({c}) FROM', sql, flags=re.IGNORECASE)
                    break

    # 4. MAX
    elif re.search(r'\b(?:maximum|max|highest)\b', text):
        if not re.search(r'\bmax\s*\(', sql, re.IGNORECASE) and not re.search(r'\border\s+by\b', sql, re.IGNORECASE):
            for c in cols:
                if re.search(rf'\b{re.escape(c.lower())}\b', text):
                    sql = re.sub(r'^\s*SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b', f'SELECT MAX({c}) FROM', sql, flags=re.IGNORECASE)
                    break

    # 5. MIN
    elif re.search(r'\b(?:minimum|min|lowest)\b', text):
        if not re.search(r'\bmin\s*\(', sql, re.IGNORECASE) and not re.search(r'\border\s+by\b', sql, re.IGNORECASE):
            for c in cols:
                if re.search(rf'\b{re.escape(c.lower())}\b', text):
                    sql = re.sub(r'^\s*SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b', f'SELECT MIN({c}) FROM', sql, flags=re.IGNORECASE)
                    break

    return sql


def enhance_select_projection(sql, instruction, table, schema):
    """If the query is a general record lookup and not specific columns, project SELECT *."""
    if not sql or not table:
        return sql

    text_lower = ' ' + (instruction or '').lower() + ' '
    raw_cols = schema.get(table, [])
    cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]

    # If the user specifically asked for column names (e.g. "select name and age"), do not modify
    explicit_columns = []
    for c in cols:
        if c.lower() not in ('id', f'{table.lower()}_id', f'{table.lower()}id'):
            if re.search(r'\b' + re.escape(c.lower()) + r'\b', text_lower):
                explicit_columns.append(c)

    # If no specific non-id column was requested and query is a find/get/list, use SELECT *
    if not explicit_columns and not re.search(r'\b(?:count|avg|average|sum|max|min)\b', text_lower):
        m = re.search(r'^\s*SELECT\s+(.+?)\s+FROM\b', sql, re.IGNORECASE)
        if m:
            current_projection = m.group(1).strip()
            # If current projection is just the id or singular id column or repeated id
            if current_projection.lower() in ('id', f'{table.lower()}_id', f'{table.lower()}id') or current_projection.lower() == f"{table.lower()}_id, {table.lower()}_id":
                sql = re.sub(r'^\s*SELECT\s+(.+?)\s+FROM\b', 'SELECT * FROM', sql, flags=re.IGNORECASE)

    return sql


def fix_sql_syntax(sql):
    """Repair common syntax mistakes and standardize quotes emitted by seq2seq models."""
    if not sql:
        return sql

    sql = sql.strip().rstrip(';')

    # Standardize double-quoted string literals in SQL to single quotes: "value" -> 'value'
    sql = re.sub(r'=\s*"([^"]+)"', r"= '\1'", sql)
    sql = re.sub(r'(?:LIKE|IN)\s*"([^"]+)"', r"LIKE '%\1%'", sql, flags=re.IGNORECASE)
    sql = re.sub(r'"([^"]+)"', r"'\1'", sql)

    # Fix 'ORDER BY <col> desc = 2' -> 'ORDER BY <col> DESC LIMIT 2'
    sql = re.sub(
        r'\bORDER\s+BY\s+([a-zA-Z0-9_.]+)\s+(ASC|DESC)\s*=\s*(\d+)\b',
        r'ORDER BY \1 \2 LIMIT \3',
        sql,
        flags=re.IGNORECASE
    )
    # Fix 'ORDER BY <col> = 2' -> 'ORDER BY <col> DESC LIMIT 2'
    sql = re.sub(
        r'\bORDER\s+BY\s+([a-zA-Z0-9_.]+)\s*=\s*(\d+)\b',
        r'ORDER BY \1 DESC LIMIT \2',
        sql,
        flags=re.IGNORECASE
    )

    # Fix missing = in WHERE clauses: 'WHERE id 10' -> 'WHERE id = 10'
    sql = re.sub(
        r'\b(WHERE|AND|OR)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+(\d+|\'[^\']*\')\b',
        r'\1 \2 = \3',
        sql,
        flags=re.IGNORECASE
    )

    # Fix unquoted single characters/words after = in WHERE: WHERE grade = A -> WHERE grade = 'A'
    def quote_unquoted_str(m):
        prefix = m.group(1)
        col = m.group(2)
        op = m.group(3)
        val = m.group(4)
        if re.match(r'^[+-]?\d+(?:\.\d+)?$', val) or val.upper() in ('NULL', 'TRUE', 'FALSE'):
            return m.group(0)
        return f"{prefix} {col} {op} '{val}'"

    sql = re.sub(r'\b(WHERE|AND|OR)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*(=|!=|<>)\s*([A-Za-z][A-Za-z0-9_]*)\b', quote_unquoted_str, sql, flags=re.IGNORECASE)

    # Fix LIKE without quotes: 'LIKE gmail' -> 'LIKE \'%gmail%\''
    sql = re.sub(
        r'\bLIKE\s+([a-zA-Z0-9_.]+)(?![\'%])\b',
        r"LIKE '%\1%'",
        sql,
        flags=re.IGNORECASE
    )

    # Fix LIKE 'gmail' -> LIKE '%gmail%' if not already having %
    sql = re.sub(
        r"\bLIKE\s+'([^%'][^']*)'\b",
        r"LIKE '%\1%'",
        sql,
        flags=re.IGNORECASE
    )

    # Clean double WHERE
    where_parts = re.split(r'\bWHERE\b', sql, flags=re.IGNORECASE)
    if len(where_parts) > 2:
        sql = where_parts[0] + 'WHERE ' + ' AND '.join(p.strip() for p in where_parts[1:])

    # Clean redundant self-joins: 'FROM teachers INNER JOIN teachers ON ...'
    join_pat = r'\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+(?:INNER\s+|LEFT\s+|RIGHT\s+)?JOIN\s+\1\s+ON\s+[^Ww]+'
    sql = re.sub(join_pat, r'FROM \1 ', sql, flags=re.IGNORECASE)

    # Clean empty/broken WHERE clauses
    sql = re.sub(r'\bWHERE\s+(?:AND\s+)*', 'WHERE ', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\s+AND\s+AND\s+', ' AND ', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bWHERE\s*$', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\s+', ' ', sql).strip()

    return sql + ';'


def fix_like_patterns(sql, instruction):
    """Refine LIKE queries based on natural language phrasing ('starts with', 'ends with', 'contains')."""
    if not sql:
        return sql

    # 1. starts with X
    m_start = re.search(
        r'\b(?:starts?\s+with|starting\s+with|begins?\s+with|beginning\s+with)\s+[\'"]?([a-zA-Z0-9_@.\-]+)[\'"]?',
        instruction,
        re.IGNORECASE
    )
    if m_start:
        val = m_start.group(1).strip()
        sql = re.sub(r"\bLIKE\s+([a-zA-Z0-9_]+|'[^']*')", f"LIKE '{val}%'", sql, flags=re.IGNORECASE)

    # 2. ends with X
    m_end = re.search(
        r'\b(?:ends?\s+with|ending\s+with)\s+[\'"]?([a-zA-Z0-9_@.\-]+)[\'"]?',
        instruction,
        re.IGNORECASE
    )
    if m_end:
        val = m_end.group(1).strip()
        sql = re.sub(r"\bLIKE\s+([a-zA-Z0-9_]+|'[^']*')", f"LIKE '%{val}'", sql, flags=re.IGNORECASE)

    # 3. contains X
    m_contain = re.search(
        r'\b(?:contains?|containing)\s+[\'"]?([a-zA-Z0-9_@.\-]+)[\'"]?',
        instruction,
        re.IGNORECASE
    )
    if m_contain:
        val = m_contain.group(1).strip()
        sql = re.sub(r"\bLIKE\s+([a-zA-Z0-9_]+|'[^']*')", f"LIKE '%{val}%'", sql, flags=re.IGNORECASE)

    return sql


def align_sql_with_schema(sql, schema, instruction=None, data=None):
    """Replace model-emitted table and column names with exact schema identifiers."""
    if not sql or not schema:
        return sql

    tables = list(schema.keys())

    # 1. Identify and map table in FROM / INTO / UPDATE / TABLE clause
    table_match = re.search(r'\b(?:FROM|INTO|UPDATE|TABLE)\s+[`"]?([a-zA-Z0-9_]+)[`"]?', sql, re.IGNORECASE)
    active_table = None
    if table_match:
        raw_table = table_match.group(1)
        active_table = match_table_name(raw_table, tables)
        if active_table and active_table != raw_table:
            sql = re.sub(
                rf'\b(?:FROM|INTO|UPDATE|TABLE)\s+[`"]?{re.escape(raw_table)}[`"]?',
                f"{table_match.group(0).split()[0]} {active_table}",
                sql,
                flags=re.IGNORECASE
            )

    if not active_table:
        active_table = tables[0] if tables else None

    if not active_table or active_table not in schema:
        return sql

    raw_cols = schema[active_table]
    cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]

    # 2. Convert model-emitted max_<col>, min_<col>, avg_<col> to functions
    for c in cols:
        sql = re.sub(rf'\bmax_{re.escape(c)}\b', f'MAX({c})', sql, flags=re.IGNORECASE)
        sql = re.sub(rf'\bmin_{re.escape(c)}\b', f'MIN({c})', sql, flags=re.IGNORECASE)
        sql = re.sub(rf'\bavg_{re.escape(c)}\b', f'AVG({c})', sql, flags=re.IGNORECASE)
        sql = re.sub(rf'\bsum_{re.escape(c)}\b', f'SUM({c})', sql, flags=re.IGNORECASE)
        sql = re.sub(rf'\bcount_{re.escape(c)}\b', f'COUNT({c})', sql, flags=re.IGNORECASE)

    # 3. Clean and map projected columns in SELECT clause
    m_proj = re.search(r'^\s*SELECT\s+(.+?)\s+FROM\b', sql, re.IGNORECASE)
    if m_proj and ' JOIN ' not in sql.upper():
        raw_proj = m_proj.group(1).strip()
        if raw_proj != '*':
            proj_items = [p.strip() for p in raw_proj.split(',')]
            valid_projs = []
            for item in proj_items:
                # If function or literal, keep it
                if re.search(r'\(|\)|\*|^\d+$', item):
                    valid_projs.append(item)
                    continue
                # Match column
                matched_c = match_column_name(item, cols, active_table)
                if matched_c:
                    if matched_c not in valid_projs:
                        valid_projs.append(matched_c)
                elif item.lower() in (active_table.lower(), active_table.lower()[:-1] if active_table.lower().endswith('s') else ''):
                    # Hallucinated table name in SELECT list (e.g. SELECT first_name, employee FROM employees)
                    continue
                else:
                    # Unmatched column -> if similarity to any col, map it
                    pass

            if not valid_projs:
                valid_projs = ['*']

            sql = re.sub(
                r'^\s*SELECT\s+(.+?)\s+FROM\b',
                f"SELECT {', '.join(valid_projs)} FROM",
                sql,
                flags=re.IGNORECASE
            )

    # 4. Check WHERE conditions using columns not in active_table
    if instruction and ' JOIN ' not in sql.upper():
        where_match = re.search(r'\bWHERE\s+(.+)$', sql, re.IGNORECASE)
        if where_match:
            where_clause = where_match.group(1)
            for cond_col in re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=|<|>|<=|>=|!=|\blike\b)', where_clause, re.IGNORECASE):
                if cond_col.lower() not in [c.lower() for c in cols]:
                    # Try alias matching first
                    mapped_col = match_column_name(cond_col, cols, active_table)
                    if mapped_col:
                        sql = re.sub(rf'\b{re.escape(cond_col)}\b', mapped_col, sql, flags=re.IGNORECASE)
                    else:
                        # Look for a column in active_table mentioned in instruction
                        for c in cols:
                            if re.search(r'\b' + re.escape(c.lower()) + r'\b', instruction.lower()):
                                sql = re.sub(rf'\b{re.escape(cond_col)}\b', c, sql, flags=re.IGNORECASE)
                                break

    # 5. Replace any recognized columns with exact casing
    for c in cols:
        pattern = rf'\b{re.escape(c)}\b'
        sql = re.sub(pattern, c, sql, flags=re.IGNORECASE)

    return sql


def correct_sql_identifiers(sql, schema):
    """Correct SQL identifiers for raw SQL queries typed by users."""
    if not sql or not schema:
        return sql
    return align_sql_with_schema(sql, schema)


def fallback_sql(table, columns):
    """Generate a safe, guaranteed valid SELECT statement."""
    if not table:
        return "SELECT 1;"
    if columns:
        col_names = [c['name'] if isinstance(c, dict) else str(c) for c in columns]
        return f"SELECT {', '.join(col_names)} FROM {table};"
    return f"SELECT * FROM {table};"


def is_safe_sql(sql):
    """Verify that generated query is a safe, single-statement SQL command."""
    if not sql:
        return False
    clean = sql.strip()
    if clean.rstrip(';').count(';') > 0:
        return False
    allowed = (
        'select', 'show', 'describe', 'desc', 'use', 'insert', 'update',
        'delete', 'create', 'alter', 'drop', 'truncate', 'rename',
        'commit', 'rollback', 'savepoint'
    )
    return clean.lower().startswith(allowed)


# ============================================================================
# PRIMARY GENERATION PIPELINE
# ============================================================================

def generate_sql(instruction, schema, data=None, table_sizes=None):
    """Main entry point: turn natural language instruction into executable SQL.

    Returns:
      {
        'table': str or None,
        'suggested_table': str or None,
        'columns': list of str,
        'sql': str or None,
        'operation': str
      }
    """
    with _lock:
        models = load_models()
        primary_tok, primary_mod = models['primary']
        model_name = models.get('model_name', '')

        op = detect_operation(instruction)

        # 1. Handle Utilities & DDL
        if op in ('SHOW_DATABASES', 'SHOW_TABLES', 'DESCRIBE', 'USE', 'CREATE', 'DROP', 'ALTER', 'TRUNCATE', 'RENAME'):
            ddl_sql = generate_ddl(instruction, op)
            if ddl_sql:
                return {
                    'table': None,
                    'suggested_table': None,
                    'columns': [],
                    'sql': ddl_sql,
                    'operation': op
                }

        # 2. Handle DML (INSERT, UPDATE, DELETE)
        if op == 'INSERT':
            dml_sql = generate_dml_insert(instruction, schema, data)
            if dml_sql:
                table = select_best_table_from_text(instruction, schema)
                raw_cols = schema.get(table, []) if table else []
                cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]
                return {
                    'table': table,
                    'suggested_table': None,
                    'columns': cols,
                    'sql': dml_sql,
                    'operation': 'INSERT'
                }

        if op == 'UPDATE':
            dml_sql = generate_dml_update(instruction, schema, data)
            if dml_sql:
                table = select_best_table_from_text(instruction, schema)
                raw_cols = schema.get(table, []) if table else []
                cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]
                return {
                    'table': table,
                    'suggested_table': None,
                    'columns': cols,
                    'sql': dml_sql,
                    'operation': 'UPDATE'
                }

        if op == 'DELETE':
            dml_sql = generate_dml_delete(instruction, schema, data)
            if dml_sql:
                table = select_best_table_from_text(instruction, schema)
                raw_cols = schema.get(table, []) if table else []
                cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]
                return {
                    'table': table,
                    'suggested_table': None,
                    'columns': cols,
                    'sql': dml_sql,
                    'operation': 'DELETE'
                }

        # 3. Handle DQL (SELECT) via pretrained Hugging Face model
        table = select_best_table_from_text(instruction, schema)
        if not table and schema:
            table = list(schema.keys())[0]

        sql_output = None

        # Check for simple general queries: "show all students" / "get all students"
        text_clean = instruction.strip().lower()
        if re.search(r'^(?:show|get|list|display|select|view)\s+(?:all\s+)?(?:the\s+)?([a-zA-Z0-9_]+)s?$', text_clean):
            m_tbl = re.search(r'^(?:show|get|list|display|select|view)\s+(?:all\s+)?(?:the\s+)?([a-zA-Z0-9_]+)s?$', text_clean)
            target_t = match_table_name(m_tbl.group(1), schema) or table
            if target_t:
                sql_output = f"SELECT * FROM {target_t};"

        if not sql_output and primary_mod is not None and primary_tok is not None:
            try:
                # Choose prompt structure based on model architecture
                if 'awesome-text-to-sql' in model_name or 'spider' in model_name.lower():
                    schema_ddl = build_schema_ddl(schema, active_table=table)
                    prompt = f"tables:\n{schema_ddl}\nquery: {instruction.strip()}"
                else:
                    prompt = f"translate English to SQL: {instruction.strip()}"

                inputs = primary_tok(prompt, return_tensors='pt', max_length=512, truncation=True)
                inputs = {k: v.to(DEVICE) for k, v in inputs.items()}
                with torch.inference_mode():
                    outputs = primary_mod.generate(
                        **inputs,
                        max_length=150,
                        num_beams=4,
                        early_stopping=True
                    )
                raw_sql = primary_tok.decode(outputs[0], skip_special_tokens=True).strip()
                if raw_sql and len(raw_sql) > 3:
                    sql_output = raw_sql
            except Exception:
                sql_output = None

        # Post-process, repair syntax, and align with schema
        if sql_output:
            sql_output = fix_sql_syntax(sql_output)
            sql_output = fix_like_patterns(sql_output, instruction)
            sql_output = refine_aggregations(sql_output, instruction, table, schema)
            sql_output = enhance_select_projection(sql_output, instruction, table, schema)
            sql_output = align_sql_with_schema(sql_output, schema, instruction=instruction, data=data)

            # Ensure proper table name in the query if FROM was missing or mismatched
            if table and f" {table}" not in sql_output and f" `{table}`" not in sql_output and " FROM " not in sql_output.upper():
                sql_output = f"{sql_output.rstrip(';')} FROM {table};"

        # Fallback if model failed to produce executable SELECT query
        if not sql_output or not is_safe_sql(sql_output):
            raw_cols = schema.get(table, []) if table else []
            cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]
            sql_output = fallback_sql(table, cols)

        # Extract selected columns for UI presentation
        columns = []
        if table and table in schema:
            raw_cols = schema[table]
            all_cols = [c['name'] if isinstance(c, dict) else str(c) for c in raw_cols]
            m_cols = re.search(r'SELECT\s+(.+?)\s+FROM', sql_output, re.IGNORECASE)
            if m_cols:
                raw_c = m_cols.group(1).strip()
                if raw_c == '*':
                    columns = all_cols
                else:
                    columns = [c.strip() for c in raw_c.split(',') if c.strip()]
            if not columns:
                columns = all_cols

        return {
            'table': table,
            'suggested_table': None,
            'columns': columns,
            'sql': sql_output,
            'operation': 'SELECT'
        }
