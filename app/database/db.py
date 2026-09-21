import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "app_mahasiswa.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Tabel User
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            telegram_chat_id TEXT,
            email TEXT,
            institution TEXT,
            student_id TEXT,
            major TEXT,
            password_hash TEXT
        )
    ''')
    
    # Tabel Deadlines Tugas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deadlines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            tugas_name TEXT NOT NULL,
            deadline_date TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        )
    ''')

    # Tabel Riwayat Rangkuman & Hasil AI
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT NOT NULL,
            action_type TEXT NOT NULL,
            result_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Catatan pengiriman agar setiap pengingat hanya terkirim satu kali.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deadline_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deadline_id INTEGER NOT NULL,
            reminder_type TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(deadline_id, reminder_type)
        )
    ''')

    # Jadwal dipisahkan dari file unggahan sementara agar hasil pemrosesan tetap ada.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            schedule_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL,
            day_name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            course_name TEXT NOT NULL,
            room TEXT,
            lecturer TEXT,
            class_name TEXT
        )
    ''')

    # Migrate databases created by earlier versions that used `title`.
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    for column, definition in (
        ("email", "TEXT"),
        ("institution", "TEXT"),
        ("student_id", "TEXT"),
        ("major", "TEXT"),
        ("password_hash", "TEXT"),
    ):
        if column not in user_columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    # Old guest records do not have an email, while all new accounts must be unique.
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")

    deadline_columns = {row[1] for row in cursor.execute("PRAGMA table_info(deadlines)")}
    if "title" in deadline_columns and "tugas_name" not in deadline_columns:
        cursor.execute("ALTER TABLE deadlines RENAME COLUMN title TO tugas_name")
        deadline_columns.remove("title")
        deadline_columns.add("tugas_name")
    if "timezone" not in deadline_columns:
        cursor.execute("ALTER TABLE deadlines ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Jakarta'")
    
    conn.commit()
    conn.close()
