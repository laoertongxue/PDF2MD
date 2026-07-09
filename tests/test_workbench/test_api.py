from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench.schema import apply_workbench_schema


def client(tmp_path):
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

    return TestClient(build_app(factory))


def course_root(tmp_path):
    root = tmp_path / "fs" / "workbench-courses" / "out"
    root.mkdir(parents=True, exist_ok=True)
    return root


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


def test_create_course_rejects_root_outside_workbench_base(tmp_path):
    c = client(tmp_path)

    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": str(tmp_path / "outside")},
    )

    assert res.status_code == 400


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
