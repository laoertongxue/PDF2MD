import copy
import json
import os
from pathlib import Path

import pytest

from parsing_core.workbench.ocr.chapters import (
    ChapterConfirmationError,
    _chapter_fingerprint,
    detect_chapter_tree,
    load_chapter_confirmation,
    persist_chapter_confirmation,
    validate_chapter_confirmation,
)


def _page(number, *lines, evidence=None, fingerprint=None):
    blocks = []
    for index, text in enumerate(lines):
        blocks.append(
            {
                "id": f"p{number}-b{index}",
                "type": "paragraph",
                "text": text,
                "region": {"x": 0.1, "y": 0.1 + index * 0.1, "width": 0.8, "height": 0.08},
                "bounding_box": {"x": 0.1, "y": 0.1 + index * 0.1, "width": 0.8, "height": 0.08},
                "confidence": 0.98,
                "reading_order": index + 1,
                "candidates": [],
                "uncertainty_reason": "",
                "table": None,
                "formula": None,
                "source_region": f"p{number}-r{index}",
            }
        )
    return {
        "page": number,
        "decision": {
            "payload": {
                "page": {"number": number, "width": 1200, "height": 1600},
                "final_blocks": blocks,
                "resolved_conflicts": [],
                "tables": [],
                "formulas": [],
                "decision_evidence": ["ocr evidence"],
                "confidence": 0.98,
                "status": "accepted",
            }
        },
        "page_input_fingerprint": fingerprint or f"page-input-{number}",
        "evidence_fingerprint": evidence or f"evidence-{number}",
    }


def _confirmation_contract():
    tree = detect_chapter_tree(
        [_page(1, "目录", "第一章 战略管理 ........ 2"), _page(2, "第一章 战略管理", "正文")],
        input_fingerprint="book-input",
    )
    chapter = tree["chapters"][0]
    return {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": chapter["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": chapter,
        "chapter_fingerprint": _chapter_fingerprint(chapter),
    }


def test_detects_multilevel_toc_and_maps_body_ranges_deterministically():
    pages = [
        _page(1, "目录", "第一章 战略管理 ........ 5", "1.1 战略的定义 ........ 7"),
        _page(2, "前言", "本书介绍管理问题。"),
        _page(3, "第一章 战略管理", "战略是组织的长期方向。"),
        _page(4, "1.1 战略的定义", "战略回答组织去哪里。"),
        _page(5, "第二章 外部环境", "环境影响组织选择。"),
    ]

    first = detect_chapter_tree(pages, input_fingerprint="book-input")
    second = detect_chapter_tree(list(reversed(pages)), input_fingerprint="book-input")

    assert first == second
    assert [node["number"] for node in first["chapters"]] == ["第一章", "第二章"]
    assert first["chapters"][0]["page_start"] == 3
    assert first["chapters"][0]["page_end"] == 4
    assert first["chapters"][0]["children"][0]["page_start"] == 4
    assert first["chapters"][0]["children"][0]["page_end"] == 4
    assert first["chapters"][0]["children"][0]["number"] == "1.1"
    assert first["chapters"][0]["needs_confirmation"] is False
    assert first["chapters"][0]["source_evidence"]


def test_marks_missing_toc_as_confirmation_required():
    tree = detect_chapter_tree(
        [_page(1, "第一章 战略管理", "正文"), _page(2, "第二章 外部环境", "正文")],
        input_fingerprint="book-input",
    )

    assert tree["warnings"] == ["目录页未识别，章节边界来自正文标题"]
    assert tree["needs_confirmation"] is True
    assert all(chapter["needs_confirmation"] for chapter in tree["chapters"])


def test_supports_english_chapter_numbers_and_toc_page_mapping():
    tree = detect_chapter_tree(
        [
            _page(1, "Contents", "Chapter 1 Foundations ........ 3", "1.1 Scope ........ 4"),
            _page(2, "Chapter 1 Foundations", "The foundation."),
            _page(3, "1.1 Scope", "The scope."),
        ],
        input_fingerprint="book-input",
    )

    assert tree["chapters"][0]["number"] == "Chapter 1"
    assert tree["chapters"][0]["toc_page"] == 3
    assert tree["chapters"][0]["page_start"] == 2
    assert tree["chapters"][0]["children"][0]["number"] == "1.1"
    assert tree["chapters"][0]["children"][0]["toc_page"] == 4


