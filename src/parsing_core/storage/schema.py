import sqlite3
import time

_WAL_RETRY_ATTEMPTS = 5
_WAL_RETRY_DELAY_SECONDS = 0.05

TASK_RECOVERY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS task_recovery (
  task_id              TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  sectioning_complete  INTEGER NOT NULL DEFAULT 0 CHECK(sectioning_complete IN (0, 1)),
  expected_sections    INTEGER NOT NULL DEFAULT 0 CHECK(expected_sections >= 0),
  resume_owner         TEXT,
  resume_generation    INTEGER NOT NULL DEFAULT 0 CHECK(resume_generation >= 0)
);
CREATE INDEX IF NOT EXISTS idx_task_recovery_owner ON task_recovery(resume_owner);
INSERT OR IGNORE INTO task_recovery (task_id)
SELECT id FROM tasks;
"""

SCHEMA_SQL = (
    """
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  file_path     TEXT NOT NULL,
  snapshot_path TEXT NOT NULL,
  file_sha256   TEXT NOT NULL,
  status        TEXT NOT NULL,
  model_tier    TEXT NOT NULL DEFAULT 'stub',
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  error_msg     TEXT
);
"""
    + TASK_RECOVERY_TABLE_SQL
    + """

CREATE TABLE IF NOT EXISTS sections (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  raw_md_path   TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  char_count    INTEGER NOT NULL,
  ai_status     TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  UNIQUE(task_id, seq)
);

CREATE TABLE IF NOT EXISTS ai_artifacts (
  id            TEXT PRIMARY KEY,
  section_id    TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
  ai_md_path    TEXT NOT NULL,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cost_usd      REAL,
  retry_count   INTEGER NOT NULL DEFAULT 0,
  model_name    TEXT,
  created_at    INTEGER NOT NULL,
  UNIQUE(section_id)
);

CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_section_task ON sections(task_id);
CREATE INDEX IF NOT EXISTS idx_sha_file ON tasks(file_sha256);
CREATE INDEX IF NOT EXISTS idx_sha_section ON sections(sha256);
"""
)


def _enable_write_ahead_logging(conn: sqlite3.Connection) -> None:
    for attempt in range(_WAL_RETRY_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if attempt + 1 == _WAL_RETRY_ATTEMPTS or str(error) != "locking protocol":
                raise
            time.sleep(_WAL_RETRY_DELAY_SECONDS * (attempt + 1))


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    _enable_write_ahead_logging(conn)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
