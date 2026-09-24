import importlib
import sqlite3
import sys

from fastapi.testclient import TestClient

from app.database.db import PostgresCursor


def test_db_reads_project_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SUPABASE_DB_URL=postgresql://user:pass@db.example.supabase.co:5432/postgres?sslmode=require\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    import app.database.db as db_module
    importlib.reload(db_module)

    assert db_module.supabase_database_url() == (
        "postgresql://user:pass@db.example.supabase.co:5432/postgres?sslmode=require"
    )


def test_postgres_cursor_supports_executemany():
    class DummyCursor:
        def __init__(self):
            self.calls = []

        def executemany(self, sql, parameters):
            self.calls.append((sql, parameters))

        @property
        def description(self):
            return None

    inner = DummyCursor()
    cursor = PostgresCursor(inner)

    cursor.executemany(
        "INSERT INTO schedule_entries (schedule_id, day_name, course_name) VALUES (?, ?, ?)",
        [(1, "SENIN", "Algoritma"), (1, "SELASA", "Basis Data")],
    )

    assert inner.calls == [
        (
            "INSERT INTO schedule_entries (schedule_id, day_name, course_name) VALUES (%s, %s, %s)",
            [(1, "SENIN", "Algoritma"), (1, "SELASA", "Basis Data")],
        )
    ]


def test_postgres_cursor_exposes_rowcount(monkeypatch):
    class DummyCursor:
        def __init__(self):
            self.rowcount = 7

    cursor = PostgresCursor(DummyCursor())

    assert cursor.rowcount == 7


def test_register_allows_missing_telegram_id(monkeypatch, tmp_path):
    import app.database.db as db_module
    import app.main as main_module

    monkeypatch.setattr(db_module, "load_project_env", lambda: None)
    monkeypatch.setenv("SUPABASE_DB_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "app_mahasiswa.db"))
    monkeypatch.setattr(main_module, "COOKIE_SECRET", b"test-secret")

    db_module.init_db()
    client = TestClient(main_module.app)

    response = client.post(
        "/register",
        data={
            "name": "Ayu",
            "email": "ayu@example.com",
            "institution": "Universitas X",
            "student_id": "20231234",
            "major": "Teknik Informatika",
            "telegram_chat_id": "",
            "password": "password123",
            "password_confirmation": "password123",
        },
    )

    assert response.status_code == 200
    assert response.url.path == "/dashboard"


def test_get_db_falls_back_to_sqlite_when_supabase_is_unavailable(monkeypatch, tmp_path):
    import app.database.db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "fallback.db"))
    monkeypatch.setattr(db_module, "supabase_database_url", lambda: "postgresql://bad:pw@localhost:5432/db")

    class FakePsycopgModule:
        @staticmethod
        def connect(*args, **kwargs):
            raise RuntimeError("bad password")

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopgModule)

    conn = db_module.get_db()
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_parse_schedule_entries_accepts_raw_pdf_text():
    import app.main as main_module

    text = """SENIN
07.30-09.10 Pemrograman Web | B-203 | Dr. Andi
SELASA
09.00-10.30 Basis Data | LAB 1 | Siti A.
"""

    entries = main_module.parse_schedule_entries(text)

    assert entries[0]["day"] == "SENIN"
    assert entries[0]["course"] == "Pemrograman Web"
    assert entries[1]["day"] == "SELASA"


def test_parse_schedule_entries_handles_day_and_time_on_separate_lines():
    import app.main as main_module

    text = """SENIN
07.30-09.10
Pemrograman Web | B-203 | Dr. Andi
SELASA
09.00-10.30
Basis Data | LAB 1 | Siti A.
"""

    entries = main_module.parse_schedule_entries(text)

    assert entries[0]["day"] == "SENIN"
    assert entries[0]["course"] == "Pemrograman Web"
    assert entries[1]["day"] == "SELASA"


def test_extract_schedule_entries_keeps_raw_text_when_gemini_is_unavailable(monkeypatch):
    import app.utils.gemini_helper as gemini_helper

    monkeypatch.setattr(gemini_helper, "client", None)

    text = """SENIN
07.30-09.10 Pemrograman Web | B-203 | Dr. Andi
SELASA
09.00-10.30 Basis Data | LAB 1 | Siti A.
"""

    assert gemini_helper.extract_schedule_entries(text) == text


def test_default_gemini_model_uses_supported_flash_variant(monkeypatch):
    import app.utils.gemini_helper as gemini_helper

    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    assert gemini_helper.get_model_name().startswith("gemini-")
    assert "flash" in gemini_helper.get_model_name().lower()
    assert gemini_helper.get_model_name() in {"gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"}


def test_extract_schedule_entries_returns_raw_text_when_gemini_fails(monkeypatch):
    import app.utils.gemini_helper as gemini_helper

    monkeypatch.setattr(gemini_helper, "client", object())
    monkeypatch.setattr(gemini_helper, "generate_text", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")))

    text = """SENIN
07.30-09.10 Pemrograman Web | B-203 | Dr. Andi
SELASA
09.00-10.30 Basis Data | LAB 1 | Siti A.
"""

    assert gemini_helper.extract_schedule_entries(text) == text


def test_send_due_reminders_only_sends_at_expected_deadline_windows(monkeypatch):
    import app.services.telegram_reminders as reminder_module

    deadline = reminder_module.datetime.now(reminder_module.TIMEZONE) + reminder_module.timedelta(days=1, minutes=1)
    row = {
        "id": 42,
        "tugas_name": "Tugas UTS",
        "deadline_date": deadline.strftime("%Y-%m-%d %H:%M"),
        "timezone": "Asia/Jakarta",
        "telegram_chat_id": "123456789",
    }

    class FakeCursor:
        def __init__(self):
            self.sent = []

        def execute(self, sql, params=()):
            self.sent.append((sql, params))
            if sql.startswith("SELECT d.id"):
                return self
            if sql.startswith("SELECT 1 FROM deadline_notifications"):
                return self
            return self

        def fetchall(self):
            return [row]

        def fetchone(self):
            return None

        def commit(self):
            return None

        def close(self):
            return None

    fake_conn = FakeCursor()
    monkeypatch.setattr(reminder_module, "get_db", lambda: fake_conn)
    monkeypatch.setattr(reminder_module, "send_telegram_message", lambda chat_id, message: True)

    reminder_module.send_due_reminders()

    assert any("INSERT INTO deadline_notifications" in sql for sql, _ in fake_conn.sent)
