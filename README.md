# nl2sql-interface

A web app that connects to a database and lets you run SQL queries from the browser.

## Tech Stack
- **Backend**: Python + Flask
- **Databases**: MySQL and PostgreSQL
- **Frontend**: HTML, CSS, JavaScript
- **Auth**: JWT tokens, bcrypt passwords, Google login
- **User storage**: SQLite (`users.db`)

## Main Features
- **Login / Signup** - email + password, or Google account
- **Connect to database** - enter host, user, password, port
- **Run SQL queries** - type any query and see results
- **Browse databases** - list databases, tables, and schema
- **View sample data** - see first 5 rows of any table
- **Switch databases** - change active database with `USE`
- **Change password** - from user profile
- **Auto-reconnect** - reconnects if connection drops

## How to Run
```bash
pip install -r requirement.txt
python app.py
```

Then open: http://localhost:5001

## Notes
- Runs on port 5001 (auto-finds a free port if taken)
- Sessions last 24 hours
- Passwords are hashed before storing
- Model weights (`nl2sql_model/`) are excluded from this repo due to GitHub's 100 MB file limit
