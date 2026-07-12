import asyncio
import errno
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api import routes_workbench
from parsing_core.serving.api.deps import get_scheduler
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench import pipeline as workbench_pipeline
from parsing_core.workbench.keychain import KeychainError
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.source_import import CourseStorageError, TextbookImportBatch


def client(tmp_path, *, raise_server_exceptions=True):
    db_path = tmp_path / "serve.db"

    def factory():
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        return Orchestrator(
            Repository(conn),
            FsLayout(base_dir=str(tmp_path / "fs")),
            StubLLMClient(),
            str(db_path),
        )

    return TestClient(build_app(factory), raise_server_exceptions=raise_server_exceptions)


def course_root(tmp_path):
    root = tmp_path / "fs" / "workbench-courses" / "out"
    root.mkdir(parents=True, exist_ok=True)
    return root


def confirmed_chapter(client, root):
    course = client.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = client.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()
    chapter = client.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]
    confirm_res = client.post(f"/api/workbench/chapters/{chapter['id']}/confirm")
    assert confirm_res.status_code == 200
    return course, source, chapter


def test_create_course_and_list(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": str(root)},
    )
    assert res.status_code == 200
    course_id = res.json()["id"]

    res = c.get("/api/workbench/courses")
    assert res.status_code == 200
    assert res.json()[0]["id"] == course_id


def test_create_course_accepts_absolute_root_outside_workbench_base(tmp_path):
    c = client(tmp_path)
    root = tmp_path / "outside"

    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": str(root)},
    )

    assert res.status_code == 200
    assert res.json()["root_dir"] == str(root.resolve())
    assert root.is_dir()


def test_create_course_rejects_relative_root(tmp_path):
    c = client(tmp_path)

    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": "relative-course"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "root_dir must be an absolute path"


def test_create_course_rejects_home_relative_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c = client(tmp_path)

    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": "~/MBA"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "root_dir must be an absolute path"


def test_create_course_rejects_root_that_is_a_file(tmp_path):
    c = client(tmp_path)
    root = tmp_path / "course.txt"
    root.write_text("not a directory", encoding="utf-8")

    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": str(root)},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "root_dir cannot be created"


def test_create_course_rejects_nul_in_root_path(tmp_path):
    c = client(tmp_path, raise_server_exceptions=False)

    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": f"{tmp_path}/bad\0path"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "root_dir is invalid"


def test_create_source_rejects_file_outside_course_root(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    outside = tmp_path / "outside.md"
    outside.write_text("## 第一章\n战略是选择。", encoding="utf-8")

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(outside), "title": "战略教材"},
    )

    assert res.status_code == 400


def test_import_multiple_textbooks_creates_independent_sources(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    first = tmp_path / "战略管理.pdf"
    second = tmp_path / "案例集.DOCX"
    first.write_bytes(b"main-book")
    second.write_bytes(b"case-book")

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(first), str(second)]},
    )

    assert res.status_code == 200
    assert [item["title"] for item in res.json()["items"]] == ["战略管理", "案例集"]
    assert len({item["source_id"] for item in res.json()["items"]}) == 2
    stored_paths = [Path(item["stored_path"]) for item in res.json()["items"]]
    assert [path.read_bytes() for path in stored_paths] == [b"main-book", b"case-book"]
    sources = c.get(f"/api/workbench/courses/{course['id']}/sources").json()
    assert [source["kind"] for source in sources] == ["main", "main"]
    assert {source["file_path"] for source in sources} == {str(path) for path in stored_paths}


def test_import_textbooks_saves_custom_titles_without_changing_stored_filenames(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    first = tmp_path / "strategy.pdf"
    second = tmp_path / "cases.docx"
    first.write_bytes(b"strategy")
    second.write_bytes(b"cases")

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={
            "paths": [str(first), str(second)],
            "titles": [" 战略管理（第 5 版） ", "案例与实践"],
        },
    )

    assert res.status_code == 200
    items = res.json()["items"]
    assert [item["title"] for item in items] == ["战略管理（第 5 版）", "案例与实践"]
    assert [Path(item["stored_path"]).name for item in items] == ["strategy.pdf", "cases.docx"]
    sources = c.get(f"/api/workbench/courses/{course['id']}/sources").json()
    assert [source["title"] for source in sources] == ["战略管理（第 5 版）", "案例与实践"]


@pytest.mark.parametrize(
    "titles",
    [
        [],
        ["一本", "多一本"],
        ["   "],
        ["x" * 121],
    ],
)
def test_import_textbooks_rejects_invalid_custom_titles(tmp_path, titles):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)], "titles": titles},
    )

    assert res.status_code == 422
    assert str(source) not in res.text


def test_import_same_source_twice_creates_two_files_and_sources(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source), str(source)]},
    )

    assert res.status_code == 200
    items = res.json()["items"]
    assert [Path(item["stored_path"]).name for item in items] == ["book.pdf", "book-2.pdf"]
    assert len({item["source_id"] for item in items}) == 2


def test_import_textbooks_returns_404_before_reading_paths_for_unknown_course(tmp_path):
    c = client(tmp_path)

    res = c.post(
        "/api/workbench/courses/missing/sources/import",
        json={"paths": [str(tmp_path / "missing.pdf")]},
    )

    assert res.status_code == 404
    assert res.json()["detail"] == "course not found"


@pytest.mark.parametrize("payload", [{}, {"paths": []}, {"paths": "book.pdf"}])
def test_import_textbooks_rejects_structurally_invalid_requests(tmp_path, payload):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()

    res = c.post(f"/api/workbench/courses/{course['id']}/sources/import", json=payload)

    assert res.status_code == 422


