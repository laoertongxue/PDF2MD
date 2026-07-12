import stat
import threading
from pathlib import Path

import pytest

from parsing_core.workbench.source_import import (
    SUPPORTED_TEXTBOOK_EXTENSIONS,
    CourseStorageError,
    import_textbook_file,
)


def course_root(tmp_path: Path) -> Path:
    root = tmp_path / "course"
    root.mkdir()
    return root


def test_supported_textbook_extensions_cover_office_and_common_images():
    assert {
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
    } <= SUPPORTED_TEXTBOOK_EXTENSIONS


@pytest.mark.parametrize("suffix", [".pdf", ".PDF", ".doc", ".DOCX"])
def test_import_copies_supported_file_and_preserves_unicode_title(tmp_path, suffix):
    root = course_root(tmp_path)
    source = tmp_path / f"战略管理（第 3 版）{suffix}"
    source.write_bytes(b"textbook-content")

    imported = import_textbook_file(root, source)

    assert imported.title == "战略管理（第 3 版）"
    assert imported.source_path == source.resolve()
    assert imported.stored_path == root / "教材原文件" / source.name
    assert imported.stored_path.read_bytes() == b"textbook-content"
    assert source.read_bytes() == b"textbook-content"


def test_import_preserves_private_source_permissions(tmp_path):
    root = course_root(tmp_path)
    source = tmp_path / "private.pdf"
    source.write_bytes(b"private")
    source.chmod(0o600)

    imported = import_textbook_file(root, source)

    assert stat.S_IMODE(imported.stored_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "source_factory",
    [
        lambda tmp_path: Path("relative.pdf"),
        lambda tmp_path: Path("~/book.pdf"),
        lambda tmp_path: tmp_path / "missing.pdf",
        lambda tmp_path: tmp_path / "directory.pdf",
        lambda tmp_path: tmp_path / "book.txt",
    ],
)
def test_import_rejects_invalid_source_paths(tmp_path, source_factory):
    root = course_root(tmp_path)
    source = source_factory(tmp_path)
    if source == tmp_path / "directory.pdf":
        source.mkdir()
    elif source == tmp_path / "book.txt":
        source.write_text("unsupported", encoding="utf-8")

    with pytest.raises(ValueError):
        import_textbook_file(root, source)

    assert not (root / "教材原文件").exists()


@pytest.mark.parametrize(
    "root_factory",
    [
        lambda tmp_path: Path("relative-course"),
        lambda tmp_path: Path("~/course"),
        lambda tmp_path: tmp_path / "missing-course",
        lambda tmp_path: tmp_path / "course.pdf",
    ],
)
def test_import_rejects_invalid_course_roots(tmp_path, root_factory):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")
    root = root_factory(tmp_path)
    if root == tmp_path / "course.pdf":
        root.write_bytes(b"not-a-directory")

    with pytest.raises(ValueError):
        import_textbook_file(root, source)


def test_repeated_imports_create_independent_numbered_files(tmp_path):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"same-source")

    imported = [import_textbook_file(root, source) for _ in range(3)]

    assert [item.stored_path.name for item in imported] == ["book.pdf", "book-2.pdf", "book-3.pdf"]
    assert [item.stored_path.read_bytes() for item in imported] == [b"same-source"] * 3


def test_existing_same_name_file_is_preserved(tmp_path):
    root = course_root(tmp_path)
    target_dir = root / "教材原文件"
    target_dir.mkdir()
    existing = target_dir / "book.pdf"
    existing.write_bytes(b"existing")
    source = tmp_path / "book.pdf"
    source.write_bytes(b"new")

    imported = import_textbook_file(root, source)

    assert existing.read_bytes() == b"existing"
    assert imported.stored_path.name == "book-2.pdf"
    assert imported.stored_path.read_bytes() == b"new"


def test_final_file_appears_atomically_after_copy_completes(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    content = b"complete-textbook-content"
    source.write_bytes(content)
    copy_started = threading.Event()
    release_copy = threading.Event()
    real_copyfileobj = __import__("shutil").copyfileobj

    def paused_copy(source_file, target_file, *args, **kwargs):
        close_source = not hasattr(source_file, "read")
        close_target = not hasattr(target_file, "write")
        if close_source:
            source_file = open(source_file, "rb")
        if close_target:
            target_file = open(target_file, "wb")
        try:
            target_file.write(source_file.read(8))
            target_file.flush()
            copy_started.set()
            assert release_copy.wait(timeout=5)
            real_copyfileobj(source_file, target_file)
        finally:
            if close_source:
                source_file.close()
            if close_target:
                target_file.close()

    monkeypatch.setattr("parsing_core.workbench.source_import.shutil.copyfile", paused_copy)
    monkeypatch.setattr("parsing_core.workbench.source_import.shutil.copyfileobj", paused_copy)
    results = []
    errors = []

    def run_import():
        try:
            results.append(import_textbook_file(root, source))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_import)
    thread.start()
    try:
        assert copy_started.wait(timeout=5)
        target_dir = root / "教材原文件"
        assert not (target_dir / "book.pdf").exists()
        assert not (target_dir / "book-2.pdf").exists()
        visible_files = list(target_dir.iterdir())
        assert len(visible_files) == 1
        assert visible_files[0].name.startswith(".")
        assert visible_files[0].suffix == ".tmp"
    finally:
        release_copy.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    assert results[0].stored_path == root / "教材原文件" / "book.pdf"
    assert results[0].stored_path.read_bytes() == content
    assert list((root / "教材原文件").glob("*.tmp")) == []


