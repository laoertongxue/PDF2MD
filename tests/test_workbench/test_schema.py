import sqlite3

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench.schema import apply_workbench_schema


def test_apply_workbench_schema_creates_tables(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }

    assert "wb_courses" in tables
    assert "wb_sources" in tables
    assert "wb_chapters" in tables
    assert "wb_attachments" in tables
    assert "wb_note_blocks" in tables
    assert "wb_cards" in tables
    assert "wb_runs" in tables
    assert "wb_topics" in tables
    assert "wb_topic_chapters" in tables
    assert "wb_topic_note_blocks" in tables
    assert "wb_topic_cards" in tables
    assert "wb_topic_runs" in tables


def test_apply_workbench_schema_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)
    conn.execute(
        """
        INSERT INTO wb_courses (id, title, root_dir, created_at, updated_at)
        VALUES ('course-1', '战略管理', '/tmp/course', 1, 1)
        """
    )
    conn.commit()
    apply_workbench_schema(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(wb_cards)").fetchall()}
    assert {"id", "course_id", "chapter_id", "kind", "title", "body", "favorite"} <= cols

    topic_cols = {r[1] for r in conn.execute("PRAGMA table_info(wb_topics)").fetchall()}
    assert {
        "id",
        "course_id",
        "seq",
        "title",
        "description",
        "status",
        "confirmed",
        "stale_reason",
        "generation_reason",
        "created_at",
        "updated_at",
    } == topic_cols
    stored_title = conn.execute(
        "SELECT title FROM wb_courses WHERE id = 'course-1'"
    ).fetchone()[0]
    assert stored_title == "战略管理"


def test_topic_schema_applies_database_defaults(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)
    conn.execute(
        """
        INSERT INTO wb_courses (id, title, root_dir, created_at, updated_at)
        VALUES ('course-1', '战略管理', '/tmp/course', 1, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO wb_topics (id, course_id, seq, title, created_at, updated_at)
        VALUES ('topic-1', 'course-1', 0, '竞争优势', 1, 1)
        """
    )

    row = conn.execute(
        "SELECT description, status, confirmed, stale_reason, generation_reason "
        "FROM wb_topics WHERE id = 'topic-1'"
    ).fetchone()
    assert tuple(row) == ("", "DRAFT", 0, "", "")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """
            INSERT INTO wb_topics
              (id, course_id, seq, title, confirmed, created_at, updated_at)
            VALUES ('topic-2', 'course-1', 1, '非法确认值', 2, 1, 1)
            """
        )


def test_topic_schema_constraints_and_foreign_keys(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    topic_indexes = conn.execute("PRAGMA index_list(wb_topics)").fetchall()
    assert any(index[2] for index in topic_indexes)

    run_indexes = conn.execute("PRAGMA index_list(wb_topic_runs)").fetchall()
    unique_run_columns = {
        tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall())
        for index in run_indexes
        if index[2]
    }
    assert ("topic_id", "round_key") not in unique_run_columns


def test_apply_schema_preserves_existing_topics_table_without_rebuilding(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
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
        CREATE TABLE wb_topics (
          id TEXT PRIMARY KEY,
          course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'DRAFT',
          confirmed INTEGER NOT NULL DEFAULT 0,
          stale_reason TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(course_id, seq)
        );
        INSERT INTO wb_courses
          (id, title, root_dir, created_at, updated_at)
        VALUES ('course-1', '战略管理', '/tmp/course', 1, 1);
        INSERT INTO wb_topics
          (id, course_id, seq, title, confirmed, created_at, updated_at)
        VALUES ('topic-1', 'course-1', 0, '旧主题', 2, 1, 1);
        """
    )

    apply_workbench_schema(conn)

    row = conn.execute(
        "SELECT title, confirmed, generation_reason FROM wb_topics WHERE id = 'topic-1'"
    ).fetchone()
    assert tuple(row) == ("旧主题", 2, "")