def test_marks_page_conflict_and_duplicate_title_without_silent_choice():
    pages = [
        _page(1, "目录", "第一章 组织行为 ........ 4", "第一章 组织行为 ........ 8"),
        _page(2, "第一章 组织行为", "正文"),
        _page(3, "第一章 组织行为", "另一处正文"),
    ]
    tree = detect_chapter_tree(pages, input_fingerprint="book-input")

    assert tree["needs_confirmation"] is True
    assert "目录页码冲突" in tree["warnings"]
    assert "正文标题重复" in tree["warnings"]
    assert all(chapter["needs_confirmation"] for chapter in tree["chapters"])


def test_confirmation_is_versioned_and_bound_to_current_evidence(tmp_path):
    tree = detect_chapter_tree(
        [_page(1, "目录", "第一章 战略管理 ........ 2"), _page(2, "第一章 战略管理", "正文")],
        input_fingerprint="book-input",
    )
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": tree["chapters"][0]["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": tree["chapters"][0],
        "chapter_fingerprint": _chapter_fingerprint(tree["chapters"][0]),
    }
    target = tmp_path / "confirmation.json"
    persist_chapter_confirmation(target, confirmation)
    assert load_chapter_confirmation(target) == confirmation
    validate_chapter_confirmation(confirmation, tree)

    changed = dict(tree)
    changed["evidence_fingerprint"] = "changed"
    with pytest.raises(ChapterConfirmationError, match="evidence"):
        validate_chapter_confirmation(confirmation, changed)


