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
  input_fingerprint TEXT NOT NULL DEFAULT '',
  citation_ids_json TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS wb_schema_versions (
  component TEXT PRIMARY KEY,
  version INTEGER NOT NULL CHECK (version >= 0)
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

OCR_SCHEMA_VERSION = 3


class UnsupportedOcrSchemaVersionError(RuntimeError):
    """Raised when a database was created by a newer OCR schema."""


OCR_TABLE_ORDER = (
    "wb_ocr_pages",
    "wb_ocr_observations",
    "wb_ocr_diffs",
    "wb_ocr_decisions",
    "wb_page_blocks",
    "wb_ocr_leases",
)
OCR_TABLE_SQL = {
    "wb_ocr_pages": """
        CREATE TABLE wb_ocr_pages (
          id TEXT PRIMARY KEY NOT NULL,
          source_id TEXT NOT NULL REFERENCES wb_sources(id) ON DELETE CASCADE,
          page_number INTEGER NOT NULL CHECK (page_number >= 1),
          render_config_hash TEXT NOT NULL,
          image_path TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL CHECK (created_at >= 0),
          UNIQUE(source_id, page_number, render_config_hash, input_hash)
        )
    """,
    "wb_ocr_observations": """
        CREATE TABLE wb_ocr_observations (
          id TEXT PRIMARY KEY NOT NULL,
          page_id TEXT NOT NULL REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          engine TEXT NOT NULL CHECK (
            engine IN ('apple_vision', 'codex_vision', 'baidu_pp_structure')
          ),
          input_hash TEXT NOT NULL,
          engine_config_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
          created_at INTEGER NOT NULL CHECK (created_at >= 0),
          UNIQUE(page_id, engine, input_hash, engine_config_hash),
          UNIQUE(id, page_id)
        )
    """,
    "wb_ocr_diffs": """
        CREATE TABLE wb_ocr_diffs (
          id TEXT PRIMARY KEY NOT NULL,
          page_id TEXT NOT NULL REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          left_observation_id TEXT NOT NULL,
          right_observation_id TEXT NOT NULL,
          diff_json TEXT NOT NULL CHECK (json_valid(diff_json)),
          adjudication_reason TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL CHECK (created_at >= 0),
          FOREIGN KEY(left_observation_id, page_id)
            REFERENCES wb_ocr_observations(id, page_id) ON DELETE CASCADE,
          FOREIGN KEY(right_observation_id, page_id)
            REFERENCES wb_ocr_observations(id, page_id) ON DELETE CASCADE
        )
    """,
    "wb_ocr_decisions": """
        CREATE TABLE wb_ocr_decisions (
          page_id TEXT PRIMARY KEY NOT NULL REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK (
            status IN ('direct', 'automated_adjudicated', 'waiting_resource', 'failed')
          ),
          final_blocks_json TEXT NOT NULL CHECK (json_valid(final_blocks_json)),
          evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          decided_at INTEGER NOT NULL CHECK (decided_at >= 0)
        )
    """,
    "wb_page_blocks": """
        CREATE TABLE wb_page_blocks (
          id TEXT PRIMARY KEY NOT NULL,
          page_id TEXT NOT NULL REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL CHECK (seq >= 0),
          block_type TEXT NOT NULL CHECK (
            block_type IN (
              'title', 'body', 'page_number', 'footnote', 'table', 'formula', 'image', 'text'
            )
          ),
          text TEXT NOT NULL,
          bbox_json TEXT NOT NULL CHECK (json_valid(bbox_json)),
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          created_at INTEGER NOT NULL CHECK (created_at >= 0),
          UNIQUE(page_id, seq)
        )
    """,
    "wb_ocr_leases": """
        CREATE TABLE wb_ocr_leases (
          page_id TEXT PRIMARY KEY NOT NULL REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          owner_id TEXT NOT NULL,
          heartbeat_at INTEGER NOT NULL CHECK (heartbeat_at >= 0),
          expires_at INTEGER NOT NULL CHECK (expires_at > heartbeat_at),
          input_fingerprint TEXT NOT NULL
        )
    """,
}
OCR_INDEX_SQL = {
    "idx_wb_ocr_pages_source": (
        "CREATE INDEX idx_wb_ocr_pages_source ON wb_ocr_pages(source_id, page_number)"
    ),
    "idx_wb_ocr_observations_page": (
        "CREATE INDEX idx_wb_ocr_observations_page "
        "ON wb_ocr_observations(page_id, created_at)"
    ),
    "idx_wb_ocr_diffs_page": (
        "CREATE INDEX idx_wb_ocr_diffs_page ON wb_ocr_diffs(page_id, created_at)"
    ),
    "idx_wb_page_blocks_page": (
        "CREATE INDEX idx_wb_page_blocks_page ON wb_page_blocks(page_id, seq)"
    ),
}
OCR_INDEX_SIGNATURES = {
    "idx_wb_ocr_pages_source": (
        "wb_ocr_pages",
        ("source_id", "page_number"),
        False,
        "c",
        False,
    ),
    "idx_wb_ocr_observations_page": (
        "wb_ocr_observations",
        ("page_id", "created_at"),
        False,
        "c",
        False,
    ),
    "idx_wb_ocr_diffs_page": (
        "wb_ocr_diffs",
        ("page_id", "created_at"),
        False,
        "c",
        False,
    ),
    "idx_wb_page_blocks_page": ("wb_page_blocks", ("page_id", "seq"), False, "c", False),
}
OCR_TABLE_COLUMNS = {
    "wb_ocr_pages": (
        "id",
        "source_id",
        "page_number",
        "render_config_hash",
        "image_path",
        "input_hash",
        "created_at",
    ),
    "wb_ocr_observations": (
        "id",
        "page_id",
        "engine",
        "input_hash",
        "engine_config_hash",
        "payload_json",
        "created_at",
    ),
    "wb_ocr_diffs": (
        "id",
        "page_id",
        "left_observation_id",
        "right_observation_id",
        "diff_json",
        "adjudication_reason",
        "created_at",
    ),
    "wb_ocr_decisions": (
        "page_id",
        "status",
        "final_blocks_json",
        "evidence_json",
        "confidence",
        "decided_at",
    ),
    "wb_page_blocks": (
        "id",
        "page_id",
        "seq",
        "block_type",
        "text",
        "bbox_json",
        "confidence",
        "created_at",
    ),
    "wb_ocr_leases": (
        "page_id",
        "owner_id",
        "heartbeat_at",
        "expires_at",
        "input_fingerprint",
    ),
}
OCR_MIGRATION_DEFAULTS = {
    "wb_ocr_observations": {"engine_config_hash": ""},
    "wb_ocr_diffs": {"adjudication_reason": ""},
    "wb_ocr_decisions": {"decided_at": 0},
}


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def _ocr_schema_version(conn: sqlite3.Connection) -> int | None:
    if not _table_columns(conn, "wb_schema_versions"):
        return None
    version = conn.execute(
        "SELECT version FROM wb_schema_versions WHERE component = 'ocr'"
    ).fetchone()
    return None if version is None else version[0]


def _reject_future_ocr_schema(conn: sqlite3.Connection) -> None:
    version = _ocr_schema_version(conn)
    if version is not None and version > OCR_SCHEMA_VERSION:
        raise UnsupportedOcrSchemaVersionError(
            f"database OCR schema version {version} is newer than supported "
            f"version {OCR_SCHEMA_VERSION}; refusing to modify it"
        )


def _ocr_tables_are_current(conn: sqlite3.Connection) -> bool:
    for table in OCR_TABLE_ORDER:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None or "".join(row[0].split()).lower() != "".join(
            OCR_TABLE_SQL[table].split()
        ).lower():
            return False
    return True


def _index_signature(
    conn: sqlite3.Connection, index_name: str
) -> tuple[str, tuple[str, ...], bool, str, bool] | None:
    index = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    if index is None:
        return None
    table = index[0]
    details = next(
        row for row in conn.execute(f"PRAGMA index_list({table})") if row[1] == index_name
    )
    columns = tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index_name})"))
    return table, columns, bool(details[2]), details[3], bool(details[4])