def test_import_textbooks_rejects_more_than_fifty_paths(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(tmp_path / "missing.pdf")] * 51},
    )

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_import_copy_does_not_block_health_on_same_event_loop(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    root = course_root(tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")
    copy_started = threading.Event()
    release_copy = threading.Event()
    real_copyfileobj = __import__("shutil").copyfileobj

    def paused_copy(source_file, target_file, *args, **kwargs):
        copy_started.set()
        assert release_copy.wait(timeout=3)
        return real_copyfileobj(source_file, target_file, *args, **kwargs)

    monkeypatch.setattr(
        "parsing_core.workbench.source_import.shutil.copyfileobj",
        paused_copy,
    )
    transport = ASGITransport(app=test_client.app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        course = (
            await async_client.post(
                "/api/workbench/courses",
                json={"title": "战略管理", "description": "", "root_dir": str(root)},
            )
        ).json()
        started_at = time.monotonic()
        import_request = asyncio.create_task(
            async_client.post(
                f"/api/workbench/courses/{course['id']}/sources/import",
                json={"paths": [str(source)]},
            )
        )
        try:
            assert await asyncio.to_thread(copy_started.wait, 3)
            health = await asyncio.wait_for(async_client.get("/health"), timeout=0.5)
            assert time.monotonic() - started_at < 0.5
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
        finally:
            release_copy.set()
        import_response = await import_request

    assert import_response.status_code == 200


def test_import_textbooks_maps_invalid_input_to_400_without_leaking_path(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    invalid = tmp_path / "secret" / "missing.pdf"

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(invalid)]},
    )

    assert res.status_code == 400
    assert str(invalid) not in res.text


def test_import_textbooks_maps_storage_exhaustion_to_507(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    def fail_fsync(file_descriptor):
        raise OSError(errno.ENOSPC, "private target path")

    monkeypatch.setattr("parsing_core.workbench.source_import.os.fsync", fail_fsync)

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)]},
    )

    assert res.status_code == 507
    assert res.json()["detail"] == "course storage could not complete import"
    assert "private target path" not in res.text
    assert list((root / "教材原文件").iterdir()) == []
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


def test_import_textbooks_rejects_storage_without_hardlinks(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"book")

    def fail_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "hardlinks disabled")

    monkeypatch.setattr("parsing_core.workbench.source_import.os.link", fail_link)

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)]},
    )

    assert res.status_code == 507
    assert res.json()["detail"] == "course storage does not support atomic imports"
    assert list((root / "教材原文件").iterdir()) == []
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


def test_import_rolls_back_through_directory_fd_when_target_path_is_replaced(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    target_dir = root / "教材原文件"
    target_dir.mkdir()
    detached_dir = root / "detached-textbooks"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "book.pdf"
    victim.write_bytes(b"victim")
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"new-book")
    real_copyfileobj = __import__("shutil").copyfileobj
    replaced = False

    def replace_directory_after_copy(source_file, target_file, *args, **kwargs):
        nonlocal replaced
        result = real_copyfileobj(source_file, target_file, *args, **kwargs)
        if not replaced:
            replaced = True
            target_dir.rename(detached_dir)
            target_dir.symlink_to(outside_dir, target_is_directory=True)
        return result

    monkeypatch.setattr(
        "parsing_core.workbench.source_import.shutil.copyfileobj",
        replace_directory_after_copy,
    )

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)]},
    )

    assert res.status_code == 500
    assert res.json()["detail"] == "course storage changed during import"
    assert victim.read_bytes() == b"victim"
    assert {path.name for path in outside_dir.iterdir()} == {"book.pdf"}
    assert list(detached_dir.iterdir()) == []
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


def test_next_import_recovers_file_published_before_database_commit(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    crashed_source = tmp_path / "crashed" / "book.pdf"
    replacement_source = tmp_path / "replacement" / "book.pdf"
    crashed_source.parent.mkdir()
    replacement_source.parent.mkdir()
    crashed_source.write_bytes(b"orphan")
    replacement_source.write_bytes(b"replacement")

    batch = TextbookImportBatch(root)
    batch.import_file(crashed_source)
    target_dir = root / "教材原文件"
    assert len(list(target_dir.glob(".*.import-journal"))) == 1
    batch._close()

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(replacement_source)]},
    )

    assert res.status_code == 200
    stored_path = Path(res.json()["items"][0]["stored_path"])
    assert stored_path.name == "book.pdf"
    assert stored_path.read_bytes() == b"replacement"
    assert list(target_dir.glob(".*.import-journal")) == []
    assert len(c.get(f"/api/workbench/courses/{course['id']}/sources").json()) == 1


def test_recovery_keeps_file_committed_before_journal_cleanup(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"committed")
    second.write_bytes(b"second")

    batch = TextbookImportBatch(root)
    imported = batch.import_file(first)
    repo = routes_workbench._repo(get_scheduler())
    repo.create_sources(course["id"], [("main", str(imported.stored_path), imported.title)])
    target_dir = root / "教材原文件"
    assert len(list(target_dir.glob(".*.import-journal"))) == 1
    batch._close()

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(second)]},
    )

    assert res.status_code == 200
    assert imported.stored_path.read_bytes() == b"committed"
    assert list(target_dir.glob(".*.import-journal")) == []
    assert len(c.get(f"/api/workbench/courses/{course['id']}/sources").json()) == 2


