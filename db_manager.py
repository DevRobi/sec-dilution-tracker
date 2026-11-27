import sqlite3
import pandas as pd
import json
from datetime import datetime

DB_PATH = "tracker.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Table for company info
    c.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            cik TEXT PRIMARY KEY,
            ticker TEXT,
            title TEXT
        )
    ''')
    
    # Table for share statistics
    # Dynamically build the create table statement
    create_stats_sql = '''
        CREATE TABLE IF NOT EXISTS stats (
            cik TEXT PRIMARY KEY,
            latest_shares INTEGER,
            prev_shares_1y INTEGER,
            change_pct_1q REAL,
            date_1q TEXT,
            last_date TEXT,
            last_updated TEXT,
            FOREIGN KEY(cik) REFERENCES companies(cik)
        )
    '''
    # We will add columns dynamically if they don't exist
    c.execute(create_stats_sql)
    
    # Define all year columns we need
    required_cols = []
    # 1 to 10 years
    for y in range(1, 11):
        required_cols.append(f"change_pct_{y}y")
        required_cols.append(f"date_{y}y")
        
    c.execute("PRAGMA table_info(stats)")
    existing_cols = [info[1] for info in c.fetchall()]
    
    for col in required_cols:
        if col not in existing_cols:
            try:
                c.execute(f"ALTER TABLE stats ADD COLUMN {col} TEXT") # Using TEXT for dates, REAL for pct (sqlite is loose)
                # Actually, standardizing: REAL for change_pct, TEXT for dates.
                # Since I used TEXT in the ALTER above for everything, let's fix it for next time or just use standard sqlite affinities
                # SQLite column types in ALTER are hints.
            except sqlite3.OperationalError:
                pass 
    
    # Table for historical data points (for charting)
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cik TEXT,
            date TEXT,
            shares INTEGER,
            source TEXT,
            FOREIGN KEY(cik) REFERENCES companies(cik)
        )
    ''')

    # Table for financial metrics (SBC, Buybacks, etc.)
    c.execute('''
        CREATE TABLE IF NOT EXISTS financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cik TEXT,
            date TEXT,
            metric TEXT,
            value REAL,
            FOREIGN KEY(cik) REFERENCES companies(cik),
            UNIQUE(cik, date, metric)
        )
    ''')

    # Table for stock prices (daily/weekly)
    c.execute('''
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cik TEXT,
            date TEXT,
            price REAL,
            FOREIGN KEY(cik) REFERENCES companies(cik),
            UNIQUE(cik, date)
        )
    ''')
    
    # Table for listings of Convertible Notes, ATM, Shelf Offerings (Text)
    c.execute('''
        CREATE TABLE IF NOT EXISTS offerings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cik TEXT,
            date TEXT,
            category TEXT,
            text TEXT,
            source_filing TEXT,
            amount REAL,
            amount_text TEXT,
            FOREIGN KEY(cik) REFERENCES companies(cik),
            UNIQUE(cik, date, category, source_filing) 
        )
    ''')
    
    # Migration: Add amount columns to existing offerings table if they don't exist
    c.execute("PRAGMA table_info(offerings)")
    offerings_cols = [info[1] for info in c.fetchall()]
    
    if 'amount' not in offerings_cols:
        try:
            c.execute("ALTER TABLE offerings ADD COLUMN amount REAL")
        except sqlite3.OperationalError:
            pass
            
    if 'amount_text' not in offerings_cols:
        try:
            c.execute("ALTER TABLE offerings ADD COLUMN amount_text TEXT")
        except sqlite3.OperationalError:
            pass
    
    conn.commit()
    
    # Table for persistent user storage
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            password_hash TEXT,
            full_name TEXT,
            created_at TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

import hashlib

def hash_password(password):
    """Simple SHA-256 hashing for demo purposes."""
    return hashlib.sha256(password.encode()).hexdigest()

def add_user(username, email, password, full_name=""):
    """Registers a new user."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        pw_hash = hash_password(password)
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('''
            INSERT INTO users (username, email, password_hash, full_name, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (username, email, pw_hash, full_name, created_at))
        conn.commit()
        return True, "User registered successfully."
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed: users.username" in str(e):
            return False, "Username already exists."
        elif "UNIQUE constraint failed: users.email" in str(e):
            return False, "Email already registered."
        return False, f"Registration failed: {e}"
    finally:
        conn.close()

def authenticate_user(username_or_email, password):
    """Verifies user credentials."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pw_hash = hash_password(password)
    # Check both username and email
    c.execute('''
        SELECT username, full_name FROM users 
        WHERE (username = ? OR email = ?) AND password_hash = ?
    ''', (username_or_email, username_or_email, pw_hash))
    user = c.fetchone()
    conn.close()
    if user:
        return True, {"username": user[0], "full_name": user[1]}
    return False, None

