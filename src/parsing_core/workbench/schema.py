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
  UNIQUE(source_id, seq)
);

CREATE TABLE IF NOT EXISTS wb_attachments (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  chapter_id TEXT REFERENCES wb_chapters(id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at INTEGER NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_wb_sources_course ON wb_sources(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_chapters_course ON wb_chapters(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_cards_course ON wb_cards(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_runs_chapter ON wb_runs(chapter_id);
"""


def apply_workbench_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(WORKBENCH_SCHEMA_SQL)
    conn.commit()
