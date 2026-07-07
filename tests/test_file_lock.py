from pathlib import Path

from parsing_core.utils.file_lock import snapshot


def test_snapshot_returns_different_path(tmp_path: Path):
    src = tmp_path / "orig.txt"
    src.write_text("payload")
    snap = snapshot(str(src))
    assert snap != str(src)
    assert Path(snap).read_text() == "payload"


def test_snapshot_does_not_modify_original(tmp_path: Path):
    src = tmp_path / "orig.txt"
    src.write_text("original")
    snap = snapshot(str(src))
    Path(snap).write_text("mutated")
    assert src.read_text() == "original"


def test_snapshot_preserves_extension(tmp_path: Path):
    src = tmp_path / "data.xlsx"
    src.write_text("x")
    snap = snapshot(str(src))
    assert snap.endswith(".xlsx")