def save_offerings(cik, offerings_df):
    """
    Saves text snippets for offerings (ATM, Convertible, Shelf).
    offerings_df columns: date, category, text, source_filing, amount (optional), amount_text (optional)
    """
    if offerings_df is None or offerings_df.empty:
        return

    conn = sqlite3.connect(DB_PATH)
    data = []
    for _, row in offerings_df.iterrows():
        date_str = row['date'].strftime('%Y-%m-%d') if pd.notnull(row['date']) else None
        if date_str:
            amount = row.get('amount', None)
            amount_text = row.get('amount_text', None)
            data.append((cik, date_str, row['category'], row['text'], row['source_filing'], amount, amount_text))
            
    # Use REPLACE to update if parsing logic improves later
    conn.executemany('''
        INSERT OR REPLACE INTO offerings (cik, date, category, text, source_filing, amount, amount_text)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', data)
    
    conn.commit()
    conn.close()

def get_offerings(cik):
    conn = sqlite3.connect(DB_PATH)
    query = 'SELECT * FROM offerings WHERE cik = ? ORDER BY date DESC'
    df = pd.read_sql_query(query, conn, params=(cik,))
    conn.close()
    return df

def save_companies(companies_list):
    """
    companies_list: list of dicts {'cik_str': 123, 'ticker': 'AAPL', 'title': 'Apple Inc'}
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    data = []
    for comp in companies_list:
        # SEC provides CIK as int, we want 10-digit string often, but for DB simple string is fine
        # We pad CIK to 10 digits for API calls later
        cik_clean = str(comp['cik_str']).zfill(10)
        data.append((cik_clean, comp['ticker'], comp['title']))
        
    c.executemany('''
        INSERT OR REPLACE INTO companies (cik, ticker, title)
        VALUES (?, ?, ?)
    ''', data)
    
    conn.commit()
    conn.close()

def save_stats(cik, stats_dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Prepare columns and placeholders dynamically
    cols = ['cik', 'latest_shares', 'prev_shares_1y', 'change_pct_1q', 'date_1q', 'last_date', 'last_updated']
    
    # Add year columns 1-10
    for y in range(1, 11):
        cols.append(f'change_pct_{y}y')
        cols.append(f'date_{y}y')
        
    placeholders = ', '.join(['?'] * len(cols))
    cols_str = ', '.join(cols)
    
    query = f'''
        INSERT OR REPLACE INTO stats ({cols_str}) VALUES ({placeholders})
    '''
    
    values = [
        cik,
        stats_dict.get('latest_shares'),
        stats_dict.get('prev_shares_1y'),
        stats_dict.get('change_pct_1q'),
        stats_dict.get('date_1q'),
        stats_dict.get('last_date'),
        datetime.now().isoformat()
    ]
    
    for y in range(1, 11):
        values.append(stats_dict.get(f'change_pct_{y}y'))
        values.append(stats_dict.get(f'date_{y}y'))
        
    c.execute(query, values)
    
    conn.commit()
    conn.close()

def save_history(cik, history_df):
    conn = sqlite3.connect(DB_PATH)
    # Clear old history for this CIK to avoid duplicates/complex merges for now
    conn.execute("DELETE FROM history WHERE cik = ?", (cik,))
    
    # Prepare data
    data = []
    for _, row in history_df.iterrows():
        # Convert Timestamp to string for sqlite
        date_str = row['end'].strftime('%Y-%m-%d') if pd.notnull(row['end']) else None
        data.append((cik, date_str, row['val'], row.get('form', 'Unknown')))
        
    conn.executemany('''
        INSERT INTO history (cik, date, shares, source)
        VALUES (?, ?, ?, ?)
    ''', data)
    
    conn.commit()
    conn.close()

def get_all_stats():
    conn = sqlite3.connect(DB_PATH)
    query = '''
        SELECT 
            c.ticker, c.title, s.* 
        FROM stats s
        JOIN companies c ON s.cik = c.cik
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def get_history(cik):
    conn = sqlite3.connect(DB_PATH)
    query = 'SELECT * FROM history WHERE cik = ? ORDER BY date ASC'
    df = pd.read_sql_query(query, conn, params=(cik,))
    conn.close()
    return df

def get_company_info(ticker):
    conn = sqlite3.connect(DB_PATH)
    query = 'SELECT * FROM companies WHERE ticker = ?'
    df = pd.read_sql_query(query, conn, params=(ticker,))
    conn.close()
    if not df.empty:
        return df.iloc[0]
    return None

def save_financials(cik, financials_df):
    """
    Saves financial metrics (SBC, Buybacks, etc.)
    financials_df columns: date, metric, value
    """
    if financials_df is None or financials_df.empty:
        return

    conn = sqlite3.connect(DB_PATH)
    data = []
    for _, row in financials_df.iterrows():
        date_str = row['date'].strftime('%Y-%m-%d') if pd.notnull(row['date']) else None
        if date_str:
            data.append((cik, date_str, row['metric'], row['value']))
            
    conn.executemany('''
        INSERT OR REPLACE INTO financials (cik, date, metric, value)
        VALUES (?, ?, ?, ?)
    ''', data)
    
    conn.commit()
    conn.close()

def get_financials(cik):
    conn = sqlite3.connect(DB_PATH)
    query = 'SELECT * FROM financials WHERE cik = ? ORDER BY date ASC'
    df = pd.read_sql_query(query, conn, params=(cik,))
    conn.close()
    return df

def save_prices(cik, price_df):
    """
    Saves historical stock prices.
    price_df columns: date, price
    """
    if price_df is None or price_df.empty:
        return

    conn = sqlite3.connect(DB_PATH)
    data = []
    for _, row in price_df.iterrows():
        date_str = row['date'].strftime('%Y-%m-%d') if pd.notnull(row['date']) else None
        if date_str:
            data.append((cik, date_str, row['price']))
            
    conn.executemany('''
        INSERT OR REPLACE INTO prices (cik, date, price)
        VALUES (?, ?, ?)
    ''', data)
    
    conn.commit()
    conn.close()def get_prices(cik):
    conn = sqlite3.connect(DB_PATH)
    query = 'SELECT * FROM prices WHERE cik = ? ORDER BY date ASC'
    df = pd.read_sql_query(query, conn, params=(cik,))
    conn.close()
    return df