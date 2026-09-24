import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "app_mahasiswa.db")
DB_PATH = os.path.join("/tmp", "app_mahasiswa.db") if os.getenv("VERCEL") else PROJECT_DB_PATH


def load_project_env():
    """Prefer the working-directory .env, then fall back to the project root .env."""
    cwd_env = Path.cwd() / ".env"
    project_env = Path(__file__).resolve().parents[2] / ".env"

    if cwd_env.exists():
        load_dotenv(cwd_env, override=True)
        return

    if project_env.exists():
        load_dotenv(project_env, override=True)


class PostgresRow(dict):
    """Mapping row that also supports SQLite-style numeric indexes."""

    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self._lastrowid = None

    @staticmethod
    def _postgres_sql(sql):
        return sql.replace("?", "%s")

    def execute(self, sql, parameters=()):
        statement = self._postgres_sql(sql)
        if statement.lstrip().upper().startswith("INSERT") and "RETURNING" not in statement.upper():
            statement = f"{statement.rstrip().rstrip(';')} RETURNING id"
        self._cursor.execute(statement, tuple(parameters))
        self._lastrowid = None
        if "RETURNING ID" in statement.upper():
            row = self._cursor.fetchone()
            self._lastrowid = row[0] if row else None
        return self

    def executemany(self, sql, seq_of_parameters):
        statement = self._postgres_sql(sql)
        self._cursor.executemany(statement, [tuple(parameters) for parameters in seq_of_parameters])
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return self._wrap(row)

    def fetchall(self):
        return [self._wrap(row) for row in self._cursor.fetchall()]

    def _wrap(self, row):
        if row is None:
            return None
        return PostgresRow([column.name for column in self._cursor.description], row)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", 0)

    @property
    def lastrowid(self):
        return self._lastrowid


class PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return PostgresCursor(self._connection.cursor())

    def execute(self, sql, parameters=()):
        cursor = self.cursor()
        return cursor.execute(sql, parameters)

    def commit(self):
        self._connection.commit()

    def close(self):
        self._connection.close()


def supabase_database_url():
    load_project_env()
    return os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")


def get_db():
    database_url = supabase_database_url()
    if database_url:
        try:
            import psycopg
        except ImportError as exc:
            print("Supabase PostgreSQL unavailable: psycopg is not installed. Falling back to local SQLite.")
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            return conn
        try:
            connection = psycopg.connect(database_url, sslmode="require")
            return PostgresConnection(connection)
        except Exception as exc:
            print(f"Supabase PostgreSQL unavailable ({exc}). Falling back to local SQLite.")
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            return conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    if isinstance(conn, PostgresConnection):
        conn.close()
        return
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