def test_recovery_keeps_committed_file_from_another_course_with_shared_root(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course_a = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    course_b = c.post(
        "/api/workbench/courses",
        json={"title": "组织行为", "description": "", "root_dir": str(root)},
    ).json()
    source_a = tmp_path / "strategy.pdf"
    source_b = tmp_path / "organization.pdf"
    source_a.write_bytes(b"committed-a")
    source_b.write_bytes(b"course-b")

    batch = TextbookImportBatch(root)
    imported_a = batch.import_file(source_a)
    routes_workbench._repo(get_scheduler()).create_sources(
        course_a["id"],
        [("main", str(imported_a.stored_path), imported_a.title)],
    )
    batch._close()
    target_dir = root / "教材原文件"
    assert len(list(target_dir.glob(".*.import-journal"))) == 1

    res = c.post(
        f"/api/workbench/courses/{course_b['id']}/sources/import",
        json={"paths": [str(source_b)]},
    )

    assert res.status_code == 200
    assert imported_a.stored_path.read_bytes() == b"committed-a"
    assert list(target_dir.glob(".*.import-journal")) == []
    assert len(c.get(f"/api/workbench/courses/{course_a['id']}/sources").json()) == 1
    assert len(c.get(f"/api/workbench/courses/{course_b['id']}/sources").json()) == 1


@pytest.mark.parametrize("cleanup_failure", ["unlink", "fsync"])
def test_committed_import_succeeds_when_journal_cleanup_is_deferred(
    tmp_path, monkeypatch, cleanup_failure
):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    real_unlink = TextbookImportBatch._unlink
    real_fsync = TextbookImportBatch._fsync_directory

    def defer_journal_cleanup(batch, name, original_error=None):
        if name.endswith(".import-journal"):
            return False
        return real_unlink(batch, name, original_error)

    fsync_calls = 0

    def fail_commit_fsync(batch):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise CourseStorageError("directory fsync failed")
        return real_fsync(batch)

    if cleanup_failure == "unlink":
        monkeypatch.setattr(TextbookImportBatch, "_unlink", defer_journal_cleanup)
    else:
        monkeypatch.setattr(TextbookImportBatch, "_fsync_directory", fail_commit_fsync)

    first_res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(first)]},
    )

    target_dir = root / "教材原文件"
    assert first_res.status_code == 200
    assert len(c.get(f"/api/workbench/courses/{course['id']}/sources").json()) == 1
    assert len(list(target_dir.glob(".*.import-journal"))) == 1

    monkeypatch.setattr(TextbookImportBatch, "_unlink", real_unlink)
    monkeypatch.setattr(TextbookImportBatch, "_fsync_directory", real_fsync)
    second_res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(second)]},
    )

    assert second_res.status_code == 200
    assert list(target_dir.glob(".*.import-journal")) == []
    sources = c.get(f"/api/workbench/courses/{course['id']}/sources").json()
    assert len(sources) == 2
    assert {source["title"] for source in sources} == {"first", "second"}


def test_failed_orphan_cleanup_keeps_journal_for_next_recovery(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    orphan_source = tmp_path / "orphan" / "book.pdf"
    new_source = tmp_path / "new" / "new.pdf"
    orphan_source.parent.mkdir()
    new_source.parent.mkdir()
    orphan_source.write_bytes(b"orphan")
    new_source.write_bytes(b"new")

    batch = TextbookImportBatch(root)
    orphan = batch.import_file(orphan_source)
    batch._close()
    target_dir = root / "教材原文件"
    journal = next(target_dir.glob(".*.import-journal"))
    real_unlink = TextbookImportBatch._unlink

    def fail_orphan_unlink(import_batch, name, original_error=None):
        if name == "book.pdf":
            return False
        return real_unlink(import_batch, name, original_error)

    monkeypatch.setattr(TextbookImportBatch, "_unlink", fail_orphan_unlink)

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(new_source)]},
    )

    assert res.status_code == 507
    assert orphan.stored_path.read_bytes() == b"orphan"
    assert journal.exists()
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


@pytest.mark.parametrize("register_source", [False, True])
def test_mark_failure_with_final_cleanup_failure_is_recovered(
    tmp_path, monkeypatch, register_source
):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source = tmp_path / "book.pdf"
    next_source = tmp_path / "next.pdf"
    source.write_bytes(b"published-before-failure")
    next_source.write_bytes(b"next")
    real_mark = TextbookImportBatch._mark_journal_published
    real_unlink = TextbookImportBatch._unlink

    def fail_mark(import_batch, record):
        raise CourseStorageError("journal mark failed")

    def fail_final_unlink(import_batch, name, original_error=None):
        if name == "book.pdf":
            return False
        return real_unlink(import_batch, name, original_error)

    monkeypatch.setattr(TextbookImportBatch, "_mark_journal_published", fail_mark)
    monkeypatch.setattr(TextbookImportBatch, "_unlink", fail_final_unlink)

    failed = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)]},
    )

    target_dir = root / "教材原文件"
    published = target_dir / "book.pdf"
    journals = list(target_dir.glob(".*.import-journal"))
    assert failed.status_code == 507
    assert published.read_bytes() == b"published-before-failure"
    assert len(journals) == 1

    if register_source:
        routes_workbench._repo(get_scheduler()).create_sources(
            course["id"],
            [("main", str(published), "book")],
        )

    monkeypatch.setattr(TextbookImportBatch, "_mark_journal_published", real_mark)
    monkeypatch.setattr(TextbookImportBatch, "_unlink", real_unlink)

    recovered = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(next_source)]},
    )

    assert recovered.status_code == 200
    assert list(target_dir.glob(".*.import-journal")) == []
    if register_source:
        assert published.read_bytes() == b"published-before-failure"
        assert len(c.get(f"/api/workbench/courses/{course['id']}/sources").json()) == 2
    else:
        assert not published.exists()
        assert len(c.get(f"/api/workbench/courses/{course['id']}/sources").json()) == 1


@pytest.mark.parametrize("replace_after_insert", [1, 2])
def test_directory_replacement_during_insert_rolls_back_database_and_stable_directory(
    tmp_path, replace_after_insert
):
    c = client(tmp_path)
    root = course_root(tmp_path)
    target_dir = root / "教材原文件"
    detached_dir = root / "detached-textbooks"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "first.pdf"
    victim.write_bytes(b"victim")
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    sources = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for source in sources:
        source.write_bytes(source.stem.encode())
    repo = routes_workbench._repo(get_scheduler())
    replaced = False

    def replace_target_directory():
        nonlocal replaced
        if not replaced:
            replaced = True
            target_dir.rename(detached_dir)
            target_dir.symlink_to(outside_dir, target_is_directory=True)
        return 0

    repo.conn.create_function("replace_target_directory", 0, replace_target_directory)
    repo.conn.executescript(
        f"""
        CREATE TRIGGER replace_target_during_source_insert
        AFTER INSERT ON wb_sources
        WHEN (
          SELECT COUNT(*) FROM wb_sources WHERE course_id = NEW.course_id
        ) = {replace_after_insert}
        BEGIN
          SELECT replace_target_directory();
        END;
        """
    )
    repo.conn.commit()

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source) for source in sources]},
    )

    assert res.status_code == 500
    assert res.json()["detail"] == "course storage changed during import"
    assert replaced
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []
    assert list(detached_dir.iterdir()) == []
    assert list(detached_dir.glob(".*.import-journal")) == []
    assert victim.read_bytes() == b"victim"
    assert {path.name for path in outside_dir.iterdir()} == {"first.pdf"}


