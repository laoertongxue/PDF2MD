import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench.schema import apply_workbench_schema


def _app_factory(db_path: Path, fs_root: Path):
    def factory():
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        return Orchestrator(
            Repository(conn),
            FsLayout(base_dir=str(fs_root)),
            StubLLMClient(),
            str(db_path),
        )

    return build_app(factory)


def _expect_ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_docx(path: Path, chapters: tuple[tuple[str, str], ...]) -> None:
    paragraphs = []
    for title, body in chapters:
        paragraphs.extend((
            f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>{title}</w:t></w:r></w:p>',
            f"<w:p><w:r><w:t>{body}</w:t></w:r></w:p>",
        ))
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
            'document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body></w:document>",
        )


def test_multi_textbook_topic_fusion_survives_restart_and_stales_shared_topics(tmp_path):
    db_path = tmp_path / "workbench.db"
    fs_root = tmp_path / "runtime"
    course_root = tmp_path / "人力资源管理"
    external_root = tmp_path / "外部教材"
    external_root.mkdir()
    textbook_paths = []
    for book, chapters in (
        ("教材A", (("第一章 组织与岗位", "岗位设计内容"), ("第二章 招聘", "招聘内容"))),
        ("教材B", (("第一章 组织与岗位", "组织设计内容"), ("第二章 绩效", "绩效内容"))),
    ):
        path = external_root / f"{book}.docx"
        _write_docx(path, chapters)
        textbook_paths.append(path)
    original_digests = {path: _file_digest(path) for path in textbook_paths}

    app = _app_factory(db_path, fs_root)
    with TestClient(app) as client:
        course = _expect_ok(client.post(
            "/api/workbench/courses",
            json={
                "title": "人力资源管理",
                "description": "双教材主题融合验收",
                "root_dir": str(course_root),
            },
        ))
        imported = _expect_ok(client.post(
            f"/api/workbench/courses/{course['id']}/sources/import",
            json={
                "paths": [str(path) for path in textbook_paths],
                "titles": ["教材A", "教材B"],
            },
        ))["items"]
        assert len(imported) == 2
        assert all(
            Path(item["stored_path"]).parent == course_root / "教材原文件"
            for item in imported
        )
        assert all(Path(item["stored_path"]).is_file() for item in imported)
        assert {path: _file_digest(path) for path in textbook_paths} == original_digests

        chapters_by_source = []
        for source in imported:
            chapters = _expect_ok(client.post(
                f"/api/workbench/sources/{source['source_id']}/detect-chapters"
            ))
            assert len(chapters) == 2
            chapters_by_source.append(chapters)
            for chapter in chapters:
                assert _expect_ok(client.post(
                    f"/api/workbench/chapters/{chapter['id']}/confirm"
                ))["status"] == "CONFIRMED"
                assert _expect_ok(client.post(
                    f"/api/workbench/chapters/{chapter['id']}/run",
                    json={"executor": "stub"},
                ))["status"] == "COMPLETED"

        generated = _expect_ok(client.post(
            f"/api/workbench/courses/{course['id']}/topics/generate",
            json={"executor": "stub"},
        ))
        assert len(generated) == 4
        first = generated[0]
        second = generated[1]
        shared_chapter_id = chapters_by_source[0][0]["id"]
        first_mapping = [
            shared_chapter_id,
            chapters_by_source[0][1]["id"],
            chapters_by_source[1][0]["id"],
        ]
        second_mapping = [shared_chapter_id, chapters_by_source[1][1]["id"]]
        assert set(_expect_ok(client.put(
            f"/api/workbench/topics/{first['id']}/chapters",
            json={"chapter_ids": first_mapping},
        ))["chapter_ids"]) == set(first_mapping)
        assert set(_expect_ok(client.put(
            f"/api/workbench/topics/{second['id']}/chapters",
            json={"chapter_ids": second_mapping},
        ))["chapter_ids"]) == set(second_mapping)
        for unused in generated[2:]:
            assert client.delete(f"/api/workbench/topics/{unused['id']}").status_code == 204

        confirmed = _expect_ok(client.post(
            f"/api/workbench/courses/{course['id']}/topics/confirm"
        ))
        assert len(confirmed) == 2
        assert all(topic["confirmed"] for topic in confirmed)
        for topic in confirmed:
            result = _expect_ok(client.post(
                f"/api/workbench/topics/{topic['id']}/run",
                json={"executor": "stub"},
            ))
            assert result["status"] == "COMPLETED"

    with TestClient(_app_factory(db_path, fs_root)) as restarted:
        topics = _expect_ok(restarted.get(
            f"/api/workbench/courses/{course['id']}/topics"
        ))
        assert len(topics) == 2
        assert [set(topic["chapter_ids"]) for topic in topics] == [
            set(first_mapping),
            set(second_mapping),
        ]

        snapshots = {}
        for topic in topics:
            blocks = _expect_ok(restarted.get(
                f"/api/workbench/topics/{topic['id']}/note-blocks"
            ))
            cards = _expect_ok(restarted.get(
                f"/api/workbench/topics/{topic['id']}/cards"
            ))
            runs = _expect_ok(restarted.get(
                f"/api/workbench/topics/{topic['id']}/runs"
            ))
            assert len(blocks) == 14
            assert len([block for block in blocks if block["kind"].endswith("_mermaid")]) == 2
            assert 8 <= len(cards) <= 12
            assert runs and all(run["status"] == "COMPLETED" for run in runs)
            topic_dir = next((course_root / "课程主题").glob(f"{topic['seq'] + 1:02d}-*"))
            markdown_path = topic_dir / "intensive-note.md"
            markdown = markdown_path.read_text(encoding="utf-8")
            assert sum(f"## {section}." in markdown for section in range(1, 16)) == 15
            assert markdown.count("```mermaid") == 2
            snapshots[topic["id"]] = (blocks, cards, markdown)

        assert _expect_ok(restarted.post(
            f"/api/workbench/chapters/{shared_chapter_id}/confirm"
        ))["status"] == "CONFIRMED"
        assert _expect_ok(restarted.post(
            f"/api/workbench/chapters/{shared_chapter_id}/run",
            json={"executor": "stub"},
        ))["status"] == "COMPLETED"

        stale_topics = _expect_ok(restarted.get(
            f"/api/workbench/courses/{course['id']}/topics"
        ))
        assert [topic["status"] for topic in stale_topics] == ["STALE", "STALE"]
        for topic in stale_topics:
            assert _expect_ok(restarted.get(
                f"/api/workbench/topics/{topic['id']}/note-blocks"
            )) == snapshots[topic["id"]][0]
            assert _expect_ok(restarted.get(
                f"/api/workbench/topics/{topic['id']}/cards"
            )) == snapshots[topic["id"]][1]
            topic_dir = next((course_root / "课程主题").glob(f"{topic['seq'] + 1:02d}-*"))
            current_markdown = (topic_dir / "intensive-note.md").read_text(encoding="utf-8")
            assert current_markdown == snapshots[topic["id"]][2]
