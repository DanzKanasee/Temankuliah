import importlib
import json
import os
import sqlite3
import sys
import uuid

import pytest

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


def test_postgres_cursor_returns_inserted_id():
    class DummyCursor:
        description = None

        def execute(self, sql, parameters):
            self.sql = sql
            self.parameters = parameters

        def fetchone(self):
            return (42,)

    inner = DummyCursor()
    cursor = PostgresCursor(inner)

    cursor.execute("INSERT INTO schedules (user_id) VALUES (?)", (7,))

    assert inner.sql == "INSERT INTO schedules (user_id) VALUES (%s) RETURNING id"
    assert cursor.lastrowid == 42


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="requires a dedicated TEST_POSTGRES_URL schema")
def test_postgres_schedule_insert_returns_id_and_persists_child_rows():
    import psycopg

    test_url = os.environ["TEST_POSTGRES_URL"]
    marker = uuid.uuid4().hex
    conn = psycopg.connect(test_url)
    try:
        cursor = PostgresCursor(conn.cursor())
        cursor.execute(
            "INSERT INTO public.users (name, email) VALUES (?, ?)",
            ("Schedule integration test", f"schedule-test-{marker}@example.invalid"),
        )
        user_id = cursor.lastrowid
        assert isinstance(user_id, int)

        cursor.execute(
            "INSERT INTO public.schedules (user_id, filename, schedule_text) VALUES (?, ?, ?)",
            (user_id, f"test-{marker}.pdf", "Integration test schedule"),
        )
        schedule_id = cursor.lastrowid
        assert isinstance(schedule_id, int)
        cursor.executemany(
            "INSERT INTO public.schedule_entries (schedule_id, day_name, start_time, end_time, course_name) VALUES (?, ?, ?, ?, ?)",
            [(schedule_id, "RABU", "07:30", "09:10", "Test Algoritma")],
        )

        stored = conn.execute(
            "SELECT s.id, COUNT(e.id) FROM public.schedules s "
            "JOIN public.schedule_entries e ON e.schedule_id = s.id "
            "WHERE s.id = %s GROUP BY s.id",
            (schedule_id,),
        ).fetchone()
        assert stored == (schedule_id, 1)
    finally:
        conn.rollback()
        conn.close()


def test_scanned_pdf_upload_persists_schedule_and_confirms_success(caplog, monkeypatch, tmp_path):
    import logging
    from pathlib import Path

    from pypdf import PdfReader

    import app.database.db as db_module
    import app.main as main_module
    import app.utils.gemini_helper as gemini_helper

    fixture = Path(__file__).parent / "fixtures" / "scanned_schedule.pdf"
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(fixture).pages)
    assert not pdf_text.strip(), "fixture must be image-only"

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "schedule-test.db"))
    monkeypatch.setattr(db_module, "supabase_database_url", lambda: None)
    monkeypatch.setattr(main_module, "COOKIE_SECRET", b"schedule-upload-test-secret")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(main_module, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(
        main_module,
        "extract_text_from_file",
        lambda *_: pytest.fail("PDF upload must use the direct media extraction path"),
    )

    schedule_rows = [
        {"day": "RABU", "start": "07:30", "end": "09:10", "course": "Algoritma dan Pemrograman", "room": "A101"},
        {"day": "KAMIS", "start": "09:20", "end": "11:00", "course": "Basis Data", "room": "B203"},
    ]

    class FakeModels:
        calls = 0

        def generate_content(self, *, model, contents, config):
            self.calls += 1
            media = contents[0].inline_data
            assert media.mime_type == "application/pdf"
            assert len(media.data) > 1000
            return type("Response", (), {"text": json.dumps(schedule_rows)})()

    fake_models = FakeModels()
    monkeypatch.setattr(gemini_helper, "client", type("FakeClient", (), {"models": fake_models})())

    db_module.init_db()
    conn = db_module.get_db()
    cursor = conn.execute(
        "INSERT INTO users (name, email) VALUES (?, ?)",
        ("Schedule upload test", "schedule-upload-test@example.invalid"),
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()

    caplog.set_level(logging.INFO, logger="app.main")
    with TestClient(main_module.app) as client:
        assert "app_startup build_id=" in caplog.text
        unauthenticated = client.post(
            "/schedules/upload",
            files={"file": (fixture.name, fixture.read_bytes(), "application/pdf")},
            follow_redirects=False,
        )
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/"
        assert "schedule_upload_commit_confirmed" not in caplog.text

        response = client.post(
            "/schedules/upload",
            files={"file": (fixture.name, fixture.read_bytes(), "application/pdf")},
            cookies={"user_session": main_module.session_value(user_id)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/schedules"
    assert fake_models.calls == 1
    assert "schedule_upload_commit_confirmed" in caplog.text

    conn = db_module.get_db()
    schedule = conn.execute(
        "SELECT id FROM schedules WHERE user_id = ? AND filename = ?", (user_id, fixture.name)
    ).fetchone()
    assert schedule is not None
    schedule_id = schedule["id"]
    assert isinstance(schedule_id, int)
    stored_entries = conn.execute(
        "SELECT day_name, start_time, end_time, course_name FROM schedule_entries WHERE schedule_id = ? ORDER BY day_name",
        (schedule_id,),
    ).fetchall()
    conn.close()
    assert [tuple(row) for row in stored_entries] == [
        ("KAMIS", "09:20", "11:00", "Basis Data"),
        ("RABU", "07:30", "09:10", "Algoritma dan Pemrograman"),
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
    assert gemini_helper.get_model_name() in {"gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"}


def test_all_gemini_text_and_media_paths_share_two_model_attempt_limit(monkeypatch):
    import app.utils.gemini_helper as gemini_helper

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")

    assert gemini_helper.model_candidates() == ["gemini-3.6-flash", "gemini-3.8-flash"]


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
