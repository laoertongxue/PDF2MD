from parsing_core.storage.schema import init_db
from parsing_core.workbench.schema import apply_workbench_schema


def test_apply_workbench_schema_creates_tables(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert "wb_courses" in tables
    assert "wb_sources" in tables
    assert "wb_chapters" in tables
    assert "wb_attachments" in tables
    assert "wb_note_blocks" in tables
    assert "wb_cards" in tables
    assert "wb_runs" in tables


def test_apply_workbench_schema_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)
    apply_workbench_schema(conn)

    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(wb_cards)").fetchall()
    }
    assert {"id", "course_id", "chapter_id", "kind", "title", "body", "favorite"} <= cols
