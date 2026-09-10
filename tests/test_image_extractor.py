# tests/test_image_extractor.py
from pathlib import Path
from urllib.parse import quote

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


def test_local_destination_is_angle_wrapped_and_percent_encoded(tmp_path: Path):
    src = "![diagram](data:image/png;base64,aW1hZ2U=)"
    out_dir = tmp_path / "Application Support" / r"figures>chapter\one"
    out_dir.mkdir(parents=True)

    result_md, images = extract_images(src, str(out_dir))

    image_path = out_dir / "img_000.png"
    encoded_path = quote(str(image_path), safe="/")
    assert result_md == f"![diagram](<{encoded_path}>)"
    assert images == [str(image_path)]
    assert image_path.read_bytes() == b"image"


def test_multiline_alt_is_normalized_to_one_markdown_line(tmp_path: Path):
    src = "![first\r\nsecond\nthird](data:image/png;base64,aW1hZ2U=)"
    out_dir = tmp_path / "Application Support" / "images"
    out_dir.mkdir(parents=True)

    result_md, _ = extract_images(src, str(out_dir))

    assert result_md.startswith("![first second third](<")
    assert "\r" not in result_md
    assert "\n" not in result_md