@pytest.mark.parametrize(
    "journal_content",
    [
        b'{"version": 1, "final_name": ',
        json.dumps(
            {
                "version": 1,
                "final_name": "../victim.pdf",
                "temporary_name": ".book.tmp",
                "device": 1,
                "inode": 1,
            }
        ).encode(),
    ],
)
def test_recovery_does_not_delete_files_for_invalid_journal(tmp_path, journal_content):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    target_dir = root / "教材原文件"
    target_dir.mkdir()
    journal = target_dir / ".invalid.import-journal"
    journal.write_bytes(journal_content)
    victim = root / "victim.pdf"
    victim.write_bytes(b"victim")
    source = tmp_path / "new.pdf"
    source.write_bytes(b"new")

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)]},
    )

    assert res.status_code == 507
    assert res.json()["detail"] == "course storage could not complete import"
    assert victim.read_bytes() == b"victim"
    assert journal.read_bytes() == journal_content
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


def test_second_copy_failure_removes_first_file_and_creates_no_sources(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    sources = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for source in sources:
        source.write_bytes(source.stem.encode())
    real_import = routes_workbench.TextbookImportBatch.import_file
    calls = 0

    def fail_second(batch, source_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CourseStorageError("course storage could not complete import")
        return real_import(batch, source_path)

    monkeypatch.setattr(routes_workbench.TextbookImportBatch, "import_file", fail_second)

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source) for source in sources]},
    )

    assert res.status_code == 507
    assert list((root / "教材原文件").iterdir()) == []
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


def test_second_database_insert_failure_rolls_back_files_and_sources(tmp_path):
    c = client(tmp_path, raise_server_exceptions=False)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    sources = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for source in sources:
        source.write_bytes(source.stem.encode())
    repo = routes_workbench._repo(get_scheduler())
    repo.conn.executescript(
        """
        CREATE TRIGGER fail_second_source
        BEFORE INSERT ON wb_sources
        WHEN (SELECT COUNT(*) FROM wb_sources) = 1
        BEGIN
          SELECT RAISE(ABORT, 'private database failure');
        END;
        """
    )
    repo.conn.commit()

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source) for source in sources]},
    )

    assert res.status_code == 500
    assert "private database failure" not in res.text
    assert list((root / "教材原文件").iterdir()) == []
    assert c.get(f"/api/workbench/courses/{course['id']}/sources").json() == []


def test_cleanup_failure_does_not_mask_database_error_and_attempts_remaining_files(
    tmp_path, monkeypatch
):
    c = client(tmp_path, raise_server_exceptions=False)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    sources = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for source in sources:
        source.write_bytes(source.stem.encode())

    def fail_database(self, course_id, source_specs, guard):
        raise RuntimeError("original database error")

    real_unlink = routes_workbench.TextbookImportBatch._unlink
    attempted = []

    def fail_first_unlink(batch, name, original_error=None):
        if name.endswith(".pdf"):
            attempted.append(name)
        if name == "first.pdf":
            if original_error is not None:
                original_error.add_note("cleanup failed")
            return False
        return real_unlink(batch, name, original_error)

    monkeypatch.setattr(
        routes_workbench.WorkbenchRepository,
        "create_sources_guarded",
        fail_database,
    )
    monkeypatch.setattr(routes_workbench.TextbookImportBatch, "_unlink", fail_first_unlink)

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source) for source in sources]},
    )

    assert res.status_code == 500
    assert "original database error" not in res.text
    assert attempted == ["second.pdf", "first.pdf"]
    assert (root / "教材原文件" / "first.pdf").exists()
    assert not (root / "教材原文件" / "second.pdf").exists()
    assert len(list((root / "教材原文件").glob(".*.import-journal"))) == 1


def test_import_preserves_preexisting_same_name_file_when_batch_rolls_back(tmp_path, monkeypatch):
    c = client(tmp_path, raise_server_exceptions=False)
    root = course_root(tmp_path)
    target_dir = root / "教材原文件"
    target_dir.mkdir()
    existing = target_dir / "book.pdf"
    existing.write_bytes(b"existing")
    source = tmp_path / "book.pdf"
    source.write_bytes(b"new")

    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()

    def fail_database(self, course_id, source_specs, guard):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        routes_workbench.WorkbenchRepository,
        "create_sources_guarded",
        fail_database,
    )

    res = c.post(
        f"/api/workbench/courses/{course['id']}/sources/import",
        json={"paths": [str(source)]},
    )

    assert res.status_code == 500
    assert existing.read_bytes() == b"existing"
    assert not (target_dir / "book-2.pdf").exists()