def _ocr_indexes_are_current(conn: sqlite3.Connection) -> bool:
    return all(
        _index_signature(conn, index_name) == signature
        for index_name, signature in OCR_INDEX_SIGNATURES.items()
    )


def _repair_ocr_indexes(conn: sqlite3.Connection) -> None:
    for index_name, expected in OCR_INDEX_SIGNATURES.items():
        actual = _index_signature(conn, index_name)
        if actual == expected:
            continue
        if actual is not None:
            conn.execute(f"DROP INDEX {index_name}")
        conn.execute(OCR_INDEX_SQL[index_name])


def _backup_ocr_rows(conn: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    backups: dict[str, list[dict[str, object]]] = {}
    for table in OCR_TABLE_ORDER:
        columns = _table_columns(conn, table)
        if not columns:
            continue
        cursor = conn.execute(f"SELECT * FROM {table}")
        rows = cursor.fetchall()
        missing = set(OCR_TABLE_COLUMNS[table]) - set(columns)
        defaults = OCR_MIGRATION_DEFAULTS.get(table, {})
        if rows and missing - defaults.keys():
            raise RuntimeError(f"cannot safely migrate populated {table}: missing columns")
        backups[table] = []
        for row in rows:
            restored = dict(zip(columns, row, strict=True))
            restored.update({column: defaults[column] for column in missing})
            backups[table].append(restored)
    return backups


def _restore_ocr_rows(
    conn: sqlite3.Connection, backups: dict[str, list[dict[str, object]]]
) -> None:
    for table in OCR_TABLE_ORDER:
        for row in backups.get(table, []):
            columns = tuple(row)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )


def _apply_ocr_schema(conn: sqlite3.Connection) -> None:
    _reject_future_ocr_schema(conn)
    if _ocr_schema_version(conn) == OCR_SCHEMA_VERSION and _ocr_tables_are_current(conn):
        if _ocr_indexes_are_current(conn):
            return
        conn.execute("BEGIN")
        try:
            _repair_ocr_indexes(conn)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        return
    backups = _backup_ocr_rows(conn)
    conn.execute("BEGIN")
    try:
        for table in reversed(OCR_TABLE_ORDER):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table in OCR_TABLE_ORDER:
            conn.execute(OCR_TABLE_SQL[table])
        _restore_ocr_rows(conn, backups)
        for sql in OCR_INDEX_SQL.values():
            conn.execute(sql)
        conn.execute(
            "INSERT INTO wb_schema_versions (component, version) VALUES ('ocr', ?) "
            "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
            (OCR_SCHEMA_VERSION,),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def apply_workbench_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("apply_workbench_schema must run outside an active transaction")
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("apply_workbench_schema could not enable foreign keys")
    _reject_future_ocr_schema(conn)
    conn.executescript(WORKBENCH_SCHEMA_SQL)
    _apply_ocr_schema(conn)
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
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_runs)")}
    for name, definition in {
        "input_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "citation_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if name not in run_columns:
            conn.execute(f"ALTER TABLE wb_runs ADD COLUMN {name} {definition}")
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
