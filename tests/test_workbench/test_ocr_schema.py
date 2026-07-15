import sqlite3
from dataclasses import FrozenInstanceError, fields

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench.schema import (
    UnsupportedOcrSchemaVersionError,
    apply_workbench_schema,
)

OCR_TABLES = {
    "wb_ocr_pages",
    "wb_ocr_observations",
    "wb_ocr_diffs",
    "wb_ocr_decisions",
    "wb_page_blocks",
    "wb_ocr_leases",
}


def _insert_course_and_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO wb_courses (id, title, root_dir, created_at, updated_at) "
        "VALUES ('course-1', '战略管理', '/tmp/course', 1, 1)"
    )
    conn.execute(
        "INSERT INTO wb_sources "
        "(id, course_id, kind, file_path, title, status, created_at, updated_at) "
        "VALUES ('source-1', 'course-1', 'pdf', '/tmp/book.pdf', '教材', 'READY', 1, 1)"
    )


def _insert_page(conn: sqlite3.Connection, page_id: str = "page-1") -> None:
    conn.execute(
        "INSERT INTO wb_ocr_pages "
        "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
        "VALUES (?, 'source-1', 1, 'render-v1', '/tmp/page-1.png', 'input-1', 1)",
        (page_id,),
    )


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()[0]


def _insert_observation_pair(conn: sqlite3.Connection) -> None:
    for observation_id, engine in (
        ("observation-1", "apple_vision"),
        ("observation-2", "codex_vision"),
    ):
        conn.execute(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES (?, 'page-1', ?, 'input-1', 'config-1', '{}', 1)",
            (observation_id, engine),
        )


def _insert_complete_ocr_graph(conn: sqlite3.Connection) -> None:
    _insert_course_and_source(conn)
    _insert_page(conn)
    _insert_observation_pair(conn)
    conn.execute(
        "INSERT INTO wb_ocr_diffs "
        "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
        "VALUES ('diff-1', 'page-1', 'observation-1', 'observation-2', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_decisions "
        "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
        "VALUES ('page-1', 'direct', '[]', '{}', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_page_blocks "
        "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
        "VALUES ('block-1', 'page-1', 0, 'body', '内容', '[]', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_leases "
        "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
        "VALUES ('page-1', 'worker-1', 1, 2, 'fingerprint')"
    )


def _database_snapshot(conn: sqlite3.Connection):
    schema = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    rows = {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in tables
    }
    return schema, rows


def test_ocr_schema_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))

    apply_workbench_schema(conn)
    apply_workbench_schema(conn)

    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert OCR_TABLES <= names


