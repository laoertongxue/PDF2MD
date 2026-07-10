import json

import pytest
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api import routes_workbench
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench import pipeline as workbench_pipeline
from parsing_core.workbench.keychain import KeychainError
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


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
    assert res.json()[0]["course_id"] == course["id"]
    assert res.json()[0]["chapter_id"] == chapter_id
    assert res.json()[0]["kind"] == "topic"

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
