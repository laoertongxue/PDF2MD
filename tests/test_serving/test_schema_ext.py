from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema


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
