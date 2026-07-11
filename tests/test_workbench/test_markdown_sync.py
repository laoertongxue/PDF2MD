import gc
import os
import threading
import unicodedata
from pathlib import Path

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench import markdown_sync
from parsing_core.workbench.markdown_sync import (
    allocate_safe_source_names,
    atomic_write_bundle,
    redact_sensitive_text,
    safe_name,
    sync_chapter_markdown,
)
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


def test_sync_chapter_markdown_writes_note_cards_and_mermaid(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(tmp_path / "source.md"))

    Path(chapter.source_md_path).write_text("## 第一章\n原文", encoding="utf-8")
    repo.upsert_note_block(chapter.id, "summary", "本章概要", "战略是取舍。", 0)
    repo.upsert_note_block(chapter.id, "knowledge_mermaid", "知识结构图", "flowchart TD\nA-->B", 1)
    repo.create_card(course.id, chapter.id, "topic", "为什么战略不是口号", "一个可写选题。")

    paths = sync_chapter_markdown(repo, chapter.id)

    note = Path(paths["note"]).read_text(encoding="utf-8")
    cards = Path(paths["cards"]).read_text(encoding="utf-8")
    assert "## 本章概要" in note
    assert "```mermaid" in note
    assert "flowchart TD" in note
    assert "为什么战略不是口号" in cards


def test_sync_chapter_markdown_uses_pure_mermaid_from_fenced_body(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(tmp_path / "source.md"))
    Path(chapter.source_md_path).write_text("## 第一章\n原文", encoding="utf-8")
    repo.upsert_note_block(
        chapter.id,
        "knowledge_mermaid",
        "知识结构图",
        "```mermaid\nflowchart TD\n  CustomNode[自定义节点] --> NextNode[下一步]\n```",
        0,
    )

    paths = sync_chapter_markdown(repo, chapter.id)

    note = Path(paths["note"]).read_text(encoding="utf-8")
    assert note.count("```mermaid") == 1
    assert "CustomNode[自定义节点]" in note


def test_safe_name_normalizes_malicious_and_equivalent_names():
    assert safe_name("../a\\b\x00. ") == "-a-b"
    assert safe_name(".") == "untitled"
    assert safe_name("e\u0301") == safe_name("é")
    assert len(safe_name("长" * 500).encode()) <= 180


def test_sources_with_same_title_get_stable_distinct_textbook_directories(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    first = repo.create_source(course.id, "main", "/tmp/1.pdf", "同名")
    real_suffix = repo.create_source(course.id, "main", "/tmp/2.pdf", "同名（2）")
    duplicate = repo.create_source(course.id, "main", "/tmp/3.pdf", "同名")
    paths = []
    for source in (first, real_suffix, duplicate):
        source_md = tmp_path / f"{source.id}.md"
        source_md.write_text("原文", encoding="utf-8")
        chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(source_md))
        paths.append(Path(sync_chapter_markdown(repo, chapter.id)["note"]))
    assert [path.parents[1].name for path in paths] == ["同名", "同名（2）", "同名（3）"]
    assert len(set(paths)) == 3


def test_safe_source_names_are_unique_after_sanitizing_normalizing_and_truncating():
    long = "长" * 200
    allocated = allocate_safe_source_names(
        [
            ("slash", "A/B"),
            ("backslash", "A\\B"),
            ("real_suffix", "A-B（2）"),
            ("unicode_1", "e\u0301"),
            ("unicode_2", "é"),
            ("long_1", long + "A"),
            ("long_2", long + "a"),
        ]
    )
    normalized = [unicodedata.normalize("NFKC", value).casefold() for value in allocated.values()]
    assert len(normalized) == len(set(normalized))
    assert allocated["slash"] == "A-B"
    assert allocated["backslash"] == "A-B（3）"
    assert allocated["real_suffix"] == "A-B（2）"
    assert all(len(value.encode()) <= 180 for value in allocated.values())