def test_ocr_schema_enforces_foreign_keys_and_unique_inputs(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_page(conn, "page-duplicate")
    conn.execute(
        "INSERT INTO wb_ocr_pages "
        "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
        "VALUES ('page-new-input', 'source-1', 1, 'render-v1', '/tmp/page-1.png', "
        "'input-2', 1)"
    )

    observation = (
        "observation-1",
        "page-1",
        "apple_vision",
        "input-1",
        "engine-v1",
        "{}",
        1,
    )
    conn.execute(
        "INSERT INTO wb_ocr_observations "
        "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        observation,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('observation-2', ?, ?, ?, ?, ?, ?)",
            observation[1:],
        )


def test_ocr_diff_observations_must_belong_to_same_page(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    conn.execute(
        "INSERT INTO wb_ocr_pages "
        "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
        "VALUES ('page-2', 'source-1', 2, 'render-v1', '/tmp/page-2.png', 'input-2', 1)"
    )
    for observation_id, page_id in (
        ("observation-1", "page-1"),
        ("observation-2", "page-2"),
    ):
        conn.execute(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES (?, ?, 'apple_vision', 'input', 'config', '{}', 1)",
            (observation_id, page_id),
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES "
            "('diff-1', 'page-1', 'observation-1', 'observation-2', '{}', 1)"
        )

    conn.execute(
        "INSERT INTO wb_page_blocks "
        "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
        "VALUES ('block-1', 'page-1', 0, 'text', '内容', '[]', 0.9, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('block-2', 'page-1', 0, 'text', '重复', '[]', 0.8, 1)"
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES ('missing-page', 'worker-1', 1, 2, 'fingerprint')"
        )


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        pytest.param(
            "wb_ocr_pages",
            "INSERT INTO wb_ocr_pages "
            "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
            "VALUES (NULL, 'source-1', 2, 'render', '/tmp/page.png', 'input-2', 1)",
            id="pages",
        ),
        pytest.param(
            "wb_ocr_observations",
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES (NULL, 'page-1', 'baidu_pp_structure', 'input', 'config', '{}', 1)",
            id="observations",
        ),
        pytest.param(
            "wb_ocr_diffs",
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES (NULL, 'page-1', 'observation-1', 'observation-2', '{}', 1)",
            id="diffs",
        ),
        pytest.param(
            "wb_ocr_decisions",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES (NULL, 'direct', '[]', '{}', 0.5, 1)",
            id="decisions",
        ),
        pytest.param(
            "wb_page_blocks",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES (NULL, 'page-1', 0, 'body', '', '[]', 0.5, 1)",
            id="blocks",
        ),
        pytest.param(
            "wb_ocr_leases",
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES (NULL, 'worker', 1, 2, 'fingerprint')",
            id="leases",
        ),
    ],
)
def test_ocr_text_primary_keys_explicitly_reject_null(tmp_path, table, sql):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    _insert_observation_pair(conn)

    primary_key = next(row for row in conn.execute(f"PRAGMA table_info({table})") if row[5])
    assert primary_key[2].upper() == "TEXT"
    assert primary_key[3] == 1
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "INSERT INTO wb_ocr_pages "
            "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
            "VALUES ('orphan-page', NULL, 2, 'render', '/tmp/page.png', 'input', 1)",
            id="page-source",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('orphan-observation', NULL, 'apple_vision', 'input', 'config', '{}', 1)",
            id="observation-page",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('orphan-diff', NULL, 'observation-1', 'observation-2', '{}', 1)",
            id="diff-page",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('orphan-diff', 'page-1', NULL, 'observation-2', '{}', 1)",
            id="diff-left-observation",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('orphan-diff', 'page-1', 'observation-1', NULL, '{}', 1)",
            id="diff-right-observation",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES (NULL, 'direct', '[]', '{}', 0.5, 1)",
            id="decision-page",
        ),
        pytest.param(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('orphan-block', NULL, 0, 'body', '', '[]', 0.5, 1)",
            id="block-page",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES (NULL, 'worker', 1, 2, 'fingerprint')",
            id="lease-page",
        ),
    ],
)
def test_ocr_foreign_keys_reject_null_orphans(tmp_path, sql):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    _insert_observation_pair(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute(sql)


@pytest.mark.parametrize("delete_target", ["source", "course"])
def test_deleting_source_or_course_cascades_ocr_results(tmp_path, delete_target):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    for observation_id, engine in (
        ("observation-1", "apple_vision"),
        ("observation-2", "codex_vision"),
    ):
        conn.execute(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES (?, 'page-1', ?, 'input-1', 'engine-v1', '{}', 1)",
            (observation_id, engine),
        )
    conn.execute(
        "INSERT INTO wb_ocr_diffs "
        "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
        "VALUES "
        "('diff-1', 'page-1', 'observation-1', 'observation-2', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_decisions "
        "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
        "VALUES ('page-1', 'direct', '[]', '{}', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_leases "
        "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
        "VALUES ('page-1', 'worker-1', 1, 2, 'fingerprint')"
    )
    conn.execute(
        "INSERT INTO wb_page_blocks "
        "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
        "VALUES ('block-1', 'page-1', 0, 'body', '内容', '[]', 0.9, 1)"
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    if delete_target == "source":
        conn.execute("DELETE FROM wb_sources WHERE id = 'source-1'")
    else:
        conn.execute("DELETE FROM wb_courses WHERE id = 'course-1'")

    for table in OCR_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_ocr_schema_upgrades_old_workbench_fixture(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    conn.executescript(
        """
        CREATE TABLE wb_courses (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          root_dir TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE wb_sources (
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
        CREATE TABLE wb_chapters (
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
        INSERT INTO wb_courses
          (id, title, root_dir, created_at, updated_at)
        VALUES ('course-1', '旧课程', '/tmp/course', 1, 1);
        INSERT INTO wb_sources
          (id, course_id, kind, file_path, title, status, created_at, updated_at)
        VALUES ('source-1', 'course-1', 'pdf', '/tmp/book.pdf', '旧教材', 'READY', 1, 1);
        INSERT INTO wb_chapters
          (id, source_id, course_id, seq, title, status, created_at, updated_at)
        VALUES ('chapter-1', 'source-1', 'course-1', 1, '旧章节', 'READY', 1, 1);
        """
    )

    apply_workbench_schema(conn)
    apply_workbench_schema(conn)
    _insert_page(conn)

    assert conn.execute("SELECT title FROM wb_sources").fetchone()[0] == "旧教材"
    assert conn.execute(
        "SELECT title, source_start, source_end, confirmed_snapshot_json "
        "FROM wb_chapters WHERE id = 'chapter-1'"
    ).fetchone() == ("旧章节", 0, 0, "")


def test_ocr_schema_rebuilds_empty_partial_tables(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    conn.execute("CREATE TABLE wb_ocr_pages (id TEXT PRIMARY KEY)")

    apply_workbench_schema(conn)
    apply_workbench_schema(conn)

    assert "input_hash" in {row[1] for row in conn.execute("PRAGMA table_info(wb_ocr_pages)")}
    assert "input_hash" in _table_sql(conn, "wb_ocr_pages")


def test_ocr_schema_migrates_populated_v1_page_table(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    conn.execute("DROP TABLE wb_ocr_pages")
    conn.execute(
        """
        CREATE TABLE wb_ocr_pages (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL REFERENCES wb_sources(id) ON DELETE CASCADE,
          page_number INTEGER NOT NULL,
          render_config_hash TEXT NOT NULL,
          image_path TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(source_id, page_number, render_config_hash)
        )
        """
    )
    _insert_page(conn)
    conn.commit()

    apply_workbench_schema(conn)

    assert conn.execute("SELECT id, input_hash FROM wb_ocr_pages").fetchone() == (
        "page-1",
        "input-1",
    )
    assert "input_hash" in _table_sql(conn, "wb_ocr_pages")


def test_ocr_schema_recovers_interrupted_partial_migration(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    for table in (
        "wb_ocr_leases",
        "wb_page_blocks",
        "wb_ocr_decisions",
        "wb_ocr_diffs",
        "wb_ocr_observations",
        "wb_ocr_pages",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(
        """
        CREATE TABLE wb_ocr_pages (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL REFERENCES wb_sources(id) ON DELETE CASCADE,
          page_number INTEGER NOT NULL,
          render_config_hash TEXT NOT NULL,
          image_path TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(source_id, page_number, render_config_hash)
        )
        """
    )
    _insert_page(conn)
    conn.execute(
        """
        CREATE TABLE wb_ocr_observations (
          id TEXT PRIMARY KEY,
          page_id TEXT NOT NULL REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          engine TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO wb_ocr_observations "
        "(id, page_id, engine, input_hash, payload_json, created_at) "
        "VALUES ('observation-1', 'page-1', 'apple_vision', 'input-1', '{}', 1)"
    )
    conn.execute(
        """
        CREATE TABLE wb_ocr_decisions (
          page_id TEXT PRIMARY KEY REFERENCES wb_ocr_pages(id) ON DELETE CASCADE,
          status TEXT NOT NULL,
          final_blocks_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          confidence REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO wb_ocr_decisions "
        "(page_id, status, final_blocks_json, evidence_json, confidence) "
        "VALUES ('page-1', 'direct', '[]', '{}', 0.9)"
    )
    conn.execute("DELETE FROM wb_schema_versions WHERE component = 'ocr'")
    conn.commit()

    apply_workbench_schema(conn)
    apply_workbench_schema(conn)

    assert conn.execute("SELECT id FROM wb_ocr_pages").fetchone()[0] == "page-1"
    assert conn.execute(
        "SELECT id, engine_config_hash FROM wb_ocr_observations"
    ).fetchone() == ("observation-1", "")
    assert conn.execute("SELECT page_id, decided_at FROM wb_ocr_decisions").fetchone() == (
        "page-1",
        0,
    )
    assert OCR_TABLES <= {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_ocr_schema_rebuild_preserves_all_populated_tables(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    for observation_id, engine in (
        ("observation-1", "apple_vision"),
        ("observation-2", "codex_vision"),
    ):
        conn.execute(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES (?, 'page-1', ?, 'input-1', 'config-1', '{}', 1)",
            (observation_id, engine),
        )
    conn.execute(
        "INSERT INTO wb_ocr_diffs "
        "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
        "VALUES ('diff-1', 'page-1', 'observation-1', 'observation-2', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_decisions "
        "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
        "VALUES ('page-1', 'direct', '[]', '{}', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_page_blocks "
        "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
        "VALUES ('block-1', 'page-1', 0, 'body', '内容', '[]', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_leases "
        "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
        "VALUES ('page-1', 'worker-1', 1, 2, 'fingerprint')"
    )
    conn.execute("DELETE FROM wb_schema_versions WHERE component = 'ocr'")
    conn.commit()

    apply_workbench_schema(conn)

    assert {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in OCR_TABLES
    } == {
        "wb_ocr_pages": 1,
        "wb_ocr_observations": 2,
        "wb_ocr_diffs": 1,
        "wb_ocr_decisions": 1,
        "wb_page_blocks": 1,
        "wb_ocr_leases": 1,
    }


def test_ocr_schema_repairs_missing_and_wrong_explicit_indexes(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_complete_ocr_graph(conn)
    expected = {
        "idx_wb_ocr_pages_source": ("wb_ocr_pages", ("source_id", "page_number")),
        "idx_wb_ocr_observations_page": (
            "wb_ocr_observations",
            ("page_id", "created_at"),
        ),
        "idx_wb_ocr_diffs_page": ("wb_ocr_diffs", ("page_id", "created_at")),
        "idx_wb_page_blocks_page": ("wb_page_blocks", ("page_id", "seq")),
    }
    for index_name in expected:
        conn.execute(f"DROP INDEX {index_name}")
    conn.execute("CREATE INDEX idx_wb_ocr_pages_source ON wb_ocr_pages(input_hash)")
    conn.execute("CREATE INDEX idx_wb_ocr_extra ON wb_ocr_pages(input_hash)")
    before_rows = _database_snapshot(conn)[1]
    conn.commit()

    apply_workbench_schema(conn)

    for index_name, (table, columns) in expected.items():
        index_table = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        assert index_table == (table,)
        assert tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index_name})")) == columns
        details = next(
            row for row in conn.execute(f"PRAGMA index_list({table})") if row[1] == index_name
        )
        assert (details[2], details[3], details[4]) == (0, "c", 0)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_wb_ocr_extra'"
    ).fetchone() == (1,)
    assert _database_snapshot(conn)[1] == before_rows


def test_ocr_schema_rejects_future_metadata_without_modifying_ocr_state(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_complete_ocr_graph(conn)
    current_version = conn.execute(
        "SELECT version FROM wb_schema_versions WHERE component = 'ocr'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE wb_schema_versions SET version = ? WHERE component = 'ocr'",
        (current_version + 1,),
    )
    conn.execute("CREATE INDEX idx_wb_ocr_future ON wb_ocr_pages(input_hash)")
    conn.commit()
    before = _database_snapshot(conn)

    with pytest.raises(UnsupportedOcrSchemaVersionError, match="newer"):
        apply_workbench_schema(conn)

    assert _database_snapshot(conn) == before


def test_ocr_schema_rejects_partial_table_missing_identity_columns_without_changes(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    conn.execute("CREATE TABLE wb_ocr_pages (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO wb_ocr_pages (id) VALUES ('page-1')")
    conn.commit()

    for _ in range(2):
        with pytest.raises(RuntimeError, match="cannot safely migrate populated wb_ocr_pages"):
            apply_workbench_schema(conn)

    assert conn.execute("SELECT id FROM wb_ocr_pages").fetchone()[0] == "page-1"


def test_apply_workbench_schema_rejects_active_transaction(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE transaction_probe (id INTEGER)")
    conn.execute("INSERT INTO transaction_probe VALUES (1)")
    assert conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    with pytest.raises(RuntimeError, match="outside an active transaction"):
        apply_workbench_schema(conn)

    assert conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    conn.rollback()
    apply_workbench_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_apply_workbench_schema_enables_foreign_keys_on_return(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    conn.execute("PRAGMA foreign_keys = OFF")

    apply_workbench_schema(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("case", "sql"),
    [
        (
            "page number starts at one",
            "INSERT INTO wb_ocr_pages "
            "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
            "VALUES ('invalid-page', 'source-1', 0, 'render', '/tmp/page.png', 'input', 1)",
        ),
        (
            "page creation time is nonnegative",
            "INSERT INTO wb_ocr_pages "
            "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
            "VALUES ('invalid-page', 'source-1', 2, 'render', '/tmp/page.png', 'input', -1)",
        ),
        (
            "observation engine is known",
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('invalid-observation', 'page-1', 'unknown', 'input', 'config', '{}', 1)",
        ),
        (
            "observation payload is JSON",
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('invalid-json', 'page-1', 'apple_vision', 'input', 'config', 'no', 1)",
        ),
        (
            "observation creation time is nonnegative",
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('invalid-time', 'page-1', 'apple_vision', 'input', 'config', '{}', -1)",
        ),
        (
            "diff payload is JSON",
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('invalid-diff', 'page-1', 'observation-1', 'observation-2', 'no', 1)",
        ),
        (
            "diff creation time is nonnegative",
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('invalid-diff', 'page-1', 'observation-1', 'observation-2', '{}', -1)",
        ),
        (
            "decision status is known",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'unknown', '[]', '{}', 0.5, 1)",
        ),
        (
            "decision blocks are JSON",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', 'no', '{}', 0.5, 1)",
        ),
        (
            "decision evidence is JSON",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', 'no', 0.5, 1)",
        ),
        (
            "decision confidence has lower bound",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', '{}', -0.1, 1)",
        ),
        (
            "decision confidence has upper bound",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', '{}', 1.1, 1)",
        ),
        (
            "decision time is nonnegative",
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', '{}', 0.5, -1)",
        ),
        (
            "block sequence is nonnegative",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('invalid-block', 'page-1', -1, 'body', '', '[]', 0.5, 1)",
        ),
        (
            "block type is known",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('invalid-block', 'page-1', 0, 'unknown', '', '[]', 0.5, 1)",
        ),
        (
            "block bbox is JSON",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('invalid-block', 'page-1', 0, 'body', '', 'no', 0.5, 1)",
        ),
        (
            "block confidence has lower bound",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('invalid-block', 'page-1', 0, 'body', '', '[]', -0.1, 1)",
        ),
        (
            "block confidence has upper bound",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('invalid-block', 'page-1', 0, 'body', '', '[]', 1.1, 1)",
        ),
        (
            "block creation time is nonnegative",
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('invalid-block', 'page-1', 0, 'body', '', '[]', 0.5, -1)",
        ),
        (
            "lease heartbeat is nonnegative",
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES ('page-1', 'worker', -1, 2, 'fingerprint')",
        ),
        (
            "lease expiry follows heartbeat",
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES ('page-1', 'worker', 2, 2, 'fingerprint')",
        ),
    ],
)
def test_ocr_schema_rejects_invalid_domain_values(tmp_path, case, sql):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    for observation_id, engine in (
        ("observation-1", "apple_vision"),
        ("observation-2", "codex_vision"),
    ):
        conn.execute(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES (?, 'page-1', ?, 'input-1', 'config-1', '{}', 1)",
            (observation_id, engine),
        )

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "INSERT INTO wb_ocr_pages "
            "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
            "VALUES ('null-check', 'source-1', NULL, 'render', '/tmp/page.png', 'input', 1)",
            id="page-number",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_pages "
            "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
            "VALUES ('null-check', 'source-1', 2, 'render', '/tmp/page.png', 'input', NULL)",
            id="page-created-at",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('null-check', 'page-1', NULL, 'input', 'config', '{}', 1)",
            id="observation-engine",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('null-check', 'page-1', 'apple_vision', 'input', 'config', NULL, 1)",
            id="observation-payload",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_observations "
            "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
            "VALUES ('null-check', 'page-1', 'apple_vision', 'input', 'config', '{}', NULL)",
            id="observation-created-at",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('null-check', 'page-1', 'observation-1', 'observation-2', NULL, 1)",
            id="diff-json",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_diffs "
            "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
            "VALUES ('null-check', 'page-1', 'observation-1', 'observation-2', '{}', NULL)",
            id="diff-created-at",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', NULL, '[]', '{}', 0.5, 1)",
            id="decision-status",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', NULL, '{}', 0.5, 1)",
            id="decision-blocks-json",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', NULL, 0.5, 1)",
            id="decision-evidence-json",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', '{}', NULL, 1)",
            id="decision-confidence",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_decisions "
            "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
            "VALUES ('page-1', 'direct', '[]', '{}', 0.5, NULL)",
            id="decision-decided-at",
        ),
        pytest.param(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('null-check', 'page-1', NULL, 'body', '', '[]', 0.5, 1)",
            id="block-seq",
        ),
        pytest.param(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('null-check', 'page-1', 0, NULL, '', '[]', 0.5, 1)",
            id="block-type",
        ),
        pytest.param(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('null-check', 'page-1', 0, 'body', '', NULL, 0.5, 1)",
            id="block-bbox-json",
        ),
        pytest.param(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('null-check', 'page-1', 0, 'body', '', '[]', NULL, 1)",
            id="block-confidence",
        ),
        pytest.param(
            "INSERT INTO wb_page_blocks "
            "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
            "VALUES ('null-check', 'page-1', 0, 'body', '', '[]', 0.5, NULL)",
            id="block-created-at",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES ('page-1', 'worker', NULL, 2, 'fingerprint')",
            id="lease-heartbeat",
        ),
        pytest.param(
            "INSERT INTO wb_ocr_leases "
            "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
            "VALUES ('page-1', 'worker', 1, NULL, 'fingerprint')",
            id="lease-expires",
        ),
    ],
)
def test_ocr_check_columns_reject_null(tmp_path, sql):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    _insert_observation_pair(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute(sql)


def test_ocr_schema_accepts_domain_boundaries(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    conn.execute(
        "INSERT INTO wb_ocr_pages "
        "(id, source_id, page_number, render_config_hash, image_path, input_hash, created_at) "
        "VALUES ('page-1', 'source-1', 1, 'render', '/tmp/page.png', 'input', 0)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_decisions "
        "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
        "VALUES ('page-1', 'direct', '[]', '{}', 0, 0)"
    )
    conn.execute(
        "INSERT INTO wb_page_blocks "
        "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
        "VALUES ('block-1', 'page-1', 0, 'text', '', '[]', 1, 0)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_leases "
        "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
        "VALUES ('page-1', 'worker', 0, 1, 'fingerprint')"
    )


def test_ocr_domain_models_match_schema_and_are_frozen(tmp_path):
    from parsing_core.workbench.ocr.models import (
        OcrDecision,
        OcrDiff,
        OcrLease,
        OcrObservation,
        OcrPage,
        PageBlock,
    )

    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    model_tables = {
        OcrPage: "wb_ocr_pages",
        OcrObservation: "wb_ocr_observations",
        OcrDiff: "wb_ocr_diffs",
        OcrDecision: "wb_ocr_decisions",
        PageBlock: "wb_page_blocks",
        OcrLease: "wb_ocr_leases",
    }
    for model, table in model_tables.items():
        assert [field.name for field in fields(model)] == [
            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        ]

    observation = OcrObservation(
        id="observation-1",
        page_id="page-1",
        engine="apple_vision",
        input_hash="input-1",
        engine_config_hash="engine-v1",
        payload_json="{}",
        created_at=1,
    )
    decision = OcrDecision(
        page_id="page-1",
        status="direct",
        final_blocks_json="[]",
        evidence_json="{}",
        confidence=0.9,
        decided_at=1,
    )
    block = PageBlock(
        id="block-1",
        page_id="page-1",
        seq=0,
        block_type="text",
        text="内容",
        bbox_json="[]",
        confidence=0.9,
        created_at=1,
    )

    assert block.block_type == "text"

    with pytest.raises(FrozenInstanceError):
        observation.input_hash = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.status = "failed"  # type: ignore[misc]


def test_ocr_models_construct_from_schema_rows(tmp_path):
    from parsing_core.workbench.ocr.models import (
        OcrDecision,
        OcrDiff,
        OcrLease,
        OcrObservation,
        OcrPage,
        PageBlock,
    )

    conn = init_db(str(tmp_path / "workbench.db"))
    conn.row_factory = sqlite3.Row
    apply_workbench_schema(conn)
    _insert_course_and_source(conn)
    _insert_page(conn)
    conn.execute(
        "INSERT INTO wb_ocr_observations "
        "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
        "VALUES ('observation-1', 'page-1', 'apple_vision', 'input-1', 'config-1', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_decisions "
        "(page_id, status, final_blocks_json, evidence_json, confidence, decided_at) "
        "VALUES ('page-1', 'direct', '[]', '{}', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_page_blocks "
        "(id, page_id, seq, block_type, text, bbox_json, confidence, created_at) "
        "VALUES ('block-1', 'page-1', 0, 'text', '内容', '[]', 0.9, 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_observations "
        "(id, page_id, engine, input_hash, engine_config_hash, payload_json, created_at) "
        "VALUES ('observation-2', 'page-1', 'codex_vision', 'input-1', 'config-1', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_diffs "
        "(id, page_id, left_observation_id, right_observation_id, diff_json, created_at) "
        "VALUES ('diff-1', 'page-1', 'observation-1', 'observation-2', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO wb_ocr_leases "
        "(page_id, owner_id, heartbeat_at, expires_at, input_fingerprint) "
        "VALUES ('page-1', 'worker-1', 1, 2, 'fingerprint')"
    )

    page = OcrPage(**dict(conn.execute("SELECT * FROM wb_ocr_pages").fetchone()))
    observation = OcrObservation(**dict(conn.execute(
        "SELECT * FROM wb_ocr_observations WHERE id = 'observation-1'"
    ).fetchone()))
    diff = OcrDiff(**dict(conn.execute("SELECT * FROM wb_ocr_diffs").fetchone()))
    decision = OcrDecision(**dict(conn.execute("SELECT * FROM wb_ocr_decisions").fetchone()))
    block = PageBlock(**dict(conn.execute("SELECT * FROM wb_page_blocks").fetchone()))
    lease = OcrLease(**dict(conn.execute("SELECT * FROM wb_ocr_leases").fetchone()))

    assert page.input_hash == "input-1"
    assert observation.engine_config_hash == "config-1"
    assert diff.adjudication_reason == ""
    assert decision.decided_at == 1
    assert block.block_type == "text"
    assert lease.input_fingerprint == "fingerprint"
