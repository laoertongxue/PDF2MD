# tests/test_image_extractor.py
from pathlib import Path

from parsing_core.parser.image_extractor import extract_images


def test_extracts_two_base64_images(tmp_path: Path):
    src = Path("tests/fixtures/with_base64.md").read_text()
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    result_md, images = extract_images(src, str(out_dir))
    assert len(images) == 2
    for path in images:
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0


def test_replaces_with_local_path(tmp_path: Path):
    src = Path("tests/fixtures/with_base64.md").read_text()
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    result_md, _ = extract_images(src, str(out_dir))
    assert "data:" not in result_md
    assert ".png" in result_md


def test_no_images_passthrough(tmp_path: Path):
    src = "# Title\n\nNo images here."
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    result_md, images = extract_images(src, str(out_dir))
    assert images == []
    assert result_md == src


def test_unique_filenames(tmp_path: Path):
    src = Path("tests/fixtures/with_base64.md").read_text()
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    _, images = extract_images(src, str(out_dir))
    assert len(set(images)) == len(images)
