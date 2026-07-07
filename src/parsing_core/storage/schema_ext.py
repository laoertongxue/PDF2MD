import sqlite3

BATCHES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS batches (
  id              TEXT PRIMARY KEY,
  status          TEXT NOT NULL,
  concurrency     INTEGER NOT NULL DEFAULT 4,
  policy          TEXT NOT NULL DEFAULT 'parallel',
  priority        INTEGER NOT NULL DEFAULT 0,
  total_tasks     INTEGER NOT NULL DEFAULT 0,
  completed_tasks INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,
  finished_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_batch_status ON batches(status);
"""

ALTER_TASKS_SQL = (
    "ALTER TABLE tasks ADD COLUMN batch_id TEXT REFERENCES batches(id) ON DELETE SET NULL"
)
INDEX_TASK_BATCH_SQL = "CREATE INDEX IF NOT EXISTS idx_task_batch ON tasks(batch_id)"


def apply_serve_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BATCHES_TABLE_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "batch_id" not in cols:
        conn.execute(ALTER_TASKS_SQL)
    conn.execute(INDEX_TASK_BATCH_SQL)
    conn.commit()
