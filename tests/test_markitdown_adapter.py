# tests/test_markitdown_adapter.py
from pathlib import Path

from parsing_core.parser.markitdown_adapter import MarkItDownAdapter


def test_parse_md_passthrough(tmp_path: Path):
    adapter = MarkItDownAdapter()
    md = adapter.parse(str(Path("tests/fixtures/sample.md").resolve()))
    assert "Sample" in md
    assert "Hello world" in md


def test_parse_md_text_reads_file(tmp_path: Path):
    adapter = MarkItDownAdapter()
    md_path = tmp_path / "in.md"
    md_path.write_text("# H1\n\nbody")
    md = adapter.parse_text(str(md_path))
    assert "# H1" in md
    assert "body" in md
