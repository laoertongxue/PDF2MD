import asyncio
import fcntl
import os
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api.deps import get_scheduler
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench import topic_markdown_sync
from parsing_core.workbench.codex_cli import CodexCliError
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_markdown_sync import (
    merge_unpublished_topics,
    sync_topic_map_markdown,
)


def client(tmp_path):
    db_path = tmp_path / "serve.db"

    def factory():
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        return Orchestrator(
            Repository(conn), FsLayout(base_dir=str(tmp_path / "fs")), StubLLMClient(), str(db_path)
        )

    return TestClient(build_app(factory))


def setup_course(client, tmp_path):
    root = tmp_path / "course"
    root.mkdir()
    course = client.post(
        "/api/workbench/courses",
        json={"title": "Strategy", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## One\nA\n## Two\nB", encoding="utf-8")
    source = client.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "Book"},
    ).json()
    chapters = client.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()
    for chapter in chapters:
        client.post(f"/api/workbench/chapters/{chapter['id']}/confirm")
    return course, chapters


def test_topic_models_forbid_extra_and_patch_requires_a_field(tmp_path):
    c = client(tmp_path)
    course, _ = setup_course(c, tmp_path)
    assert (
        c.post(f"/api/workbench/courses/{course['id']}/topics", json={"unknown": True}).status_code
        == 422
    )
    topic = c.post(f"/api/workbench/courses/{course['id']}/topics", json={"title": "T"}).json()
    assert c.patch(f"/api/workbench/topics/{topic['id']}", json={}).status_code == 422


def test_create_list_reorder_and_confirm_topics(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    first = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "First", "chapter_ids": [chapters[0]["id"]]},
    )
    assert first.status_code == 200
    second = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Second", "chapter_ids": [chapters[1]["id"]]},
    ).json()
    first = first.json()
    assert first["seq"] == 0
    assert first["chapter_ids"] == [chapters[0]["id"]]
    assert first["sync_status"] == "SYNCED"
    assert (tmp_path / "course" / "课程主题" / "01-First" / "topic-map.md").exists()

    reordered = c.put(
        f"/api/workbench/courses/{course['id']}/topics/reorder",
        json={"topic_ids": [second["id"], first["id"]]},
    )
    assert [item["id"] for item in reordered.json()] == [second["id"], first["id"]]
    assert all(item["sync_status"] == "SYNCED" for item in reordered.json())
    assert (tmp_path / "course" / "课程主题" / "01-Second" / "topic-map.md").exists()
    assert not (tmp_path / "course" / "课程主题" / "02-Second").exists()
    confirmed = c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    assert confirmed.status_code == 200
    assert all(item["confirmed"] for item in confirmed.json())
    assert all(item["sync_status"] == "SYNCED" for item in confirmed.json())


def test_confirm_is_atomic_when_any_topic_has_no_mapping(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    mapped = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Mapped", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics", json={"title": "Empty"})
    assert c.post(f"/api/workbench/courses/{course['id']}/topics/confirm").status_code == 409
    assert c.get(f"/api/workbench/topics/{mapped['id']}").json()["confirmed"] is False


def test_merge_topics_is_atomic_and_preserves_union_mapping(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    first = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "A", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    second = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "B", "chapter_ids": [chapters[1]["id"]]},
    ).json()

    response = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [first["id"], second["id"]], "title": "Merged"},
    )

    assert response.status_code == 200, response.json()
    merged = response.json()
    assert merged["title"] == "Merged"
    assert merged["chapter_ids"] == [chapters[0]["id"], chapters[1]["id"]]
    assert merged["confirmed"] is False
    assert merged["status"] == "DRAFT"
    assert c.get(f"/api/workbench/topics/{first['id']}").status_code == 404
    assert c.get(f"/api/workbench/topics/{second['id']}").status_code == 404
    topic_root = tmp_path / "course" / "课程主题"
    assert not (topic_root / "01-A").exists()
    assert not (topic_root / "02-B").exists()
    assert not list(topic_root.glob(".pdf2md-merge-*"))


def test_merge_protects_unknown_user_files_without_database_changes(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    user_file = tmp_path / "course" / "课程主题" / "01-A" / "notes.md"
    user_file.write_text("keep", encoding="utf-8")
    response = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [item["id"] for item in topics], "title": "Merged"},
    )
    assert response.status_code == 409
    assert user_file.read_text(encoding="utf-8") == "keep"
    assert [
        item["id"] for item in c.get(f"/api/workbench/courses/{course['id']}/topics").json()
    ] == [item["id"] for item in topics]


