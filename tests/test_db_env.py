import importlib
import os


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
