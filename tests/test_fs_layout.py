from pathlib import Path

from parsing_core.storage.fs_layout import FsLayout


def test_task_dir_pattern(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    d = fs.task_dir("t1")
    assert d == str(tmp_path / "t1")
    assert Path(d).exists()


def test_section_raw_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.section_raw_path("t1", 0)
    assert p.endswith("t1/0.raw.md")


def test_section_ai_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.section_ai_path("t1", 0)
    assert p.endswith("t1/0.ai.md")


def test_merged_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.merged_path("t1")
    assert p.endswith("t1/merged.md")


def test_default_base_uses_appdata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    fs = FsLayout()
    assert str(tmp_path) in fs.base_dir