def test_merge_second_detach_failure_restores_first_directory(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]

    def fail_second(phase, parent_fd, topic_fd, name):
        if phase == "before_detach" and name == "02-B":
            raise OSError("detach failed")

    monkeypatch.setattr(topic_markdown_sync, "_merge_file_hook", fail_second)
    with pytest.raises(OSError, match="detach failed"):
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/merge",
            json={"topic_ids": [item["id"] for item in topics], "title": "Merged"},
        )
    root = tmp_path / "course" / "课程主题"
    assert (root / "01-A").is_dir()
    assert (root / "02-B").is_dir()
    assert not list(root.glob(".pdf2md-merge-*"))


def test_merge_rejects_invalid_sets_cross_course_and_published_topics(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = c.post(
        "/api/workbench/courses",
        json={"title": "Other", "description": "", "root_dir": str(other_root)},
    ).json()
    first = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "A", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    second = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "B", "chapter_ids": [chapters[1]["id"]]},
    ).json()
    foreign = c.post(
        f"/api/workbench/courses/{other['id']}/topics", json={"title": "Foreign"}
    ).json()
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    repo.replace_topic_note_blocks(first["id"], {"summary": "published"})

    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/merge",
            json={"topic_ids": [first["id"]], "title": "X"},
        ).status_code
        == 422
    )
    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/merge",
            json={"topic_ids": [second["id"], foreign["id"]], "title": "X"},
        ).status_code
        == 409
    )
    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/merge",
            json={"topic_ids": [first["id"], second["id"]], "title": "X"},
        ).status_code
        == 409
    )
    assert {
        item["id"] for item in c.get(f"/api/workbench/courses/{course['id']}/topics").json()
    } == {first["id"], second["id"]}


def test_merge_failure_rolls_back_every_write(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    monkeypatch.setattr(
        WorkbenchRepository,
        "_mark_topic_markdown_pending",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fault")),
    )

    with pytest.raises(RuntimeError, match="fault"):
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/merge",
            json={"topic_ids": [item["id"] for item in topics], "title": "Merged"},
        )
    stored = c.get(f"/api/workbench/courses/{course['id']}/topics").json()
    assert [item["id"] for item in stored] == [item["id"] for item in topics]
    assert [item["chapter_ids"] for item in stored] == [[chapters[0]["id"]], [chapters[1]["id"]]]
    root = tmp_path / "course" / "课程主题"
    assert (root / "01-A").is_dir()
    assert (root / "02-B").is_dir()


@pytest.mark.parametrize("outer_transaction", ["begin", "dml"])
def test_merge_rejects_outer_transactions_without_db_or_fs_changes(tmp_path, outer_transaction):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    root = tmp_path / "course" / "课程主题"
    if outer_transaction == "begin":
        repo.conn.execute("BEGIN")
    else:
        shutil.rmtree(root / "01-A")
        shutil.rmtree(root / "02-B")
        repo.conn.execute(
            "UPDATE wb_courses SET updated_at = updated_at WHERE id = ?", (course["id"],)
        )

    with pytest.raises(ValueError, match="outermost transaction"):
        merge_unpublished_topics(
            repo, course["id"], [item["id"] for item in topics], title="Merged"
        )

    assert (root / "01-A").is_dir() is (outer_transaction == "begin")
    assert (root / "02-B").is_dir() is (outer_transaction == "begin")
    assert [item.id for item in repo.list_topics(course["id"])] == [item["id"] for item in topics]
    repo.conn.rollback()


def test_corrupt_hidden_merge_journal_without_lock_does_not_leak_fds(tmp_path):
    c = client(tmp_path)
    course, _ = setup_course(c, tmp_path)
    root = tmp_path / "course" / "课程主题"
    root.mkdir(exist_ok=True)
    hidden = root / f"{topic_markdown_sync.MERGE_JOURNAL_PREFIX}{'f' * 32}-corrupt"
    hidden.mkdir()
    (hidden / ".pdf2md-owner").write_text(f"topic:{'f' * 32}\n", encoding="utf-8")
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    try:
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            topic_markdown_sync._cleanup_committed_merge_journals(repo, parent_fd)
        assert len(os.listdir("/dev/fd")) == before
        assert hidden.is_dir()
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("missing_names", [{"01-A", "02-B"}, {"02-B"}])
def test_merge_missing_and_mixed_topic_directories_leave_no_visible_orphans(
    tmp_path, missing_names
):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    root = tmp_path / "course" / "课程主题"
    for name in missing_names:
        shutil.rmtree(root / name)

    response = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [item["id"] for item in topics], "title": "Merged"},
    )

    assert response.status_code == 200, response.json()
    assert not (root / "01-A").exists()
    assert not (root / "02-B").exists()
    assert not list(root.glob(".pdf2md-merge-*"))


