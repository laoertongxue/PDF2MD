import sqlite3

from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema

LEGACY_TASKS_SQL = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  file_path TEXT NOT NULL,
  snapshot_path TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  model_tier TEXT NOT NULL DEFAULT 'stub',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  error_msg TEXT
);
INSERT INTO tasks VALUES (
  'legacy-task', '/input.pdf', '/snapshot.pdf', 'sha', 'FAILED', 'stub', 1, 1, NULL
);
"""


def test_apply_creates_batches_table(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "batches" in names
    conn.close()


def test_apply_adds_batch_id_column_to_tasks(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "batch_id" in cols
    conn.close()


def test_apply_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    apply_serve_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "batch_id" in cols
    conn.close()


def test_apply_creates_indexes(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_batch_status" in indexes
    assert "idx_task_batch" in indexes
    conn.close()


def test_init_db_upgrades_legacy_database_with_recovery_checkpoint(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(LEGACY_TASKS_SQL)
    legacy.commit()
    legacy.close()

    conn = init_db(str(db_path))

    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_recovery)").fetchall()}
    assert columns == {
        "task_id",
        "sectioning_complete",
        "expected_sections",
        "resume_owner",
        "resume_generation",
    }
    assert conn.execute(
        "SELECT sectioning_complete, expected_sections, resume_owner, resume_generation "
        "FROM task_recovery WHERE task_id = 'legacy-task'"
    ).fetchone() == (0, 0, None, 0)
    conn.close()


def test_apply_serve_schema_backfills_recovery_checkpoint_idempotently(tmp_path):
    db_path = tmp_path / "legacy-serve.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(LEGACY_TASKS_SQL)

    apply_serve_schema(conn)
    apply_serve_schema(conn)

    assert conn.execute(
        "SELECT sectioning_complete, expected_sections, resume_owner, resume_generation "
        "FROM task_recovery WHERE task_id = 'legacy-task'"
    ).fetchone() == (0, 0, None, 0)
    assert (
        conn.execute("SELECT COUNT(*) FROM task_recovery WHERE task_id = 'legacy-task'").fetchone()[
            0
        ]
        == 1
    )
    conn.close()