def test_copy_failure_removes_reserved_target_and_temporary_file(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    def fail_copy(source_file, temporary_file, *args, **kwargs):
        if hasattr(temporary_file, "write"):
            temporary_file.write(b"partial")
        else:
            Path(temporary_file).write_bytes(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr("parsing_core.workbench.source_import.shutil.copyfile", fail_copy)
    monkeypatch.setattr("parsing_core.workbench.source_import.shutil.copyfileobj", fail_copy)

    with pytest.raises(CourseStorageError, match="course storage could not complete import"):
        import_textbook_file(root, source)

    assert list((root / "教材原文件").iterdir()) == []


def test_temporary_close_failure_removes_all_import_files(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")
    real_fdopen = __import__("os").fdopen

    class CloseFailure:
        def __init__(self, file_object):
            self.file_object = file_object

        def __enter__(self):
            self.file_object.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.file_object.__exit__(exc_type, exc_value, traceback)
            raise OSError("close failed")

        def __getattr__(self, name):
            return getattr(self.file_object, name)

    def fail_temporary_close(descriptor, mode="r", *args, **kwargs):
        file_object = real_fdopen(descriptor, mode, *args, **kwargs)
        if "w" in mode:
            return CloseFailure(file_object)
        return file_object

    monkeypatch.setattr("parsing_core.workbench.source_import.os.fdopen", fail_temporary_close)

    with pytest.raises(CourseStorageError, match="course storage could not complete import"):
        import_textbook_file(root, source)

    assert list((root / "教材原文件").iterdir()) == []


def test_temporary_permission_failure_removes_temporary_file(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    def fail_fchmod(descriptor, mode):
        raise OSError("permission update failed")

    monkeypatch.setattr("parsing_core.workbench.source_import.os.fchmod", fail_fchmod)

    with pytest.raises(CourseStorageError, match="course storage could not complete import"):
        import_textbook_file(root, source)

    assert list((root / "教材原文件").iterdir()) == []


def test_link_failure_removes_temporary_file_without_publishing(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    def fail_link(source_path, target_path, *args, **kwargs):
        raise OSError("link failed")

    monkeypatch.setattr("parsing_core.workbench.source_import.os.link", fail_link)

    with pytest.raises(CourseStorageError, match="course storage could not complete import"):
        import_textbook_file(root, source)

    assert list((root / "教材原文件").iterdir()) == []


def test_temporary_unlink_failure_removes_published_file_and_raises(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")
    real_unlink = __import__("os").unlink
    failed_once = False

    def fail_first_temporary_unlink(path, *args, **kwargs):
        nonlocal failed_once
        if str(path).endswith(".tmp") and not failed_once:
            failed_once = True
            raise OSError("temporary unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        "parsing_core.workbench.source_import.os.unlink",
        fail_first_temporary_unlink,
    )

    with pytest.raises(CourseStorageError, match="course storage could not complete import"):
        import_textbook_file(root, source)

    assert failed_once
    assert list((root / "教材原文件").iterdir()) == []


def test_symlink_source_is_resolved_before_copy(tmp_path):
    root = course_root(tmp_path)
    real_source = tmp_path / "real.pdf"
    real_source.write_bytes(b"resolved-content")
    source_link = tmp_path / "linked.pdf"
    source_link.symlink_to(real_source)

    imported = import_textbook_file(root, source_link)

    assert imported.source_path == real_source.resolve()
    assert imported.stored_path.name == "real.pdf"
    assert imported.stored_path.read_bytes() == b"resolved-content"


def test_concurrent_same_name_imports_do_not_overwrite(tmp_path, monkeypatch):
    root = course_root(tmp_path)
    source_a = tmp_path / "a" / "book.pdf"
    source_b = tmp_path / "b" / "book.pdf"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    source_a.write_bytes(b"content-a")
    source_b.write_bytes(b"content-b")
    barrier = threading.Barrier(2)
    arrivals = []
    arrivals_lock = threading.Lock()
    real_link = __import__("os").link

    def synchronized_link(temporary_path, destination, *args, **kwargs):
        if Path(destination).name == "book.pdf":
            with arrivals_lock:
                arrivals.append(threading.get_ident())
            barrier.wait(timeout=5)
        return real_link(temporary_path, destination, *args, **kwargs)

    monkeypatch.setattr("parsing_core.workbench.source_import.os.link", synchronized_link)
    results = []
    errors = []

    def run(source):
        try:
            results.append(import_textbook_file(root, source))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(source,)) for source in (source_a, source_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(arrivals) == 2
    assert len(set(arrivals)) == 2
    assert errors == []
    assert len(results) == 2
    assert {item.stored_path.name for item in results} == {"book.pdf", "book-2.pdf"}
    assert {item.source_path: item.stored_path.read_bytes() for item in results} == {
        source_a.resolve(): b"content-a",
        source_b.resolve(): b"content-b",
    }
    assert list((root / "教材原文件").glob("*.tmp")) == []
