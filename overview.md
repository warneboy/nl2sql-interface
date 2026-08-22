# Project Overview

## What is it?
A web app that connects to a database and lets you run SQL queries from the browser. Also known as NL2SQL interface.

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

## API Routes
- `/login`, `/signup`, `/auth/google`, `/logout` - auth
- `/connect`, `/disconnect`, `/check-session` - connection
- `/execute` - run SQL queries
- `/get-databases`, `/get-tables`, `/get-schema`, `/get-tables-with-data` - explore data

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
