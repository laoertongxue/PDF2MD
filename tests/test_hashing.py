from pathlib import Path

from parsing_core.utils.hashing import file_sha256, text_sha256


def test_text_sha256_deterministic():
    assert text_sha256("hello") == text_sha256("hello")
    assert text_sha256("hello") != text_sha256("world")


def test_text_sha256_known_value():
    assert text_sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_file_sha256(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    assert file_sha256(str(f)) == text_sha256("hello")