def test_confirmation_atomic_write_retries_short_writes(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import chapters

    tree = detect_chapter_tree(
        [_page(1, "目录", "第一章 战略管理 ........ 2"), _page(2, "第一章 战略管理", "正文")],
        input_fingerprint="book-input",
    )
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": tree["chapters"][0]["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": tree["chapters"][0],
        "chapter_fingerprint": _chapter_fingerprint(tree["chapters"][0]),
    }
    real_write = os.write
    write_sizes = []

    def short_write(fd, data):
        write_sizes.append(len(data))
        return real_write(fd, data[:13])

    monkeypatch.setattr(chapters, "_write_file", short_write, raising=False)
    target = tmp_path / "confirmation.json"

    persist_chapter_confirmation(target, confirmation)

    assert load_chapter_confirmation(target) == confirmation
    assert len(write_sizes) > 1
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure_mode", ["zero", "error"])
def test_confirmation_atomic_write_failure_preserves_target_cleans_temp_and_closes_fd(
    tmp_path, monkeypatch, failure_mode
):
    from parsing_core.workbench.ocr import chapters

    tree = detect_chapter_tree(
        [_page(1, "目录", "第一章 战略管理 ........ 2"), _page(2, "第一章 战略管理", "正文")],
        input_fingerprint="book-input",
    )
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": tree["chapters"][0]["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": tree["chapters"][0],
        "chapter_fingerprint": _chapter_fingerprint(tree["chapters"][0]),
    }
    target = tmp_path / "confirmation.json"
    target.write_bytes(b"previous")
    failure = OSError("disk full")
    opened_fds = []

    def fail_write(fd, _data):
        opened_fds.append(fd)
        if failure_mode == "zero":
            return 0
        raise failure

    monkeypatch.setattr(chapters, "_write_file", fail_write, raising=False)

    with pytest.raises(OSError) as error:
        persist_chapter_confirmation(target, confirmation)

    if failure_mode == "zero":
        assert "no progress" in str(error.value)
    else:
        assert error.value is failure
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.glob(".chapter-confirmation.*")) == []
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_confirmation_atomic_write_closes_each_fd_once_without_closing_reused_fd(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import chapters

    target = tmp_path / "confirmation.json"
    reused_path = tmp_path / "reused-fd"
    real_close = os.close
    opened_fd = None
    reused_fd = None
    close_calls = []

    def record_write(fd, data):
        nonlocal opened_fd
        opened_fd = fd
        return os.write(fd, data)

    def close_and_reuse(fd):
        nonlocal reused_fd
        close_calls.append(fd)
        real_close(fd)
        if fd == opened_fd and reused_fd is None:
            reused_fd = os.open(reused_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            assert reused_fd == fd

    monkeypatch.setattr(chapters, "_write_file", record_write)
    monkeypatch.setattr(chapters, "_close_file", close_and_reuse, raising=False)

    try:
        persist_chapter_confirmation(target, _confirmation_contract())

        assert opened_fd is not None
        assert close_calls.count(opened_fd) == 1
        assert all(close_calls.count(fd) == 1 for fd in set(close_calls))
        assert reused_fd == opened_fd
        os.fstat(reused_fd)
    finally:
        if reused_fd is not None:
            real_close(reused_fd)


def test_confirmation_replace_is_bound_to_opened_directory_fd(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import chapters

    parent = tmp_path / "confirmation-parent"
    parent.mkdir()
    moved_parent = tmp_path / "opened-parent"
    target = parent / "confirmation.json"
    real_open_directory = chapters._open_directory
    real_sync_directory = chapters._sync_directory
    opened_identity = None
    synced_identities = []

    def open_then_swap(path):
        nonlocal opened_identity
        fd = real_open_directory(path)
        opened = os.fstat(fd)
        opened_identity = (opened.st_dev, opened.st_ino)
        path.rename(moved_parent)
        path.mkdir()
        return fd

    def record_sync(fd):
        current = os.fstat(fd)
        synced_identities.append((current.st_dev, current.st_ino))
        real_sync_directory(fd)

    monkeypatch.setattr(chapters, "_open_directory", open_then_swap)
    monkeypatch.setattr(chapters, "_sync_directory", record_sync)

    persist_chapter_confirmation(target, _confirmation_contract())

    committed = moved_parent / target.name
    assert opened_identity is not None
    assert synced_identities == [opened_identity]
    assert load_chapter_confirmation(committed) == _confirmation_contract()
    assert list(parent.iterdir()) == []
    assert list(moved_parent.glob(".chapter-confirmation.*")) == []


def test_confirmation_rejects_parent_swap_before_directory_open(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import chapters

    parent = tmp_path / "confirmation-parent"
    parent.mkdir()
    moved_parent = tmp_path / "original-parent"
    target = parent / "confirmation.json"
    target.write_bytes(b"previous")
    real_mkstemp = chapters.tempfile.mkstemp
    attacker_temporary = None

    def create_then_swap(*args, **kwargs):
        nonlocal attacker_temporary
        fd, name = real_mkstemp(*args, **kwargs)
        temporary_name = Path(name).name
        parent.rename(moved_parent)
        parent.mkdir()
        attacker_temporary = parent / temporary_name
        attacker_temporary.write_bytes(b"attacker-controlled")
        return fd, name

    monkeypatch.setattr(chapters.tempfile, "mkstemp", create_then_swap)

    with pytest.raises(ValueError, match="temporary file changed"):
        persist_chapter_confirmation(target, _confirmation_contract())

    assert (moved_parent / target.name).read_bytes() == b"previous"
    assert not target.exists()
    assert attacker_temporary is not None
    assert attacker_temporary.read_bytes() == b"attacker-controlled"


@pytest.mark.parametrize("alias_kind", ["same_path", "hard_link"])
def test_atomic_replace_rejects_target_alias(tmp_path, alias_kind):
    from parsing_core.workbench.ocr import atomic_io

    temporary = tmp_path / "temporary.json"
    temporary.write_bytes(b"original")
    target = temporary
    if alias_kind == "hard_link":
        target = tmp_path / "target.json"
        os.link(temporary, target)

    with pytest.raises(ValueError, match="must not alias"):
        atomic_io.atomic_replace_file(
            temporary=temporary,
            target=target,
            close_file=os.close,
            open_directory=lambda path: os.open(
                path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            ),
            replace_file=lambda source, destination, directory_fd: os.replace(
                source.name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            ),
            sync_directory=os.fsync,
            unlink_temporary=lambda path: path.unlink(missing_ok=True),
        )

    assert temporary.read_bytes() == b"original"
    assert target.read_bytes() == b"original"


def test_confirmation_directory_open_failure_is_precommit_and_preserves_original(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import chapters

    target = tmp_path / "confirmation.json"
    target.write_bytes(b"previous")
    failure = OSError("directory open failed")

    def fail_directory_open(_path):
        raise failure

    monkeypatch.setattr(chapters, "_open_directory", fail_directory_open, raising=False)

    with pytest.raises(OSError) as error:
        persist_chapter_confirmation(target, _confirmation_contract())

    assert error.value is failure
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.glob(".chapter-confirmation.*")) == []


def test_confirmation_directory_fsync_failure_reports_committed_artifact(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import chapters

    target = tmp_path / "confirmation.json"
    target.write_bytes(b"previous")
    failure = OSError("directory fsync failed")

    def fail_directory_fsync(_fd):
        raise failure

    monkeypatch.setattr(chapters, "_sync_directory", fail_directory_fsync, raising=False)

    with pytest.raises(OSError) as error:
        persist_chapter_confirmation(target, _confirmation_contract())

    assert type(error.value).__name__ == "AtomicCommitError"
    assert getattr(error.value, "committed", False) is True
    assert getattr(error.value, "durability_uncertain", False) is True
    assert error.value.__cause__ is failure
    assert load_chapter_confirmation(target) == _confirmation_contract()
    assert list(tmp_path.glob(".chapter-confirmation.*")) == []


def test_confirmation_cleanup_failure_does_not_mask_write_error(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import atomic_io, chapters

    target = tmp_path / "confirmation.json"
    target.write_bytes(b"previous")
    write_failure = OSError("write failed")
    cleanup_calls = []

    def fail_write(_fd, _data):
        raise write_failure

    def fail_cleanup(name, directory_fd):
        os.fstat(directory_fd)
        cleanup_calls.append(name)
        raise OSError("cleanup failed")

    monkeypatch.setattr(chapters, "_write_file", fail_write)
    monkeypatch.setattr(atomic_io, "_unlink_name", fail_cleanup)

    with pytest.raises(OSError) as error:
        persist_chapter_confirmation(target, _confirmation_contract())

    assert error.value is write_failure
    assert len(cleanup_calls) == 1
    assert target.read_bytes() == b"previous"


def test_confirmation_rejects_unknown_fields_and_symlink_target(tmp_path):
    tree = detect_chapter_tree([_page(1, "第一章 标题", "正文")], input_fingerprint="book")
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "reject",
        "chapter_id": tree["chapters"][0]["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": None,
        "chapter_fingerprint": None,
        "extra": "must fail",
    }
    with pytest.raises(ChapterConfirmationError):
        validate_chapter_confirmation(confirmation, tree)

    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({key: value for key, value in confirmation.items() if key != "extra"}),
        encoding="utf-8",
    )
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    with pytest.raises(ChapterConfirmationError, match="target"):
        persist_chapter_confirmation(
            link, {key: value for key, value in confirmation.items() if key != "extra"}
        )


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("title", lambda chapter: chapter.update(title="恶意标题")),
        ("page range", lambda chapter: chapter.update(page_end=99)),
        (
            "children",
            lambda chapter: chapter["children"].append(copy.deepcopy(chapter["children"][0])),
        ),
        (
            "evidence",
            lambda chapter: chapter["source_evidence"][0].update(excerpt="伪造证据"),
        ),
    ],
)
def test_confirm_rejects_any_tampered_chapter_content(field, mutate):
    tree = detect_chapter_tree(
        [
            _page(1, "目录", "第一章 战略管理 ........ 2", "1.1 范围 ........ 3"),
            _page(2, "第一章 战略管理", "正文"),
            _page(3, "1.1 范围", "正文"),
        ],
        input_fingerprint="book-input",
    )
    original = tree["chapters"][0]
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": original["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": copy.deepcopy(original),
        "chapter_fingerprint": _chapter_fingerprint(original),
    }
    mutate(confirmation["chapter"])
    confirmation["chapter_fingerprint"] = _chapter_fingerprint(confirmation["chapter"])

    with pytest.raises(ChapterConfirmationError, match="match proposal"):
        validate_chapter_confirmation(confirmation, tree)


def test_edit_requires_new_content_fingerprint_and_keeps_evidence_bound():
    tree = detect_chapter_tree(
        [_page(1, "第一章 战略管理", "正文"), _page(2, "第二章 外部环境", "正文")],
        input_fingerprint="book-input",
    )
    original = tree["chapters"][0]
    edited = copy.deepcopy(original)
    edited["title"] = "战略管理：修订标题"
    confirmation = {
        "schema_version": 1,
        "revision": 2,
        "action": "edit",
        "chapter_id": original["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": edited,
        "chapter_fingerprint": _chapter_fingerprint(edited),
    }
    validate_chapter_confirmation(confirmation, tree)

    confirmation["chapter_fingerprint"] = _chapter_fingerprint(original)
    with pytest.raises(ChapterConfirmationError, match="content fingerprint"):
        validate_chapter_confirmation(confirmation, tree)

    confirmation["chapter_fingerprint"] = _chapter_fingerprint(edited)
    edited["source_evidence"][0]["excerpt"] = "篡改后的来源"
    confirmation["chapter_fingerprint"] = _chapter_fingerprint(edited)
    with pytest.raises(ChapterConfirmationError, match="cannot be edited"):
        validate_chapter_confirmation(confirmation, tree)