def test_merge_placeholder_cleanup_failure_keeps_hidden_retryable_journal(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    root = tmp_path / "course" / "课程主题"
    shutil.rmtree(root / "01-A")
    shutil.rmtree(root / "02-B")

    def fail_placeholder_cleanup(phase, parent_fd, topic_fd, name):
        if phase == "before_placeholder_cleanup":
            raise OSError("defer placeholder cleanup")

    monkeypatch.setattr(topic_markdown_sync, "_merge_file_hook", fail_placeholder_cleanup)
    response = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [item["id"] for item in topics], "title": "Merged"},
    )
    assert response.status_code == 200
    assert not (root / "01-A").exists()
    assert not (root / "02-B").exists()
    assert len(list(root.glob(".pdf2md-merge-*"))) == 2

    monkeypatch.setattr(topic_markdown_sync, "_merge_file_hook", lambda *args: None)
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        topic_markdown_sync._cleanup_committed_merge_journals(repo, parent_fd)
    finally:
        os.close(parent_fd)
    assert not list(root.glob(".pdf2md-merge-*"))


def test_merge_missing_target_coordinates_with_real_sync_retry_without_orphans(
    tmp_path, monkeypatch
):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    root = tmp_path / "course" / "课程主题"
    shutil.rmtree(root / "01-A")
    observed_missing = threading.Event()
    allow_merge = threading.Event()
    errors = []
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)

    def coordinate(phase, parent_fd, topic_fd, name):
        if phase == "before_target_prepare" and name == "01-A":
            assert name not in os.listdir(parent_fd)
            observed_missing.set()
            assert allow_merge.wait(timeout=3)

    monkeypatch.setattr(topic_markdown_sync, "_merge_file_hook", coordinate)

    def run_merge():
        try:
            merge_unpublished_topics(
                repo,
                course["id"],
                [item["id"] for item in topics],
                title="Merged",
            )
        except BaseException as exc:
            errors.append(exc)

    merging = threading.Thread(target=run_merge, daemon=True)
    merging.start()
    assert observed_missing.wait(timeout=3)
    sync_topic_map_markdown(repo, topics[0]["id"])
    allow_merge.set()
    merging.join(timeout=3)

    assert not merging.is_alive()
    assert errors == []
    assert repo.get_topic(topics[0]["id"]) is None
    assert repo.get_topic(topics[1]["id"]) is None
    assert not (root / "01-A").exists()
    assert not (root / "02-B").exists()
    assert not list(root.glob(".pdf2md-merge-*"))


def test_merge_cleanup_failure_keeps_hidden_journal_and_next_merge_cleans_it(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)

    def create_pair(prefix):
        return [
            c.post(
                f"/api/workbench/courses/{course['id']}/topics",
                json={"title": f"{prefix}{index}", "chapter_ids": [chapter["id"]]},
            ).json()
            for index, chapter in enumerate(chapters, 1)
        ]

    first_pair = create_pair("A")
    failed = {"done": False}

    def fail_cleanup(phase, parent_fd, topic_fd, name):
        if phase == "before_hidden_cleanup" and not failed["done"]:
            failed["done"] = True
            raise OSError("cleanup deferred")

    monkeypatch.setattr(topic_markdown_sync, "_merge_file_hook", fail_cleanup)
    response = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [item["id"] for item in first_pair], "title": "First merged"},
    )
    assert response.status_code == 200
    root = tmp_path / "course" / "课程主题"
    assert not any((root / f"0{index}-A{index}").exists() for index in (1, 2))
    assert list(root.glob(".pdf2md-merge-*"))

    monkeypatch.setattr(topic_markdown_sync, "_merge_file_hook", lambda *args: None)
    second_pair = create_pair("B")
    response = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [item["id"] for item in second_pair], "title": "Second merged"},
    )
    assert response.status_code == 200
    assert not list(root.glob(".pdf2md-merge-*"))