def test_safe_name_colliding_sources_with_same_chapter_do_not_overwrite(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    sources = [
        repo.create_source(course.id, "main", f"/tmp/{index}.pdf", title)
        for index, title in enumerate(("A/B", "A\\B", "A-B（2）"))
    ]
    notes = []
    for index, source in enumerate(sources):
        raw = tmp_path / f"source-{index}.md"
        raw.write_text(f"原文-{index}", encoding="utf-8")
        chapter = repo.create_chapter(course.id, source.id, 0, "同章", str(raw))
        notes.append((chapter, Path(sync_chapter_markdown(repo, chapter.id)["note"])))

    assert len({note.parents[1] for _, note in notes}) == 3
    first_chapter, first_note = notes[0]
    first_before = first_note.read_text(encoding="utf-8")
    sync_chapter_markdown(repo, notes[1][0].id)
    assert first_note.read_text(encoding="utf-8") == first_before
    assert f"<!-- chapter-id: {first_chapter.id} -->" in first_before


def test_marker_migration_preserves_unknown_files_and_conflict_fails(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/1.pdf", "教材")
    source_md = tmp_path / "source.md"
    source_md.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "旧名", str(source_md))
    first = Path(sync_chapter_markdown(repo, chapter.id)["note"]).parent
    (first / "mine.txt").write_text("保留", encoding="utf-8")
    repo.conn.execute("UPDATE wb_chapters SET title = '新名' WHERE id = ?", (chapter.id,))
    repo.conn.commit()
    moved = Path(sync_chapter_markdown(repo, chapter.id)["note"]).parent
    assert moved.name == "01-新名"
    assert (moved / "mine.txt").read_text(encoding="utf-8") == "保留"

    repo.conn.execute("UPDATE wb_chapters SET title = '冲突' WHERE id = ?", (chapter.id,))
    repo.conn.commit()
    conflict = moved.parent / "01-冲突"
    conflict.mkdir()
    (conflict / "user.txt").write_text("用户", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sync_chapter_markdown(repo, chapter.id)
    assert moved.exists() and (conflict / "user.txt").exists()


@pytest.mark.parametrize("foreign_marker", ["", "<!-- chapter-id: other -->"])
def test_preexisting_desired_chapter_directory_is_never_overwritten(tmp_path, foreign_marker):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "章节", str(raw))
    desired = tmp_path / "out" / "教材" / "教材" / "01-章节"
    desired.mkdir(parents=True)
    user_file = desired / "intensive-note.md"
    user_file.write_text(f"{foreign_marker}\n用户内容", encoding="utf-8")

    with pytest.raises(FileExistsError, match="target directory already exists"):
        sync_chapter_markdown(repo, chapter.id)
    assert user_file.read_text(encoding="utf-8") == f"{foreign_marker}\n用户内容"


def test_atomic_write_failure_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "note.md"
    target.write_text("旧内容", encoding="utf-8")
    original = os.replace

    def fail_replace(src, dst):
        if Path(dst) == target:
            raise OSError("replace failed")
        return original(src, dst)

    monkeypatch.setattr(markdown_sync.os, "replace", fail_replace)
    with pytest.raises(OSError):
        markdown_sync.atomic_write_text(target, "新内容")
    assert target.read_text(encoding="utf-8") == "旧内容"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("target_exists", [True, False])
def test_atomic_write_rolls_back_when_first_directory_fsync_fails(
    tmp_path, monkeypatch, target_exists
):
    target = tmp_path / "note.md"
    if target_exists:
        target.write_text("旧内容", encoding="utf-8")
    original_fsync = os.fsync
    calls = 0

    def fail_first_directory_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("primary dir fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(markdown_sync.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="primary dir fsync failed"):
        markdown_sync.atomic_write_text(target, "新内容")

    assert target.exists() is target_exists
    if target_exists:
        assert target.read_text(encoding="utf-8") == "旧内容"
    assert not list(tmp_path.glob(".*.pdf2md-tmp"))
    assert not list(tmp_path.glob(".*.pdf2md-backup"))


def test_atomic_write_preserves_primary_error_when_rollback_fsync_also_fails(tmp_path, monkeypatch):
    target = tmp_path / "note.md"
    target.write_text("旧内容", encoding="utf-8")
    original_fsync = os.fsync
    calls = 0

    def fail_directory_fsyncs(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("primary dir fsync failed")
        if calls == 3:
            raise OSError("rollback dir fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(markdown_sync.os, "fsync", fail_directory_fsyncs)
    with pytest.raises(OSError, match="primary dir fsync failed"):
        markdown_sync.atomic_write_text(target, "新内容")

    assert target.read_text(encoding="utf-8") == "旧内容"
    assert not list(tmp_path.glob(".*.pdf2md-tmp"))
    assert not list(tmp_path.glob(".*.pdf2md-backup"))


def test_atomic_write_cleanup_failure_keeps_committed_target_and_program_backup(
    tmp_path, monkeypatch
):
    target = tmp_path / "note.md"
    target.write_text("旧内容", encoding="utf-8")
    original_unlink = os.unlink
    backup_unlinks = 0

    def fail_final_backup_unlink(path, *args, **kwargs):
        nonlocal backup_unlinks
        if str(path).endswith(".pdf2md-backup"):
            backup_unlinks += 1
            if backup_unlinks == 2:
                raise OSError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(markdown_sync.os, "unlink", fail_final_backup_unlink)
    with pytest.raises(OSError, match="committed but backup cleanup failed"):
        markdown_sync.atomic_write_text(target, "新内容")

    assert target.read_text(encoding="utf-8") == "新内容"
    backups = list(tmp_path.glob(".*.pdf2md-backup"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "旧内容"
    assert not list(tmp_path.glob(".*.pdf2md-tmp"))


def test_chapter_run_redacts_paths_file_uris_and_keys(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "章节", str(raw))
    repo.upsert_run(
        chapter.id,
        "review",
        "exec",
        "DONE",
        "/Users/a/input",
        "/home/a/output",
        "正常中文 /Users/a/x /home/a/y C:\\secret\\x file:///tmp/x sk-abcdefghijklmnopqrstuvwxyz",
    )
    note = Path(sync_chapter_markdown(repo, chapter.id)["note"])
    run_text = (note.parent / "runs" / "review.md").read_text(encoding="utf-8")
    assert "正常中文" in run_text
    for secret in ("/Users/", "/home/", "C:\\", "file://", "sk-"):
        assert secret not in run_text


def test_redact_sensitive_text_handles_all_absolute_paths_without_harming_safe_text():
    value = (
        "正常中文 /tmp/a /var/log/x /private/a /Volumes/Disk/a /opt/tool "
        "/usr/local/bin/x C:\\secret\\x file:///tmp/x sk-abcdefghijklmnopqrstuvwxyz "
        "api_key=topsecret https://example.com/a/b ./notes/a.md ../cards.md A-->B 甲/乙"
    )
    redacted = redact_sensitive_text(value)
    for secret in (
        "/tmp/",
        "/var/",
        "/private/",
        "/Volumes/",
        "/opt/",
        "/usr/",
        "C:\\",
        "file://",
        "sk-",
        "topsecret",
    ):
        assert secret not in redacted
    for safe in (
        "正常中文",
        "https://example.com/a/b",
        "./notes/a.md",
        "../cards.md",
        "A-->B",
        "甲/乙",
    ):
        assert safe in redacted


@pytest.mark.parametrize("level", ["教材", "source", "chapter"])
def test_chapter_sync_rejects_symlink_escape_at_every_level(tmp_path, level):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    root = tmp_path / "out"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source = repo.create_source(
        repo.create_course("课程", "", str(root)).id, "main", "/tmp/book.pdf", "教材"
    )
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(source.course_id, source.id, 0, "章节", str(raw))
    if level == "教材":
        (root / "教材").symlink_to(outside, target_is_directory=True)
    else:
        (root / "教材").mkdir()
        if level == "source":
            (root / "教材" / "教材").symlink_to(outside, target_is_directory=True)
        else:
            (root / "教材" / "教材").mkdir()
            (root / "教材" / "教材" / "01-章节").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        sync_chapter_markdown(repo, chapter.id)
    assert not list(outside.iterdir())


def test_marker_scan_ignores_quoted_nonfirst_and_wrong_depth_markers(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "章节", str(raw))
    marker = f"<!-- chapter-id: {chapter.id} -->"
    bad = tmp_path / "out" / "教材" / "other" / "wrong"
    bad.mkdir(parents=True)
    (bad / "quoted.md").write_text(marker, encoding="utf-8")
    (bad / "intensive-note.md").write_text(f"标题\n{marker}", encoding="utf-8")
    wrong_depth = bad / "deeper"
    wrong_depth.mkdir()
    (wrong_depth / "intensive-note.md").write_text(marker, encoding="utf-8")
    path = Path(sync_chapter_markdown(repo, chapter.id)["note"])
    assert path.parent.name == "01-章节"
    assert (bad / "quoted.md").exists()


def test_chapter_first_bundle_failure_keeps_owner_and_retry_succeeds(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "章节", str(raw))
    original = markdown_sync.atomic_write_bundle_fd
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(markdown_sync, "atomic_write_bundle_fd", fail_once)
    with pytest.raises(OSError, match="disk full"):
        sync_chapter_markdown(repo, chapter.id)
    chapter_dir = tmp_path / "out" / "教材" / "教材" / "01-章节"
    assert (chapter_dir / ".pdf2md-owner").read_text(encoding="utf-8") == (
        f"chapter:{chapter.id}\n"
    )
    assert Path(sync_chapter_markdown(repo, chapter.id)["note"]).exists()


def test_bundle_lock_serializes_two_writers_and_keeps_versions_consistent(tmp_path):
    directory = tmp_path / "bundle"
    directory.mkdir()
    errors = []

    def writer(prefix):
        try:
            for version in range(100):
                value = f"{prefix}-{version}"
                atomic_write_bundle(
                    directory,
                    {"topic-map.md": value, "intensive-note.md": value, "cards.md": value},
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(prefix,)) for prefix in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    values = {
        (directory / name).read_text(encoding="utf-8")
        for name in ("topic-map.md", "intensive-note.md", "cards.md")
    }
    assert len(values) == 1
    assert not list(directory.glob("*.bundle-tmp"))
    assert not list(directory.glob("*.bundle-backup"))
    assert not (directory / ".pdf2md-bundle-journal.json").exists()


def test_bundle_fence_runs_before_recovery_and_replace_without_artifacts_on_failure(tmp_path):
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "cards.md").write_text("旧内容", encoding="utf-8")
    calls = 0

    def fence():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("owner lost")

    with pytest.raises(ValueError, match="owner lost"):
        atomic_write_bundle(directory, {"cards.md": "新内容"}, fence=fence)
    assert calls == 1
    assert (directory / "cards.md").read_text(encoding="utf-8") == "旧内容"
    assert not list(directory.glob("*.bundle-tmp"))
    assert not list(directory.glob("*.bundle-backup"))
    assert not (directory / ".pdf2md-bundle-journal.json").exists()


def test_bundle_second_fence_blocks_replace_and_cleans_prepared_files(tmp_path):
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "cards.md").write_text("旧内容", encoding="utf-8")
    calls = 0

    def fence():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("owner lost")

    with pytest.raises(ValueError, match="owner lost"):
        atomic_write_bundle(directory, {"cards.md": "新内容"}, fence=fence)
    assert calls == 2
    assert (directory / "cards.md").read_text(encoding="utf-8") == "旧内容"
    assert not list(directory.glob("*.bundle-tmp"))
    assert not list(directory.glob("*.bundle-backup"))
    assert not (directory / ".pdf2md-bundle-journal.json").exists()


def test_expired_old_owner_waiting_for_bundle_lock_cannot_overwrite_new_owner(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    topic = repo.create_topic(course.id, 0, "主题")
    repo.set_topic_markdown_sync_state(topic.id, "PENDING")
    old = repo.claim_topic_markdown_sync(topic.id, now=0, lease_ttl=10)
    new = repo.claim_topic_markdown_sync(topic.id, now=11, lease_ttl=20)
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "cards.md").write_text("旧版本", encoding="utf-8")
    new_has_lock = threading.Event()
    release_new = threading.Event()
    old_errors = []
    new_fence_calls = 0

    def new_fence():
        nonlocal new_fence_calls
        new_fence_calls += 1
        repo.fence_topic_markdown_sync(topic.id, new.owner_id, now=12, lease_ttl=20)
        if new_fence_calls == 1:
            new_has_lock.set()
            assert release_new.wait(2)

    def new_writer():
        atomic_write_bundle(directory, {"cards.md": "新版本"}, fence=new_fence)
        repo.finish_topic_markdown_sync(topic.id, new.owner_id, "SYNCED", now=13)

    def old_writer():
        try:
            atomic_write_bundle(
                directory,
                {"cards.md": "旧owner版本"},
                fence=lambda: repo.fence_topic_markdown_sync(
                    topic.id, old.owner_id, now=12, lease_ttl=20
                ),
            )
        except ValueError as exc:
            old_errors.append(str(exc))

    new_thread = threading.Thread(target=new_writer)
    old_thread = threading.Thread(target=old_writer)
    new_thread.start()
    assert new_has_lock.wait(2)
    old_thread.start()
    release_new.set()
    new_thread.join()
    old_thread.join()

    assert old_errors == ["topic Markdown sync owner lost"]
    assert (directory / "cards.md").read_text(encoding="utf-8") == "新版本"
    state = repo.get_topic_markdown_sync_state(topic.id)
    assert state.status == "SYNCED" and state.owner_id == ""
    assert not (directory / ".pdf2md-bundle-journal.json").exists()


def test_chapter_bundle_failures_do_not_leak_file_descriptors(tmp_path, monkeypatch):
    fd_dir = Path("/dev/fd")
    if not fd_dir.exists():
        pytest.skip("fd directory is unavailable")
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "章节", str(raw))
    baseline = len(list(fd_dir.iterdir()))

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(markdown_sync, "atomic_write_bundle_fd", fail)
    for _ in range(40):
        with pytest.raises(OSError):
            sync_chapter_markdown(repo, chapter.id)
    gc.collect()
    assert len(list(fd_dir.iterdir())) <= baseline
