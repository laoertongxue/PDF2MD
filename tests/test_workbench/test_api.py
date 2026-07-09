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


def test_create_course_and_list(tmp_path):
    c = client(tmp_path)
    res = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "MBA", "root_dir": str(tmp_path / "out")},
    )
    assert res.status_code == 200
    course_id = res.json()["id"]

    res = c.get("/api/workbench/courses")
    assert res.status_code == 200
    assert res.json()[0]["id"] == course_id


def test_confirm_chapter_then_run_pipeline(tmp_path):
    c = client(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(tmp_path / "out")},
    ).json()
    source_md = tmp_path / "source.md"
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


def test_detect_chapters_uses_safe_single_level_source_dir(tmp_path):
    c = client(tmp_path)
    root_dir = tmp_path / "out"
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root_dir)},
    ).json()
    source_md = tmp_path / "source.md"
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
    root_dir = tmp_path / "out"
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root_dir)},
    ).json()
    source_md = tmp_path / "source.md"
    source_md.write_text("## 01-战略选择\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": ".."},
    ).json()

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")

    assert res.status_code == 200
    assert (root_dir / "source" / "0-01-战略选择.md").exists()
    assert not (root_dir.parent / "0-01-战略选择.md").exists()