def test_merge_and_split_surface_failed_sync_and_allow_retry(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topics = [
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": title, "chapter_ids": [chapter["id"]]},
        ).json()
        for title, chapter in zip(["A", "B"], chapters, strict=True)
    ]
    from parsing_core.workbench import topic_pipeline

    real_sync = topic_pipeline.sync_topic_map_markdown
    monkeypatch.setattr(
        topic_pipeline,
        "sync_topic_map_markdown",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("sync failed")),
    )
    merged = c.post(
        f"/api/workbench/courses/{course['id']}/topics/merge",
        json={"topic_ids": [item["id"] for item in topics], "title": "Merged"},
    ).json()
    assert merged["sync_status"] == "FAILED"
    split = c.post(
        f"/api/workbench/topics/{merged['id']}/split",
        json={"title": "Split", "new_chapter_ids": [chapters[1]["id"]]},
    ).json()
    assert [item["sync_status"] for item in split] == ["FAILED", "FAILED"]
    monkeypatch.setattr(topic_pipeline, "sync_topic_map_markdown", real_sync)
    assert (
        c.post(f"/api/workbench/topics/{split[0]['id']}/sync/retry").json()["sync_status"]
        == "SYNCED"
    )


def test_split_topic_is_atomic_keeps_mapping_and_stales_published_original(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    original = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Original", "chapter_ids": [item["id"] for item in chapters]},
    ).json()
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    repo.replace_topic_note_blocks(original["id"], {"summary": "published"})

    response = c.post(
        f"/api/workbench/topics/{original['id']}/split",
        json={"title": "New", "new_chapter_ids": [chapters[1]["id"]]},
    )

    assert response.status_code == 200, response.json()
    old, new = response.json()
    assert old["id"] == original["id"]
    assert old["chapter_ids"] == [chapters[0]["id"]]
    assert old["status"] == "STALE"
    assert old["stale_reason"] == "topic chapter mapping changed by split"
    assert new["chapter_ids"] == [chapters[1]["id"]]
    assert new["status"] == "DRAFT"
    assert repo.list_topic_note_blocks(original["id"])


def test_split_rejects_empty_non_subset_and_non_proper_subset_without_changes(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    original = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Original", "chapter_ids": [item["id"] for item in chapters]},
    ).json()
    base = f"/api/workbench/topics/{original['id']}/split"
    assert (
        c.post(base, json={"title": "", "new_chapter_ids": [chapters[0]["id"]]}).status_code == 422
    )
    assert c.post(base, json={"title": "New", "new_chapter_ids": []}).status_code == 422
    assert (
        c.post(
            base, json={"title": "New", "new_chapter_ids": [item["id"] for item in chapters]}
        ).status_code
        == 400
    )
    assert c.post(base, json={"title": "New", "new_chapter_ids": ["missing"]}).status_code == 400
    assert c.get(f"/api/workbench/topics/{original['id']}").json()["chapter_ids"] == [
        item["id"] for item in chapters
    ]


def test_split_failure_rolls_back_new_topic_and_original_mapping(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    original = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Original", "chapter_ids": [item["id"] for item in chapters]},
    ).json()
    monkeypatch.setattr(
        WorkbenchRepository,
        "_mark_topic_markdown_pending",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fault")),
    )
    with pytest.raises(RuntimeError, match="fault"):
        c.post(
            f"/api/workbench/topics/{original['id']}/split",
            json={"title": "New", "new_chapter_ids": [chapters[1]["id"]]},
        )
    stored = c.get(f"/api/workbench/courses/{course['id']}/topics").json()
    assert len(stored) == 1
    assert stored[0]["id"] == original["id"]
    assert stored[0]["chapter_ids"] == [item["id"] for item in chapters]


def test_topic_patch_mapping_delete_and_result_views(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Original", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    assert (
        c.patch(f"/api/workbench/topics/{topic['id']}", json={"title": "Changed"}).json()["title"]
        == "Changed"
    )
    mapped = c.put(
        f"/api/workbench/topics/{topic['id']}/chapters",
        json={"chapter_ids": [chapters[1]["id"]]},
    ).json()
    assert mapped["chapter_ids"] == [chapters[1]["id"]]
    assert c.get(f"/api/workbench/topics/{topic['id']}/note-blocks").json() == []
    assert c.get(f"/api/workbench/topics/{topic['id']}/cards").json() == []
    assert c.get(f"/api/workbench/topics/{topic['id']}/runs").json() == []
    deleted = c.delete(f"/api/workbench/topics/{topic['id']}")
    assert deleted.status_code == 204, deleted.json()
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 404


