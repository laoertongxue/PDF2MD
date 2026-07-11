import gc
import json
import os
import threading
from pathlib import Path

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench import topic_markdown_sync
from parsing_core.workbench.markdown_sync import recover_atomic_bundle
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_markdown_sync import (
    TopicMarkdownDeleteError,
    delete_unpublished_topic,
    sync_topic_map_markdown,
    sync_topic_markdown,
)
from parsing_core.workbench.topic_pipeline import FIXED_TOPIC_KINDS

TITLES = [
    "1. 主题概要",
    "2. 关联教材与章节",
    "3. 核心概念",
    "4. 教材观点对照",
    "5. 共识与分歧",
    "6. 互补视角",
    "7. 通俗、有趣、生活化的解释",
    "8. 教材案例解读",
    "9. 现实案例与问题解决",
    "10. 综合分析框架",
    "11. 实际应用方法",
    "12. 延伸思考",
    "13. Mermaid 知识结构图",
    "14. Mermaid 应用流程图",
    "15. 写作卡片",
]


def setup_published(tmp_path):
    conn = init_db(str(tmp_path / "db.sqlite"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("课程", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    raw = tmp_path / "raw.md"
    raw.write_text("原文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "章节", str(raw))
    from parsing_core.workbench.markdown_sync import sync_chapter_markdown

    sync_chapter_markdown(repo, chapter.id)
    topic = repo.create_topic(course.id, 0, "主题", "说明", "生成原因")
    repo.update_topic(topic.id, confirmed=True, status="COMPLETED")
    repo.replace_topic_chapters(topic.id, [chapter.id])
    repo.replace_topic_note_blocks(
        topic.id,
        {
            kind: (
                "```mermaid\nflowchart TD\nA-->B\n```"
                if kind.endswith("mermaid")
                else f"内容 {kind}"
            )
            for kind in FIXED_TOPIC_KINDS
        },
    )
    repo.replace_topic_cards(
        topic.id,
        [
            {
                "card_type": "观点",
                "title": f"卡片{i}",
                "content": "内容",
                "source_refs_json": ["[《教材》·第 1 章]"],
            }
            for i in range(8)
        ],
    )
    return repo, topic, chapter


def test_topic_sync_writes_fixed_sections_diagrams_cards_and_relative_link(tmp_path):
    repo, topic, _ = setup_published(tmp_path)
    paths = sync_topic_markdown(repo, topic.id)
    note = Path(paths["note"]).read_text(encoding="utf-8")
    topic_map = Path(paths["map"]).read_text(encoding="utf-8")
    headings = [line[3:] for line in note.splitlines() if line.startswith("## ")]
    assert headings == TITLES
    assert len(headings) == 15
    assert "类型：观点" in note
    assert "来源：[《教材》·第 1 章]" in note
    assert "内容" in note
    assert note.count("```mermaid") == 2
    assert (
        len(
            [
                line
                for line in Path(paths["cards"]).read_text(encoding="utf-8").splitlines()
                if line.startswith("## ")
            ]
        )
        == 8
    )
    assert "../教材/教材/01-章节/intensive-note.md" in topic_map
    assert "<!-- topic-id:" in topic_map


def test_real_topic_sync_and_delete_complete_without_abba_deadlock(tmp_path, monkeypatch):
    repo, topic, _ = setup_published(tmp_path)
    sync_topic_markdown(repo, topic.id)
    sync_has_flock = threading.Event()
    delete_attempts_flock = threading.Event()
    errors = []
    real_flock = topic_markdown_sync.fcntl.flock

    def recording_flock(fd, operation):
        if (
            threading.current_thread().name == "topic-delete"
            and operation == topic_markdown_sync.fcntl.LOCK_EX
        ):
            delete_attempts_flock.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(topic_markdown_sync.fcntl, "flock", recording_flock)

    def fence():
        sync_has_flock.set()
        assert delete_attempts_flock.wait(timeout=3)
        repo.get_topic(topic.id)

    def run_sync():
        try:
            sync_topic_markdown(repo, topic.id, fence=fence)
        except BaseException as exc:
            errors.append(exc)

    def run_delete():
        try:
            delete_unpublished_topic(repo, topic.id)
        except ValueError:
            pass
        except BaseException as exc:
            errors.append(exc)

    syncing = threading.Thread(target=run_sync, name="topic-sync", daemon=True)
    deleting = threading.Thread(target=run_delete, name="topic-delete", daemon=True)
    syncing.start()
    assert sync_has_flock.wait(timeout=3)
    deleting.start()
    syncing.join(timeout=3)
    deleting.join(timeout=3)

    assert not syncing.is_alive()
    assert not deleting.is_alive()
    assert errors == []


def test_delete_missing_parent_and_target_serializes_with_real_sync_creation(tmp_path, monkeypatch):
    repo, topic, _ = setup_published(tmp_path)
    topics_root = tmp_path / "out" / "课程主题"
    assert not topics_root.exists()
    target_observed_missing = threading.Event()
    allow_delete_to_continue = threading.Event()
    errors = []
    delete_conflicted = threading.Event()

    def pause_before_target_open(phase, parent_fd, topic_fd, name):
        if phase == "before_target_open":
            assert topic_fd == -1
            assert name not in os.listdir(parent_fd)
            target_observed_missing.set()
            assert allow_delete_to_continue.wait(timeout=3)

    monkeypatch.setattr(topic_markdown_sync, "_delete_race_hook", pause_before_target_open)

    def run_delete():
        try:
            delete_unpublished_topic(repo, topic.id)
        except ValueError:
            delete_conflicted.set()
        except BaseException as exc:
            errors.append(exc)

    def run_sync():
        try:
            sync_topic_markdown(repo, topic.id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            allow_delete_to_continue.set()

    deleting = threading.Thread(target=run_delete, name="missing-target-delete", daemon=True)
    deleting.start()
    assert target_observed_missing.wait(timeout=3)
    syncing = threading.Thread(target=run_sync, name="missing-target-sync", daemon=True)
    syncing.start()
    syncing.join(timeout=3)
    deleting.join(timeout=3)

    assert not syncing.is_alive()
    assert not deleting.is_alive()
    assert errors == []
    assert delete_conflicted.is_set()
    assert repo.get_topic(topic.id).status == "COMPLETED"
    assert (topics_root / "01-主题" / "topic-map.md").exists()


def test_missing_target_published_rejection_removes_placeholder_and_sync_can_continue(tmp_path):
    repo, topic, _ = setup_published(tmp_path)
    target = tmp_path / "out" / "课程主题" / "01-主题"

    with pytest.raises(ValueError, match="published output"):
        delete_unpublished_topic(repo, topic.id)

    assert not target.exists()
    assert Path(sync_topic_markdown(repo, topic.id)["map"]).exists()


def test_missing_target_db_delete_failure_removes_placeholder_and_map_sync_retries(
    tmp_path, monkeypatch
):
    repo, topic, _ = setup_published(tmp_path)
    repo.conn.execute("DELETE FROM wb_topic_note_blocks WHERE topic_id = ?", (topic.id,))
    repo.conn.execute("DELETE FROM wb_topic_cards WHERE topic_id = ?", (topic.id,))
    repo.conn.commit()
    target = tmp_path / "out" / "课程主题" / "01-主题"
    original_delete = repo.delete_topic_guarded

    def fail_delete(topic_id):
        raise OSError("forced database delete failure")

    monkeypatch.setattr(repo, "delete_topic_guarded", fail_delete)
    with pytest.raises(TopicMarkdownDeleteError, match="placeholder cleanup failed"):
        delete_unpublished_topic(repo, topic.id)

    assert not target.exists()
    monkeypatch.setattr(repo, "delete_topic_guarded", original_delete)
    assert sync_topic_map_markdown(repo, topic.id).exists()


def test_missing_target_placeholder_preserves_concurrent_user_content(tmp_path, monkeypatch):
    repo, topic, _ = setup_published(tmp_path)
    repo.conn.execute("DELETE FROM wb_topic_note_blocks WHERE topic_id = ?", (topic.id,))
    repo.conn.execute("DELETE FROM wb_topic_cards WHERE topic_id = ?", (topic.id,))
    repo.conn.commit()
    target = tmp_path / "out" / "课程主题" / "01-主题"

    def inject_content(phase, parent_fd, topic_fd, name):
        if phase == "placeholder_locked":
            fd = os.open(
                "user-race.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=topic_fd
            )
            os.write(fd, b"keep me")
            os.close(fd)

    monkeypatch.setattr(topic_markdown_sync, "_delete_race_hook", inject_content)

    with pytest.raises(ValueError, match="placeholder"):
        delete_unpublished_topic(repo, topic.id)

    assert repo.get_topic(topic.id) is not None
    assert (target / "user-race.md").read_bytes() == b"keep me"


def test_delete_rejects_existing_outer_transaction_without_changing_db_or_files(tmp_path):
    repo, topic, _ = setup_published(tmp_path)
    target = Path(sync_topic_markdown(repo, topic.id)["map"]).parent
    original_map = (target / "topic-map.md").read_bytes()
    repo.conn.execute("BEGIN")

    with pytest.raises(ValueError, match="outermost transaction"):
        delete_unpublished_topic(repo, topic.id)

    repo.conn.rollback()
    assert repo.get_topic(topic.id) is not None
    assert target.is_dir()
    assert (target / "topic-map.md").read_bytes() == original_map


def test_delete_rejects_outer_transaction_and_removes_missing_target_placeholder(tmp_path):
    repo, topic, _ = setup_published(tmp_path)
    target = tmp_path / "out" / "课程主题" / "01-主题"
    assert not target.exists()
    repo.conn.execute("BEGIN")

    with pytest.raises(ValueError, match="outermost transaction"):
        delete_unpublished_topic(repo, topic.id)

    assert not target.exists()
    repo.conn.rollback()
    assert repo.get_topic(topic.id) is not None


def test_invalid_blocks_or_refs_do_not_overwrite_existing_topic_note(tmp_path):
    repo, topic, _ = setup_published(tmp_path)
    note = Path(sync_topic_markdown(repo, topic.id)["note"])
    old = note.read_text(encoding="utf-8")
    repo.conn.execute(
        "DELETE FROM wb_topic_note_blocks WHERE topic_id = ? AND kind = ?", (topic.id, "overview")
    )
    repo.conn.commit()
    with pytest.raises(ValueError, match="fourteen"):
        sync_topic_markdown(repo, topic.id)
    assert note.read_text(encoding="utf-8") == old

    repo.replace_topic_note_blocks(
        topic.id,
        {
            kind: "flowchart TD\nA-->B" if kind.endswith("mermaid") else kind
            for kind in FIXED_TOPIC_KINDS
        },
    )
    repo.conn.execute(
        "UPDATE wb_topic_cards SET source_refs_json = ? WHERE topic_id = ?",
        (json.dumps("bad"), topic.id),
    )
    repo.conn.commit()
    with pytest.raises(ValueError, match="source refs"):
        sync_topic_markdown(repo, topic.id)
    assert note.read_text(encoding="utf-8") == old


def test_topic_card_refs_must_match_current_mapping_with_duplicate_title_suffixes(tmp_path):
    repo, topic, first_chapter = setup_published(tmp_path)
    second_source = repo.create_source(topic.course_id, "main", "/tmp/second.pdf", "教材")
    second_chapter = repo.create_chapter(
        topic.course_id, second_source.id, 0, "第二来源章节", str(tmp_path / "raw.md")
    )
    outside_source = repo.create_source(topic.course_id, "main", "/tmp/outside.pdf", "教材")
    outside_chapter = repo.create_chapter(
        topic.course_id, outside_source.id, 0, "未映射章节", str(tmp_path / "raw.md")
    )
    repo.replace_topic_chapters(topic.id, [first_chapter.id, second_chapter.id])
    repo.upsert_run(first_chapter.id, "review", "old", "FAILED", "", "", "failed")
    repo.upsert_run(second_chapter.id, "review", "old", "DONE", "", "", "old", stale=True)
    valid = ["[《教材》·第 1 章]", "[《教材（2）》·第 1 章]"]
    repo.conn.execute(
        "UPDATE wb_topic_cards SET source_refs_json = ? WHERE topic_id = ?",
        (json.dumps([valid[1], valid[1]], ensure_ascii=False), topic.id),
    )
    repo.conn.commit()
    cards_path = Path(sync_topic_markdown(repo, topic.id)["cards"])
    assert cards_path.read_text(encoding="utf-8").count(valid[1]) == 16

    old = cards_path.read_text(encoding="utf-8")
    invalid_refs = [
        "[《教材（3）》·第 1 章]",  # same-title source exists, but is outside this topic
        "[《教材》·第 2 章]",  # chapter is not mapped
        "[《教材（4）》·第 1 章]",  # unknown suffix
    ]
    for invalid in invalid_refs:
        repo.conn.execute(
            "UPDATE wb_topic_cards SET source_refs_json = ? WHERE topic_id = ?",
            (json.dumps([invalid], ensure_ascii=False), topic.id),
        )
        repo.conn.commit()
        with pytest.raises(ValueError, match="unknown source ref"):
            sync_topic_markdown(repo, topic.id)
        assert cards_path.read_text(encoding="utf-8") == old

    assert outside_chapter.id not in {chapter.id for chapter in repo.list_topic_chapters(topic.id)}


@pytest.mark.parametrize("foreign_marker", ["", "<!-- topic-id: other -->"])
def test_preexisting_desired_topic_directory_is_never_overwritten(tmp_path, foreign_marker):
    repo, topic, _ = setup_published(tmp_path)
    desired = tmp_path / "out" / "课程主题" / "01-主题"
    desired.mkdir(parents=True)
    user_file = desired / "topic-map.md"
    user_file.write_text(f"{foreign_marker}\n用户内容", encoding="utf-8")

    with pytest.raises(FileExistsError, match="target directory already exists"):
        sync_topic_markdown(repo, topic.id)
    assert user_file.read_text(encoding="utf-8") == f"{foreign_marker}\n用户内容"


def test_topic_bundle_rolls_back_all_files_on_second_replace_failure(tmp_path, monkeypatch):
    repo, topic, _ = setup_published(tmp_path)
    paths = sync_topic_markdown(repo, topic.id)
    old = {key: Path(path).read_text(encoding="utf-8") for key, path in paths.items()}
    original = topic_markdown_sync.os.replace
    replacements = 0

    def fail_second_target(src, dst, *args, **kwargs):
        nonlocal replacements
        if str(dst) in {"topic-map.md", "intensive-note.md", "cards.md"}:
            replacements += 1
            if replacements == 2:
                raise OSError("second replace failed")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(topic_markdown_sync.os, "replace", fail_second_target)
    with pytest.raises(OSError, match="second replace failed"):
        sync_topic_markdown(repo, topic.id)
    assert {key: Path(path).read_text(encoding="utf-8") for key, path in paths.items()} == old


def test_topic_bundle_recovers_keyboard_interrupt_and_redacts_runs(tmp_path, monkeypatch):
    repo, topic, _ = setup_published(tmp_path)
    run = repo.create_topic_run(topic.id, "review", "fingerprint")
    repo.finish_topic_run(
        run.id,
        "FAILED",
        error="正常中文 /Users/a C:\\x file:///tmp/x sk-abcdefghijklmnopqrstuvwxyz",
    )
    paths = sync_topic_markdown(repo, topic.id)
    old = {key: Path(path).read_text(encoding="utf-8") for key, path in paths.items()}
    original = topic_markdown_sync.os.replace

    def interrupt_target(src, dst, *args, **kwargs):
        if str(dst) == "intensive-note.md":
            raise KeyboardInterrupt
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(topic_markdown_sync.os, "replace", interrupt_target)
    with pytest.raises(KeyboardInterrupt):
        sync_topic_markdown(repo, topic.id)
    assert {key: Path(path).read_text(encoding="utf-8") for key, path in paths.items()} == old
    run_text = next((Path(paths["note"]).parent / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "正常中文" in run_text and "/Users/" not in run_text and "sk-" not in run_text


@pytest.mark.parametrize("level", ["课程主题", "topic"])
def test_topic_sync_rejects_symlink_escape(tmp_path, level):
    repo, topic, _ = setup_published(tmp_path)
    root = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    if level == "课程主题":
        (root / "课程主题").symlink_to(outside, target_is_directory=True)
    else:
        (root / "课程主题").mkdir()
        (root / "课程主题" / "01-主题").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        sync_topic_markdown(repo, topic.id)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("phase", ["PREPARED", "COMMITTED"])
def test_bundle_journal_recovers_crash_and_cleans_secret_backup(tmp_path, phase):
    directory = tmp_path / "bundle"
    directory.mkdir()
    target = directory / "cards.md"
    target.write_text("旧secret", encoding="utf-8")
    backup = ".pdf2md-crash.bundle-backup"
    temp = ".pdf2md-crash.bundle-tmp"
    (directory / backup).hardlink_to(target)
    (directory / temp).write_text("新内容", encoding="utf-8")
    os.replace(directory / temp, target)
    journal = {
        "phase": phase,
        "entries": [{"target": "cards.md", "temp": temp, "backup": backup, "existed": True}],
    }
    (directory / ".pdf2md-bundle-journal.json").write_text(json.dumps(journal), encoding="utf-8")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        recover_atomic_bundle(fd)
    finally:
        os.close(fd)
    assert target.read_text(encoding="utf-8") == ("旧secret" if phase == "PREPARED" else "新内容")
    assert not (directory / backup).exists()
    assert not (directory / ".pdf2md-bundle-journal.json").exists()


def test_topic_first_bundle_failure_keeps_owner_and_retry_succeeds(tmp_path, monkeypatch):
    repo, topic, _ = setup_published(tmp_path)
    original = topic_markdown_sync.atomic_write_bundle_fd
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(topic_markdown_sync, "atomic_write_bundle_fd", fail_once)
    with pytest.raises(OSError, match="disk full"):
        sync_topic_markdown(repo, topic.id)
    topic_dir = tmp_path / "out" / "课程主题" / "01-主题"
    assert (topic_dir / ".pdf2md-owner").read_text(encoding="utf-8") == (f"topic:{topic.id}\n")
    assert Path(sync_topic_markdown(repo, topic.id)["map"]).exists()


@pytest.mark.parametrize("kind, entity_id", [("chapter", "other"), ("topic", "other")])
def test_fake_owner_never_authorizes_topic_directory(tmp_path, kind, entity_id):
    repo, topic, _ = setup_published(tmp_path)
    desired = tmp_path / "out" / "课程主题" / "01-主题"
    desired.mkdir(parents=True)
    (desired / ".pdf2md-owner").write_text(f"{kind}:{entity_id}\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sync_topic_markdown(repo, topic.id)


@pytest.mark.parametrize("failure", ["oserror", "interrupt", "fence"])
def test_topic_failures_do_not_leak_file_descriptors(tmp_path, monkeypatch, failure):
    fd_dir = Path("/dev/fd")
    if not fd_dir.exists():
        pytest.skip("fd directory is unavailable")
    repo, topic, _ = setup_published(tmp_path)
    baseline = len(list(fd_dir.iterdir()))

    def fail(*args, **kwargs):
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "fence":
            kwargs["fence"]()
        raise OSError("disk full")

    monkeypatch.setattr(topic_markdown_sync, "atomic_write_bundle_fd", fail)
    for _ in range(40):
        fence = (
            (lambda: (_ for _ in ()).throw(ValueError("owner lost")))
            if failure == "fence"
            else None
        )
        expected = (
            ValueError
            if failure == "fence"
            else KeyboardInterrupt
            if failure == "interrupt"
            else OSError
        )
        with pytest.raises(expected):
            sync_topic_markdown(repo, topic.id, fence=fence)
    gc.collect()
    assert len(list(fd_dir.iterdir())) <= baseline
