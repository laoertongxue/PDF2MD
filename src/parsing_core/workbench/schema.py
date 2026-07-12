import sqlite3

WORKBENCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wb_courses (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  root_dir TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_sources (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  file_path TEXT NOT NULL,
  title TEXT NOT NULL,
  markdown_path TEXT,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_chapters (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES wb_sources(id) ON DELETE CASCADE,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  source_md_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  source_start INTEGER NOT NULL DEFAULT 0,
  source_end INTEGER NOT NULL DEFAULT 0,
  confirmed_snapshot_json TEXT NOT NULL DEFAULT '',
  confirmed_at INTEGER,
  UNIQUE(source_id, seq)
);

CREATE TABLE IF NOT EXISTS wb_attachments (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  chapter_id TEXT REFERENCES wb_chapters(id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  source_id TEXT REFERENCES wb_sources(id) ON DELETE CASCADE,
  parsed_text TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  anchors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS wb_note_blocks (
  id TEXT PRIMARY KEY,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  seq INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(chapter_id, kind)
);

CREATE TABLE IF NOT EXISTS wb_cards (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  favorite INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_runs (
  id TEXT PRIMARY KEY,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  round_key TEXT NOT NULL,
  executor TEXT NOT NULL,
  status TEXT NOT NULL,
  input_path TEXT NOT NULL DEFAULT '',
  output_path TEXT NOT NULL DEFAULT '',
  output TEXT NOT NULL DEFAULT '',
  stale INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(chapter_id, round_key)
);

CREATE TABLE IF NOT EXISTS wb_chapter_generation_leases (
  chapter_id TEXT PRIMARY KEY REFERENCES wb_chapters(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  heartbeat_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_chapter_generation_runs (
  id TEXT PRIMARY KEY,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  round_key TEXT NOT NULL,
  status TEXT NOT NULL,
  output TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL,
  finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS wb_chapter_generation_candidates (
  run_id TEXT PRIMARY KEY REFERENCES wb_chapter_generation_runs(id) ON DELETE CASCADE,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  round_key TEXT NOT NULL,
  output TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_topics (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'DRAFT',
  confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
  stale_reason TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  generation_reason TEXT NOT NULL DEFAULT '',
  UNIQUE(course_id, seq)
);

CREATE TABLE IF NOT EXISTS wb_topic_chapters (
  topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(topic_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS wb_topic_note_blocks (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(topic_id, kind)
);

CREATE TABLE IF NOT EXISTS wb_topic_cards (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
  card_type TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_topic_runs (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
  round_key TEXT NOT NULL,
  status TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL,
  output TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL,
  finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS wb_topic_generation_leases (
  topic_id TEXT PRIMARY KEY REFERENCES wb_topics(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  heartbeat_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_topic_markdown_sync (
  topic_id TEXT PRIMARY KEY REFERENCES wb_topics(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('PENDING', 'SYNCING', 'SYNCED', 'FAILED')),
  error TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL,
  owner_id TEXT NOT NULL DEFAULT '',
  lease_expires_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wb_sources_course ON wb_sources(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_chapters_course ON wb_chapters(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_cards_course ON wb_cards(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_runs_chapter ON wb_runs(chapter_id);
CREATE INDEX IF NOT EXISTS idx_wb_chapter_generation_runs
  ON wb_chapter_generation_runs(chapter_id, started_at);
CREATE INDEX IF NOT EXISTS idx_wb_chapter_generation_candidates
  ON wb_chapter_generation_candidates(chapter_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_wb_topics_course ON wb_topics(course_id, seq);
CREATE INDEX IF NOT EXISTS idx_wb_topic_chapters_chapter ON wb_topic_chapters(chapter_id);
CREATE INDEX IF NOT EXISTS idx_wb_topic_note_blocks_topic ON wb_topic_note_blocks(topic_id);
CREATE INDEX IF NOT EXISTS idx_wb_topic_cards_topic ON wb_topic_cards(topic_id);
CREATE INDEX IF NOT EXISTS idx_wb_topic_runs_topic ON wb_topic_runs(topic_id, started_at);
"""


def apply_workbench_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(WORKBENCH_SCHEMA_SQL)
    chapter_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_chapters)")}
    for name, definition in {
        "source_start": "INTEGER NOT NULL DEFAULT 0",
        "source_end": "INTEGER NOT NULL DEFAULT 0",
        "confirmed_snapshot_json": "TEXT NOT NULL DEFAULT ''",
        "confirmed_at": "INTEGER",
    }.items():
        if name not in chapter_columns:
            conn.execute(f"ALTER TABLE wb_chapters ADD COLUMN {name} {definition}")
    attachment_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_attachments)")}
    for name, definition in {
        "source_id": "TEXT REFERENCES wb_sources(id) ON DELETE CASCADE",
        "parsed_text": "TEXT NOT NULL DEFAULT ''",
        "content_hash": "TEXT NOT NULL DEFAULT ''",
        "anchors_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if name not in attachment_columns:
            conn.execute(f"ALTER TABLE wb_attachments ADD COLUMN {name} {definition}")
    topic_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_topics)")}
    if "generation_reason" not in topic_columns:
        conn.execute("ALTER TABLE wb_topics ADD COLUMN generation_reason TEXT NOT NULL DEFAULT ''")
    card_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_cards)")}
    for column, definition in (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
    ):
        if column not in card_columns:
            conn.execute(f"ALTER TABLE wb_cards ADD COLUMN {column} {definition}")
    topic_card_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_topic_cards)")}
    for column, definition in (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in topic_card_columns:
            conn.execute(f"ALTER TABLE wb_topic_cards ADD COLUMN {column} {definition}")
    conn.execute("UPDATE wb_topic_cards SET updated_at = created_at WHERE updated_at = 0")
    sync_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_topic_markdown_sync)")}
    sync_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wb_topic_markdown_sync'"
    ).fetchone()[0]
    if {"owner_id", "lease_expires_at"} - sync_columns or "'SYNCING'" not in sync_sql:
        conn.executescript(
            """
            ALTER TABLE wb_topic_markdown_sync RENAME TO wb_topic_markdown_sync_old;
            CREATE TABLE wb_topic_markdown_sync (
              topic_id TEXT PRIMARY KEY REFERENCES wb_topics(id) ON DELETE CASCADE,
              status TEXT NOT NULL CHECK (status IN ('PENDING', 'SYNCING', 'SYNCED', 'FAILED')),
              error TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL,
              owner_id TEXT NOT NULL DEFAULT '',
              lease_expires_at INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO wb_topic_markdown_sync
              (topic_id, status, error, updated_at, owner_id, lease_expires_at)
            SELECT topic_id, status, error, updated_at, '', 0
            FROM wb_topic_markdown_sync_old;
            DROP TABLE wb_topic_markdown_sync_old;
            """
        )
    conn.commit()