@pytest.mark.asyncio
async def test_topic_request_waiting_for_repo_lock_does_not_block_health(tmp_path):
    test_client = client(tmp_path)
    course, _ = setup_course(test_client, tmp_path)
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_repo_lock():
        with repo._connection_lock:
            lock_held.set()
            assert release_lock.wait(timeout=3)

    holder = threading.Thread(target=hold_repo_lock)
    holder.start()
    assert await asyncio.to_thread(lock_held.wait, 3)
    timeout_release = threading.Timer(1.0, release_lock.set)
    timeout_release.start()

    transport = ASGITransport(app=test_client.app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        started_at = time.monotonic()
        topic_request = asyncio.create_task(
            async_client.get(f"/api/workbench/courses/{course['id']}/topics")
        )
        health_request = asyncio.create_task(async_client.get("/health"))
        try:
            health = await health_request
            assert time.monotonic() - started_at < 0.5
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert not topic_request.done()
        finally:
            release_lock.set()
        assert (await topic_request).status_code == 200
    timeout_release.cancel()
    holder.join(timeout=3)


def test_delete_unpublished_topic_removes_generated_directory_and_allows_recreate(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Reusable", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    topic_dir = tmp_path / "course" / "课程主题" / "01-Reusable"
    assert topic_dir.is_dir()
    assert {entry.name for entry in topic_dir.iterdir()} == {
        ".pdf2md-bundle.lock",
        ".pdf2md-owner",
        "topic-map.md",
    }

    deleted = c.delete(f"/api/workbench/topics/{topic['id']}")
    assert deleted.status_code == 204, deleted.json()
    assert not topic_dir.exists()

    recreated = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Reusable", "chapter_ids": [chapters[0]["id"]]},
    )
    assert recreated.status_code == 200
    assert recreated.json()["sync_status"] == "SYNCED"
    assert topic_dir.is_dir()


def test_delete_unpublished_topic_refuses_directory_with_unknown_user_file(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Protected", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    topic_dir = tmp_path / "course" / "课程主题" / "01-Protected"
    user_file = topic_dir / "my-notes.md"
    user_file.write_text("keep me", encoding="utf-8")

    response = c.delete(f"/api/workbench/topics/{topic['id']}")

    assert response.status_code == 409
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200
    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert (topic_dir / ".pdf2md-owner").exists()
    assert (topic_dir / "topic-map.md").exists()


def test_delete_unpublished_topic_restores_directory_when_cleanup_fails(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Retryable", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    topic_dir = tmp_path / "course" / "课程主题" / "01-Retryable"

    def fail_cleanup(phase, parent_fd, topic_fd, name):
        if phase == "before_unlink":
            raise OSError("simulated cleanup failure")

    monkeypatch.setattr(topic_markdown_sync, "_delete_race_hook", fail_cleanup)

    response = c.delete(f"/api/workbench/topics/{topic['id']}")

    assert response.status_code == 507
    assert response.json()["detail"] == "topic directory cleanup failed"
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200
    assert {entry.name for entry in topic_dir.iterdir()} == {
        ".pdf2md-bundle.lock",
        ".pdf2md-owner",
        "topic-map.md",
    }


def test_delete_unpublished_topic_rejects_symlinked_owner_marker(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Symlink", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    topic_dir = tmp_path / "course" / "课程主题" / "01-Symlink"
    outside = tmp_path / "outside-owner"
    outside.write_text(f"topic:{topic['id']}\n", encoding="utf-8")
    (topic_dir / ".pdf2md-owner").unlink()
    (topic_dir / ".pdf2md-owner").symlink_to(outside)

    response = c.delete(f"/api/workbench/topics/{topic['id']}")

    assert response.status_code == 409
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200
    assert outside.read_text(encoding="utf-8") == f"topic:{topic['id']}\n"


def test_delete_unpublished_topic_rejects_directory_identity_replacement(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Replaced", "chapter_ids": [chapters[0]["id"]]},
    ).json()

    def replace_directory(phase, parent_fd, topic_fd, name):
        if phase == "before_revalidate":
            os.rename(name, f"{name}.old", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, dir_fd=parent_fd)

    monkeypatch.setattr(topic_markdown_sync, "_delete_race_hook", replace_directory)

    response = c.delete(f"/api/workbench/topics/{topic['id']}")

    assert response.status_code == 409
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200


def test_delete_unpublished_topic_rejects_concurrent_file_injection(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Injected", "chapter_ids": [chapters[0]["id"]]},
    ).json()

    def inject_file(phase, parent_fd, topic_fd, name):
        if phase == "before_revalidate":
            fd = os.open(
                "user-race.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=topic_fd
            )
            os.write(fd, b"keep race content")
            os.close(fd)

    monkeypatch.setattr(topic_markdown_sync, "_delete_race_hook", inject_file)

    response = c.delete(f"/api/workbench/topics/{topic['id']}")

    assert response.status_code == 409
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200
    assert (
        tmp_path / "course" / "课程主题" / "01-Injected" / "user-race.md"
    ).read_bytes() == b"keep race content"


def test_delete_recovery_never_overwrites_concurrently_recreated_file(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "RestoreRace", "chapter_ids": [chapters[0]["id"]]},
    ).json()

    def recreate_map(phase, parent_fd, topic_fd, name):
        if phase == "after_generated_unlink":
            fd = os.open(
                "topic-map.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=topic_fd
            )
            os.write(fd, b"user replacement")
            os.close(fd)

    monkeypatch.setattr(topic_markdown_sync, "_delete_race_hook", recreate_map)

    response = c.delete(f"/api/workbench/topics/{topic['id']}")

    assert response.status_code == 409
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200
    assert (
        tmp_path / "course" / "课程主题" / "01-RestoreRace" / "topic-map.md"
    ).read_bytes() == b"user replacement"


def test_delete_unpublished_topic_waits_for_bundle_flock(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Locked", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    lock_path = tmp_path / "course" / "课程主题" / "01-Locked" / ".pdf2md-bundle.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    attempted = threading.Event()
    real_flock = fcntl.flock

    def recording_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            attempted.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(topic_markdown_sync.fcntl, "flock", recording_flock)
    result = {}

    def delete_request():
        result["response"] = c.delete(f"/api/workbench/topics/{topic['id']}")

    deleting = threading.Thread(target=delete_request)
    deleting.start()
    assert attempted.wait(timeout=3)
    assert deleting.is_alive()
    real_flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    deleting.join(timeout=3)

    assert not deleting.is_alive()
    assert result["response"].status_code == 204


def test_cards_parse_refs_and_runs_redact_sensitive_content(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    repo.replace_topic_cards(
        topic["id"],
        [
            {
                "card_type": "insight",
                "title": "Card",
                "content": "Body",
                "source_refs_json": ["Book / One"],
            }
        ],
    )
    run = repo.create_topic_run(topic["id"], "alignment", "fingerprint")
    repo.finish_topic_run(
        run.id,
        "FAILED",
        output=f"model output sk-1234567890abcdef at {tmp_path}/output",
        error=f"secret sk-1234567890abcdef at {tmp_path}/private",
    )
    cards = c.get(f"/api/workbench/topics/{topic['id']}/cards").json()
    assert cards[0]["source_refs"] == ["Book / One"]
    returned_run = c.get(f"/api/workbench/topics/{topic['id']}/runs").json()[0]
    assert "sk-1234567890abcdef" not in returned_run["error"]
    assert str(tmp_path) not in returned_run["error"]
    assert "sk-1234567890abcdef" not in returned_run["output"]
    assert str(tmp_path) not in returned_run["output"]


def test_generate_stub_is_deterministic_and_protects_confirmed_topics(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/generate",
            json={"executor": "stub"},
        ).status_code
        == 409
    )
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    generated = c.post(
        f"/api/workbench/courses/{course['id']}/topics/generate",
        json={"executor": "stub"},
    )
    assert generated.status_code == 200
    assert [topic["chapter_ids"] for topic in generated.json()] == [
        [chapter["id"]] for chapter in chapters
    ]
    assert c.post(f"/api/workbench/courses/{course['id']}/topics/confirm").status_code == 200
    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/generate",
            json={"executor": "stub"},
        ).status_code
        == 409
    )


def test_stub_run_publishes_results_and_syncs_markdown(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        assert (
            c.post(
                f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"}
            ).status_code
            == 200
        )
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Fusion", "chapter_ids": [chapter["id"] for chapter in chapters]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")

    result = c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    assert result.status_code == 200
    assert result.json()["status"] == "COMPLETED"
    assert result.json()["sync_status"] == "SYNCED"
    assert len(c.get(f"/api/workbench/topics/{topic['id']}/cards").json()) == 8
    assert c.get(f"/api/workbench/topics/{topic['id']}/runs").json()[-1]["status"] == "COMPLETED"


def test_published_edit_and_mapping_mark_stale_resync_map_and_keep_publication(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Fusion", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    edited = c.patch(f"/api/workbench/topics/{topic['id']}", json={"title": "Renamed"})
    assert edited.status_code == 200
    assert edited.json()["status"] == "STALE"
    assert edited.json()["sync_status"] == "SYNCED"
    renamed = tmp_path / "course" / "课程主题" / "01-Renamed"
    assert "## 核心概念" in (renamed / "intensive-note.md").read_text()
    assert len(c.get(f"/api/workbench/topics/{topic['id']}/cards").json()) == 8
    assert not (tmp_path / "course" / "课程主题" / "01-Fusion").exists()

    mapped = c.put(
        f"/api/workbench/topics/{topic['id']}/chapters",
        json={"chapter_ids": [chapters[1]["id"]]},
    )
    assert mapped.status_code == 200
    assert mapped.json()["status"] == "STALE"
    assert mapped.json()["sync_status"] == "SYNCED"
    assert chapters[1]["title"] in (renamed / "topic-map.md").read_text()
    assert "## 核心概念" in (renamed / "intensive-note.md").read_text()


def test_delete_rejects_topic_with_published_output(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Fusion", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    assert c.delete(f"/api/workbench/topics/{topic['id']}").status_code == 409
    assert c.get(f"/api/workbench/topics/{topic['id']}").status_code == 200


def test_mapping_rejects_chapter_from_another_course(tmp_path):
    c = client(tmp_path)
    first, first_chapters = setup_course(c, tmp_path)
    other_root = tmp_path / "other-course"
    other_root.mkdir()
    other = c.post(
        "/api/workbench/courses",
        json={"title": "Other", "description": "", "root_dir": str(other_root)},
    ).json()
    source_file = other_root / "source.md"
    source_file.write_text("## Foreign\nText")
    source = c.post(
        f"/api/workbench/courses/{other['id']}/sources",
        json={"kind": "main", "file_path": str(source_file), "title": "Other Book"},
    ).json()
    foreign = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]
    topic = c.post(
        f"/api/workbench/courses/{first['id']}/topics",
        json={"title": "T", "chapter_ids": [first_chapters[0]["id"]]},
    ).json()
    assert (
        c.put(
            f"/api/workbench/topics/{topic['id']}/chapters",
            json={"chapter_ids": [foreign["id"]]},
        ).status_code
        == 400
    )


def test_topic_queries_return_404_and_running_conflicts(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/workbench/topics/missing").status_code == 404
    assert c.get("/api/workbench/topics/missing/cards").status_code == 404
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    _, fingerprint = repo.topic_input_snapshot(topic["id"])
    repo.start_topic_generation(topic["id"], fingerprint)
    assert (
        c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"}).status_code
        == 409
    )


def test_generate_deepseek_name_and_hybrid_compatibility_use_deepseek(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    calls = []

    class Executor:
        def run(self, task_key, prompt):
            calls.append(task_key)
            payload = __import__("json").loads(prompt.split("\nINPUT:\n", 1)[1])
            return __import__("json").dumps(
                {
                    "topics": [
                        {
                            "title": "T",
                            "description": "D",
                            "chapter_ids": [item["id"] for item in payload["chapters"]],
                            "reason": "R",
                        }
                    ],
                    "unmapped_chapter_ids": [],
                }
            )

    monkeypatch.setattr(
        "parsing_core.serving.api.routes_topics._deepseek_executor", lambda sch: Executor()
    )
    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/generate", json={"executor": "deepseek"}
        ).status_code
        == 200
    )
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    for item in repo.list_topics(course["id"]):
        repo.delete_topic(item.id)
    assert (
        c.post(
            f"/api/workbench/courses/{course['id']}/topics/generate", json={"executor": "hybrid"}
        ).status_code
        == 200
    )
    assert calls == ["topic_outline", "topic_outline"]


def test_run_hybrid_success_uses_topic_pipeline(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    calls = {"deepseek": [], "codex": []}

    class RecordingExecutor:
        def __init__(self, target):
            self.target = target
            self.stub = StubIntensiveReadingExecutor()

        def run(self, round_key, task_package):
            calls[self.target].append(round_key)
            return self.stub.run(round_key, task_package)

    monkeypatch.setattr(
        "parsing_core.serving.api.routes_topics._deepseek_executor",
        lambda sch: RecordingExecutor("deepseek"),
    )
    monkeypatch.setattr(
        "parsing_core.serving.api.routes_topics.resolve_codex_path", lambda: "/bin/echo"
    )
    monkeypatch.setattr(
        "parsing_core.serving.api.routes_topics.CodexCliExecutor",
        lambda codex_path, run_dir: RecordingExecutor("codex"),
    )
    response = c.post(f"/api/workbench/topics/{topic['id']}/run-hybrid")
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["sync_status"] == "SYNCED"
    assert calls["deepseek"] == [
        "alignment",
        "comparison",
        "plain_cases",
        "framework_application",
        "cards",
    ]
    assert calls["codex"] == ["mermaid", "review"]


def test_run_hybrid_reports_missing_deepseek_key(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    monkeypatch.setattr(
        "parsing_core.serving.api.routes_topics.resolve_codex_path", lambda: "/bin/echo"
    )
    monkeypatch.setattr("parsing_core.serving.api.routes_topics.read_secret", lambda *args: "")
    response = c.post(f"/api/workbench/topics/{topic['id']}/run-hybrid")
    assert response.status_code == 400
    assert response.json()["detail"] == "deepseek api key not configured"


def test_run_hybrid_reports_missing_codex_cli(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()

    def missing():
        raise CodexCliError("missing /private/path")

    monkeypatch.setattr("parsing_core.serving.api.routes_topics.resolve_codex_path", missing)
    response = c.post(f"/api/workbench/topics/{topic['id']}/run-hybrid")
    assert response.status_code == 400
    assert response.json()["detail"] == "codex cli not configured"


def test_model_output_validation_failure_is_400_and_preserves_no_publication(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")

    class Invalid:
        def run(self, round_key, prompt):
            return "not-json"

    monkeypatch.setattr(
        "parsing_core.serving.api.routes_topics._hybrid_executor",
        lambda sch, topic_id: Invalid(),
    )
    response = c.post(f"/api/workbench/topics/{topic['id']}/run-hybrid")
    assert response.status_code == 400
    assert c.get(f"/api/workbench/topics/{topic['id']}/note-blocks").json() == []


def test_fusion_success_with_sync_failure_returns_success_and_retry_uses_507(tmp_path, monkeypatch):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "T", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")

    def disk_full(*args, **kwargs):
        raise OSError("disk full at /private/path")

    monkeypatch.setattr("parsing_core.workbench.topic_pipeline.sync_topic_markdown", disk_full)
    response = c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["sync_status"] == "FAILED"
    assert response.json()["sync_error"] == "topic Markdown sync failed"
    retry = c.post(f"/api/workbench/topics/{topic['id']}/sync/retry")
    assert retry.status_code == 507
    assert retry.json()["detail"] == "topic Markdown sync failed"


def test_recover_rejects_live_lease_and_recovers_expired_lease(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for chapter in chapters:
        c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "Fusion", "chapter_ids": [chapters[0]["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    _, fingerprint = repo.topic_input_snapshot(topic["id"])
    repo.start_topic_generation(topic["id"], fingerprint, lease_ttl=3600)
    assert c.post(f"/api/workbench/topics/{topic['id']}/recover").status_code == 409
    repo.conn.execute(
        "UPDATE wb_topic_generation_leases SET expires_at = 0 WHERE topic_id = ?",
        (topic["id"],),
    )
    repo.conn.commit()
    recovered = c.post(f"/api/workbench/topics/{topic['id']}/recover")
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "FAILED"


def test_list_topics_uses_constant_query_count(tmp_path):
    c = client(tmp_path)
    course, chapters = setup_course(c, tmp_path)
    for index in range(12):
        c.post(
            f"/api/workbench/courses/{course['id']}/topics",
            json={"title": f"Topic {index}", "chapter_ids": [chapters[0]["id"]]},
        )
    conn = get_scheduler()._query_orch.repo.conn
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        response = c.get(f"/api/workbench/courses/{course['id']}/topics")
    finally:
        conn.set_trace_callback(None)
    assert response.status_code == 200
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 5
