from parsing_core.storage.schema import init_db


def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "x.db"
    conn = init_db(str(db_path))
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert {"tasks", "sections", "ai_artifacts"} <= names
    conn.close()


def test_init_db_enables_wal(tmp_path):
    db_path = tmp_path / "x.db"
    conn = init_db(str(db_path))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_init_db_idempotent(tmp_path):
    db_path = tmp_path / "x.db"
    conn1 = init_db(str(db_path))
    conn1.close()
    conn2 = init_db(str(db_path))
    cur = conn2.execute("SELECT count(*) FROM tasks")
    assert cur.fetchone()[0] == 0
    conn2.close()