def test_concurrent_import_requests_with_same_name_do_not_overwrite(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    first = tmp_path / "a" / "book.pdf"
    second = tmp_path / "b" / "book.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first-content")
    second.write_bytes(b"second-content")
    barrier = threading.Barrier(2)
    real_import = routes_workbench.TextbookImportBatch.import_file

    def synchronized_import(batch, source_path):
        barrier.wait()
        return real_import(batch, source_path)

    monkeypatch.setattr(routes_workbench.TextbookImportBatch, "import_file", synchronized_import)
    responses = []

    def request(source):
        responses.append(
            c.post(
                f"/api/workbench/courses/{course['id']}/sources/import",
                json={"paths": [str(source)]},
            )
        )

    threads = [threading.Thread(target=request, args=(source,)) for source in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [response.status_code for response in responses] == [200, 200]
    items = [response.json()["items"][0] for response in responses]
    assert len({item["stored_path"] for item in items}) == 2
    assert {Path(item["stored_path"]).read_bytes() for item in items} == {
        b"first-content",
        b"second-content",
    }


@pytest.mark.parametrize("write_operation", ["mkdir", "write_text"])
def test_detect_chapters_maps_course_directory_write_errors_to_400(
    tmp_path, monkeypatch, write_operation
):
    c = client(tmp_path, raise_server_exceptions=False)
    root = tmp_path / "external-course"
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()

    def fail_write(*args, **kwargs):
        raise OSError("course directory is read-only")

    monkeypatch.setattr(routes_workbench.Path, write_operation, fail_write)

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")

    assert res.status_code == 400
    assert res.json()["detail"] == "course directory cannot be written"


def test_cors_allows_local_app_origins_but_not_wildcard(tmp_path):
    c = client(tmp_path)

    res = c.options(
        "/health",
        headers={
            "Origin": "http://localhost:1420",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:1420"

    res = c.options(
        "/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


def test_confirm_chapter_then_run_pipeline(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")
    assert res.status_code == 200
    chapter = res.json()[0]
    chapter_id = chapter["id"]
    res = c.post(f"/api/workbench/chapters/{chapter_id}/confirm")
    assert res.status_code == 200
    assert res.json() == {
        "id": chapter_id,
        "source_id": source["id"],
        "course_id": course["id"],
        "seq": chapter["seq"],
        "title": chapter["title"],
        "status": "CONFIRMED",
    }

    res = c.post(f"/api/workbench/chapters/{chapter_id}/run", json={"executor": "stub"})
    assert res.status_code == 200
    assert res.json()["status"] == "COMPLETED"
    assert res.json()["id"] == chapter_id

    res = c.get(f"/api/workbench/courses/{course['id']}/sources")
    assert res.status_code == 200
    assert [item["id"] for item in res.json()] == [source["id"]]

    res = c.get(f"/api/workbench/sources/{source['id']}/chapters")
    assert res.status_code == 200
    assert [item["id"] for item in res.json()] == [chapter_id]
    assert res.json()[0]["status"] == "COMPLETED"

    res = c.get(f"/api/workbench/courses/{course['id']}/cards")
    assert res.status_code == 200
    assert res.json()[0]["origin_type"] == "chapter"
    assert res.json()[0]["origin_id"] == chapter_id
    assert res.json()[0]["origin_title"] == chapter["title"]
    assert res.json()[0]["card_type"] == "topic"

    res = c.get(f"/api/workbench/chapters/{chapter_id}/note-blocks")
    assert res.status_code == 200
    blocks = {item["kind"]: item for item in res.json()}
    assert blocks["knowledge_mermaid"]["body"].startswith("graph TD")
    assert blocks["application_mermaid"]["body"].startswith("flowchart LR")


def test_workbench_list_endpoints_return_not_found(tmp_path):
    c = client(tmp_path)

    assert c.get("/api/workbench/courses/missing/sources").status_code == 404
    assert c.get("/api/workbench/sources/missing/chapters").status_code == 404
    assert c.get("/api/workbench/courses/missing/cards").status_code == 404
    assert c.get("/api/workbench/chapters/missing/note-blocks").status_code == 404


def test_chapter_note_block_patch_matches_frontend_contract_and_syncs_markdown(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    assert (
        c.post(
            f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"}
        ).status_code
        == 200
    )
    endpoint = f"/api/workbench/chapters/{chapter['id']}/note-blocks/knowledge_mermaid"
    original = next(
        item["body"]
        for item in c.get(f"/api/workbench/chapters/{chapter['id']}/note-blocks").json()
        if item["kind"] == "knowledge_mermaid"
    )
    source = "flowchart LR\nA[编辑] --> B[保存]"

    response = c.patch(endpoint, json={"body": source, "expected_body": original})

    assert response.status_code == 200, response.text
    assert response.json()["body"] == source
    assert any(
        source in path.read_text(encoding="utf-8") for path in root.rglob("intensive-note.md")
    )
    conflict = c.patch(
        endpoint,
        json={"body": "flowchart LR\nX-->Y", "expected_body": original},
    )
    assert conflict.status_code == 409


@pytest.mark.parametrize("kind", ["unknown", "cards"])
def test_chapter_note_block_patch_rejects_non_fixed_kind(tmp_path, kind):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    response = c.patch(
        f"/api/workbench/chapters/{chapter['id']}/note-blocks/{kind}",
        json={"body": "内容", "expected_body": "旧内容"},
    )
    assert response.status_code == 422


def test_chapter_note_block_patch_rejects_invalid_mermaid(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    response = c.patch(
        f"/api/workbench/chapters/{chapter['id']}/note-blocks/knowledge_mermaid",
        json={"body": "not mermaid", "expected_body": "old"},
    )
    assert response.status_code == 422


def test_chapter_note_block_patch_disk_failure_returns_507_and_retains_db_edit(
    tmp_path, monkeypatch
):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    block = next(item for item in repo.list_note_blocks(chapter["id"]) if item.kind == "summary")
    monkeypatch.setattr(
        routes_workbench,
        "sync_chapter_markdown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    response = c.patch(
        f"/api/workbench/chapters/{chapter['id']}/note-blocks/summary",
        json={"body": "数据库已保存", "expected_body": block.body},
    )

    assert response.status_code == 507
    saved = next(item for item in repo.list_note_blocks(chapter["id"]) if item.kind == "summary")
    assert saved.body == "数据库已保存"


def test_course_cards_unify_chapter_and_topic_origins_and_topic_block_patch(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course, source, chapter = confirmed_chapter(c, root)
    run_chapter = c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    assert run_chapter.status_code == 200
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "战略融合", "chapter_ids": [chapter["id"]]},
    ).json()
    assert c.post(f"/api/workbench/courses/{course['id']}/topics/confirm").status_code == 200
    run_topic = c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    assert run_topic.status_code == 200

    cards = c.get(f"/api/workbench/courses/{course['id']}/cards").json()
    assert {card["origin_type"] for card in cards} == {"chapter", "topic"}
    chapter_card = next(card for card in cards if card["origin_type"] == "chapter")
    assert chapter_card["origin_id"] == chapter["id"]
    assert chapter_card["origin_title"] == chapter["title"]
    topic_card = next(card for card in cards if card["origin_type"] == "topic")
    assert topic_card["origin_id"] == topic["id"]
    assert topic_card["origin_title"] == "战略融合"
    assert topic_card["source_refs"] == [f"[《{source['title']}》·第 1 章]"]

    block = c.patch(
        f"/api/workbench/topics/{topic['id']}/note-blocks/knowledge_mermaid",
        json={
            "content": "flowchart LR\nA[更新] --> B[保存]",
            "expected_content": next(
                item["content"]
                for item in c.get(f"/api/workbench/topics/{topic['id']}/note-blocks").json()
                if item["kind"] == "knowledge_mermaid"
            ),
        },
    )
    assert block.status_code == 200, block.text
    assert block.json()["content"].startswith("flowchart LR")
    listed = c.get(f"/api/workbench/topics/{topic['id']}/note-blocks").json()
    saved = next(item for item in listed if item["kind"] == "knowledge_mermaid")
    assert saved["content"] == block.json()["content"]
    assert (
        c.patch(
            f"/api/workbench/topics/{topic['id']}/note-blocks/not-a-block",
            json={"content": "x"},
        ).status_code
        == 422
    )


def test_topic_block_patch_sync_failure_retains_edit_and_retry_publishes(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course, _, chapter = confirmed_chapter(c, root)
    c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "战略融合", "chapter_ids": [chapter["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    source = "flowchart LR\nA[新源码] --> B[已保留]"

    def fail_sync(*_args, **_kwargs):
        raise OSError("/private/key sk-secret")

    monkeypatch.setattr("parsing_core.workbench.topic_pipeline.sync_topic_markdown", fail_sync)
    failed = c.patch(
        f"/api/workbench/topics/{topic['id']}/note-blocks/knowledge_mermaid",
        json={
            "content": source,
            "expected_content": next(
                block.content
                for block in WorkbenchRepository(
                    get_scheduler()._query_orch.repo.conn
                ).list_topic_note_blocks(topic["id"])
                if block.kind == "knowledge_mermaid"
            ),
        },
    )
    assert failed.status_code == 507
    assert failed.json() == {"detail": "topic Markdown sync failed; database edit retained"}
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    saved = next(
        block
        for block in repo.list_topic_note_blocks(topic["id"])
        if block.kind == "knowledge_mermaid"
    )
    assert saved.content == source
    assert repo.get_topic_markdown_sync_state(topic["id"]).status == "FAILED"
    assert "/private" not in failed.text and "secret" not in failed.text

    monkeypatch.undo()
    retry = c.post(f"/api/workbench/topics/{topic['id']}/sync/retry")
    assert retry.status_code == 200
    assert retry.json()["sync_status"] == "SYNCED"
    notes = [path.read_text(encoding="utf-8") for path in root.rglob("intensive-note.md")]
    assert any(source in note for note in notes)


def test_topic_block_patch_uses_cas_and_preserves_first_window_db_and_markdown(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course, _, chapter = confirmed_chapter(c, root)
    c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "融合", "chapter_ids": [chapter["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    endpoint = f"/api/workbench/topics/{topic['id']}/note-blocks/knowledge_mermaid"
    original = next(
        item["content"]
        for item in c.get(f"/api/workbench/topics/{topic['id']}/note-blocks").json()
        if item["kind"] == "knowledge_mermaid"
    )
    first_source = "flowchart LR\nA[窗口一] --> B[成功]"
    assert (
        c.patch(endpoint, json={"content": first_source, "expected_content": original}).status_code
        == 200
    )
    second = c.patch(
        endpoint,
        json={"content": "flowchart LR\nA[窗口二] --> B[冲突]", "expected_content": original},
    )
    assert second.status_code == 409
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    assert (
        next(
            block.content
            for block in repo.list_topic_note_blocks(topic["id"])
            if block.kind == "knowledge_mermaid"
        )
        == first_source
    )
    assert any(
        first_source in path.read_text(encoding="utf-8") for path in root.rglob("intensive-note.md")
    )


def test_topic_block_patch_protects_live_sync_owner_and_recovers_expired_lease(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course, _, chapter = confirmed_chapter(c, root)
    c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "融合", "chapter_ids": [chapter["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    repo = WorkbenchRepository(get_scheduler()._query_orch.repo.conn)
    original = next(
        block.content
        for block in repo.list_topic_note_blocks(topic["id"])
        if block.kind == "knowledge_mermaid"
    )
    repo.set_topic_markdown_sync_state(topic["id"], "PENDING")
    lease = repo.claim_topic_markdown_sync(topic["id"], lease_ttl=3600)
    endpoint = f"/api/workbench/topics/{topic['id']}/note-blocks/knowledge_mermaid"
    active = c.patch(
        endpoint,
        json={"content": "flowchart LR\nA-->B", "expected_content": original},
    )
    assert active.status_code == 409
    state = repo.get_topic_markdown_sync_state(topic["id"])
    assert state.status == "SYNCING" and state.owner_id == lease.owner_id
    repo.conn.execute(
        "UPDATE wb_topic_markdown_sync SET lease_expires_at = 0 WHERE topic_id = ?",
        (topic["id"],),
    )
    repo.conn.commit()
    recovered = c.patch(
        endpoint,
        json={
            "content": "flowchart LR\nA[过期] --> B[恢复]",
            "expected_content": original,
        },
    )
    assert recovered.status_code == 200
    assert repo.get_topic_markdown_sync_state(topic["id"]).status == "SYNCED"


def test_course_cards_have_stable_origin_seq_id_order_and_constant_select_count(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course, _, chapter = confirmed_chapter(c, root)
    c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})
    topic = c.post(
        f"/api/workbench/courses/{course['id']}/topics",
        json={"title": "融合", "chapter_ids": [chapter["id"]]},
    ).json()
    c.post(f"/api/workbench/courses/{course['id']}/topics/confirm")
    c.post(f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"})
    conn = get_scheduler()._query_orch.repo.conn
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        first = c.get(f"/api/workbench/courses/{course['id']}/cards")
        second = c.get(f"/api/workbench/courses/{course['id']}/cards")
    finally:
        conn.set_trace_callback(None)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    order = [(item["origin_type"], item["origin_id"], item["id"]) for item in first.json()]
    assert order == sorted(order, key=lambda item: (item[0] != "chapter", item[1], item[2]))
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 4  # one course existence check + one union query per request


def test_draft_chapter_run_returns_conflict(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()
    chapter = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/run", json={"executor": "stub"})

    assert res.status_code == 409
    assert res.json()["detail"] == "chapter must be CONFIRMED before intensive reading"


def test_run_hybrid_requires_deepseek_settings(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()
    chapter = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]
    c.post(f"/api/workbench/chapters/{chapter['id']}/confirm")

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert res.status_code == 400
    assert res.json()["detail"] == "deepseek api key not configured"


def test_save_deepseek_settings_rejects_empty_api_key_without_writing(tmp_path, monkeypatch):
    c = client(tmp_path)
    save_calls = {"count": 0}
    settings_path = tmp_path / "fs" / "workbench-settings.json"

    def fake_save_secret(service, account, api_key):
        save_calls["count"] += 1

    monkeypatch.setattr(routes_workbench, "save_secret", fake_save_secret)

    res = c.post(
        "/api/workbench/settings/deepseek",
        json={"api_key": "", "model": "deepseek-chat"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "deepseek api key cannot be empty"
    assert save_calls["count"] == 0
    assert not settings_path.exists()


def test_save_deepseek_settings_allows_model_only_update_with_existing_key(tmp_path, monkeypatch):
    c = client(tmp_path)
    save_calls = {"count": 0}
    settings_path = tmp_path / "fs" / "workbench-settings.json"

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "sk-existing-key")

    def fake_save_secret(service, account, api_key):
        save_calls["count"] += 1

    monkeypatch.setattr(routes_workbench, "save_secret", fake_save_secret)

    res = c.post(
        "/api/workbench/settings/deepseek",
        json={"model": "deepseek-reasoner"},
    )

    assert res.status_code == 200
    assert res.json() == {
        "deepseek_model": "deepseek-reasoner",
        "deepseek_key_masked": "sk-****-key",
    }
    assert save_calls["count"] == 0
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "deepseek_model": "deepseek-reasoner"
    }


def test_run_hybrid_rejects_blank_deepseek_key_without_running_pipeline(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    calls = {"run_all": 0}

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "")

    def fake_run_all(self, chapter_id):
        calls["run_all"] += 1

    monkeypatch.setattr(routes_workbench.IntensiveReadingPipeline, "run_all", fake_run_all)

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert res.status_code == 400
    assert res.json()["detail"] == "deepseek api key not configured"
    assert calls["run_all"] == 0


def test_run_hybrid_missing_codex_returns_400_without_running_pipeline(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    calls = {"run_all": 0}

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "sk-test")
    monkeypatch.setattr(
        routes_workbench,
        "resolve_codex_path",
        lambda: (_ for _ in ()).throw(routes_workbench.CodexCliError("codex cli not found")),
    )

    def fake_run_all(self, chapter_id):
        calls["run_all"] += 1

    monkeypatch.setattr(routes_workbench.IntensiveReadingPipeline, "run_all", fake_run_all)

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert res.status_code == 400
    assert res.json()["detail"] == "codex cli not found"
    assert calls["run_all"] == 0
    assert c.get(f"/api/workbench/chapters/{chapter['id']}").json()["status"] == "CONFIRMED"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_run_hybrid_pipeline_failure_marks_chapter_failed(tmp_path, monkeypatch, error_type):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "sk-test")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda: "/usr/bin/codex")

    def fake_run_all(self, chapter_id):
        raise error_type("boom")

    monkeypatch.setattr(routes_workbench.IntensiveReadingPipeline, "run_all", fake_run_all)

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert res.status_code == 500
    assert res.json()["detail"] == "boom"
    assert c.get(f"/api/workbench/chapters/{chapter['id']}").json()["status"] == "FAILED"


@pytest.mark.parametrize(
    ("endpoint", "payload", "initial_status"),
    [
        ("run", {"executor": "stub"}, "CONFIRMED"),
        ("run-hybrid", None, "CONFIRMED"),
        ("run-hybrid", None, "FAILED"),
    ],
)
def test_chapter_run_maps_markdown_sync_errors_to_safe_400(
    tmp_path,
    monkeypatch,
    endpoint,
    payload,
    initial_status,
):
    c = client(tmp_path)
    root = tmp_path / "external-course"
    _, _, chapter = confirmed_chapter(c, root)
    if initial_status == "FAILED":
        conn = init_db(str(tmp_path / "serve.db"))
        WorkbenchRepository(conn).update_chapter_status(chapter["id"], "FAILED")
        conn.close()

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "sk-test")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda: "/usr/bin/codex")
    stub = routes_workbench.StubIntensiveReadingExecutor()
    monkeypatch.setattr(
        routes_workbench.HybridIntensiveReadingExecutor,
        "run",
        lambda self, round_key, content: stub.run(round_key, content),
    )

    leaked_path = root / "private" / "intensive-note.md"

    def fail_sync(repo, chapter_id):
        raise OSError(f"cannot write {leaked_path}")

    monkeypatch.setattr(workbench_pipeline, "sync_chapter_markdown", fail_sync)

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/{endpoint}", json=payload)

    assert res.status_code == 400
    assert res.json()["detail"] == "course directory cannot be written"
    assert str(root) not in res.text
    assert c.get(f"/api/workbench/chapters/{chapter['id']}").json()["status"] == "FAILED"


def test_run_hybrid_failed_chapter_can_rerun_to_completed(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)
    calls = {"count": 0}

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "sk-test")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda: "/usr/bin/codex")

    def fake_run_all(self, chapter_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        assert chapter_id == chapter["id"]

    monkeypatch.setattr(routes_workbench.IntensiveReadingPipeline, "run_all", fake_run_all)

    first = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert first.status_code == 500
    assert c.get(f"/api/workbench/chapters/{chapter['id']}").json()["status"] == "FAILED"
    second = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")
    assert second.status_code == 200
    assert second.json()["status"] == "COMPLETED"
    assert c.get(f"/api/workbench/chapters/{chapter['id']}").json()["status"] == "COMPLETED"


def test_run_hybrid_completed_chapter_returns_conflict(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    _, _, chapter = confirmed_chapter(c, root)

    monkeypatch.setattr(routes_workbench, "read_secret", lambda service, account: "sk-test")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda: "/usr/bin/codex")
    monkeypatch.setattr(
        routes_workbench.IntensiveReadingPipeline,
        "run_all",
        lambda self, chapter_id: None,
    )

    first = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")
    second = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "chapter must be CONFIRMED or FAILED before hybrid reading"


def test_detect_chapters_replaces_old_chapters_after_source_changes(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。\n## 第二章\n战略要落地。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()

    first = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")
    source_md.write_text("## 修正后的第一章\n战略是选择。", encoding="utf-8")
    second = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")

    assert first.status_code == 200
    assert second.status_code == 200
    assert [ch["title"] for ch in second.json()] == ["修正后的第一章"]


def test_detect_chapters_refuses_to_replace_confirmed_chapters(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()
    chapter = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]
    c.post(f"/api/workbench/chapters/{chapter['id']}/confirm")
    source_md.write_text("## 修正后的第一章\n战略是选择。", encoding="utf-8")

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")

    assert res.status_code == 409
    assert res.json()["detail"] == "source has confirmed or generated chapters"
    chapters = c.get(f"/api/workbench/sources/{source['id']}/chapters").json()
    assert [item["title"] for item in chapters] == ["第一章"]


def test_detect_chapters_uses_safe_single_level_source_dir(tmp_path):
    c = client(tmp_path)
    root_dir = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root_dir)},
    ).json()
    source_md = root_dir / "source.md"
    source_md.write_text("## 第一章/选择\\定位\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略/教材\\案例"},
    ).json()

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")

    assert res.status_code == 200
    assert not (root_dir / "战略").exists()
    assert (root_dir / "战略-教材-案例" / "0-第一章_选择_定位.md").exists()


def test_detect_chapters_falls_back_for_dot_dot_source_dir(tmp_path):
    c = client(tmp_path)
    root_dir = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root_dir)},
    ).json()
    source_md = root_dir / "source.md"
    source_md.write_text("## 01-战略选择\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": ".."},
    ).json()

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")

    assert res.status_code == 200
    assert (root_dir / "source" / "0-01-战略选择.md").exists()
    assert not (root_dir.parent / "0-01-战略选择.md").exists()


def test_workbench_settings_save_and_get(tmp_path, monkeypatch):
    c = client(tmp_path)
    saved = {}

    def fake_save_secret(service, account, secret):
        saved["service"] = service
        saved["account"] = account
        saved["secret"] = secret

    def fake_read_secret(service, account):
        assert service == routes_workbench.KEYCHAIN_SERVICE
        assert account == routes_workbench.KEYCHAIN_ACCOUNT
        return saved["secret"]

    monkeypatch.setattr(routes_workbench, "save_secret", fake_save_secret)
    monkeypatch.setattr(routes_workbench, "read_secret", fake_read_secret)

    post_res = c.post(
        "/api/workbench/settings/deepseek",
        json={"api_key": "sk-abcdefghijklmnopqrstuvwxyz", "model": "deepseek-reasoner"},
    )

    assert post_res.status_code == 200
    assert post_res.json() == {
        "deepseek_model": "deepseek-reasoner",
        "deepseek_key_masked": "sk-****wxyz",
    }

    get_res = c.get("/api/workbench/settings")

    assert get_res.status_code == 200
    assert get_res.json() == {
        "deepseek_model": "deepseek-reasoner",
        "deepseek_key_masked": "sk-****wxyz",
    }

    settings_path = tmp_path / "fs" / "workbench-settings.json"
    settings_text = settings_path.read_text(encoding="utf-8")
    assert "api_key" not in settings_text
    assert "abcdefghijklmnopqrstuvwxyz" not in settings_text
    assert json.loads(settings_text) == {"deepseek_model": "deepseek-reasoner"}


def test_workbench_settings_get_without_key_returns_none(tmp_path, monkeypatch):
    c = client(tmp_path)

    def fake_read_secret(service, account):
        raise KeychainError("missing")

    monkeypatch.setattr(routes_workbench, "read_secret", fake_read_secret)

    res = c.get("/api/workbench/settings")

    assert res.status_code == 200
    assert res.json() == {
        "deepseek_model": "deepseek-chat",
        "deepseek_key_masked": None,
    }


def test_workbench_settings_save_failure_does_not_touch_keychain(tmp_path, monkeypatch):
    c = client(tmp_path)
    calls = {"save_secret": 0}

    def fake_save_settings(path, settings):
        raise OSError("disk full")

    def fake_save_secret(service, account, secret):
        calls["save_secret"] += 1

    monkeypatch.setattr(routes_workbench, "save_settings", fake_save_settings)
    monkeypatch.setattr(routes_workbench, "save_secret", fake_save_secret)

    res = c.post(
        "/api/workbench/settings/deepseek",
        json={"api_key": "sk-abcdefghijklmnopqrstuvwxyz", "model": "deepseek-reasoner"},
    )

    assert res.status_code == 500
    assert calls["save_secret"] == 0
    settings_path = tmp_path / "fs" / "workbench-settings.json"
    assert not settings_path.exists()


def test_workbench_settings_test_connection(tmp_path, monkeypatch):
    c = client(tmp_path)
    calls = {}
    settings_path = tmp_path / "fs" / "workbench-settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"deepseek_model": "deepseek-reasoner"}), encoding="utf-8")

    monkeypatch.setattr(
        routes_workbench,
        "read_secret",
        lambda service, account: "sk-test-12345678",
    )

    class FakeDeepSeekClient:
        def __init__(self, api_key, model):
            calls["api_key"] = api_key
            calls["model"] = model

        def complete(self, prompt, timeout):
            calls["prompt"] = prompt
            calls["timeout"] = timeout
            return "ok"

    monkeypatch.setattr(routes_workbench, "DeepSeekClient", FakeDeepSeekClient)

    res = c.post("/api/workbench/settings/deepseek/test")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    assert calls == {
        "api_key": "sk-test-12345678",
        "model": "deepseek-reasoner",
        "prompt": "请只回复 ok",
        "timeout": 30,
    }


def test_workbench_settings_test_connection_requires_key(tmp_path, monkeypatch):
    c = client(tmp_path)

    def fake_read_secret(service, account):
        raise KeychainError("missing")

    monkeypatch.setattr(routes_workbench, "read_secret", fake_read_secret)

    res = c.post("/api/workbench/settings/deepseek/test")

    assert res.status_code == 400
    assert res.json()["detail"] == "deepseek api key not configured"
