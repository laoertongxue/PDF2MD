import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(data_root: Path) -> tuple[subprocess.Popen[str], httpx.Client]:
    port = _unused_loopback_port()
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(data_root)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "parsing_core.serving.serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"server exited early\nstdout={stdout}\nstderr={stderr}")
        try:
            if client.get("/health").status_code == 200:
                return process, client
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    process.terminate()
    raise AssertionError("server did not become healthy")


def _stop_server(process: subprocess.Popen[str], client: httpx.Client) -> None:
    client.close()
    process.terminate()
    process.wait(timeout=10)


def _ok(response: httpx.Response):
    response.raise_for_status()
    return response.json()


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
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body></w:document>",
        )


def test_real_uvicorn_multi_textbook_topic_fusion_survives_restart(tmp_path):
    data_root = tmp_path / "data"
    course_root = tmp_path / "人力资源管理"
    external = tmp_path / "外部教材"
    external.mkdir()
    textbook_paths = []
    for book, chapters in (
        ("教材A", (("第一章 组织与岗位", "岗位设计内容"), ("第二章 招聘", "招聘内容"))),
        ("教材B", (("第一章 组织与岗位", "组织设计内容"), ("第二章 绩效", "绩效内容"))),
    ):
        path = external / f"{book}.docx"
        _write_docx(path, chapters)
        textbook_paths.append(path)

    process, client = _start_server(data_root)
    try:
        course = _ok(client.post(
            "/api/workbench/courses",
            json={"title": "人力资源管理", "description": "双教材", "root_dir": str(course_root)},
        ))
        imported = _ok(client.post(
            f"/api/workbench/courses/{course['id']}/sources/import",
            json={"paths": [str(path) for path in textbook_paths], "titles": ["教材A", "教材B"]},
        ))["items"]
        assert len(imported) == 2

        chapters_by_source = []
        for source in imported:
            chapters = _ok(client.post(
                f"/api/workbench/sources/{source['source_id']}/detect-chapters"
            ))
            assert len(chapters) == 2
            chapters_by_source.append(chapters)
            for chapter in chapters:
                assert _ok(client.post(
                    f"/api/workbench/chapters/{chapter['id']}/confirm"
                ))["status"] == "CONFIRMED"
                assert _ok(client.post(
                    f"/api/workbench/chapters/{chapter['id']}/run",
                    json={"executor": "stub"},
                ))["status"] == "COMPLETED"
                blocks = _ok(client.get(
                    f"/api/workbench/chapters/{chapter['id']}/note-blocks"
                ))
                assert {block["kind"] for block in blocks} == {
                    "summary",
                    "concepts",
                    "plain_explain",
                    "application",
                    "knowledge_mermaid",
                    "application_mermaid",
                    "reflection",
                }

        generated = _ok(client.post(
            f"/api/workbench/courses/{course['id']}/topics/generate",
            json={"executor": "stub"},
        ))
        assert len(generated) == 4
        shared_chapter_id = chapters_by_source[0][0]["id"]
        mappings = [
            [shared_chapter_id, chapters_by_source[0][1]["id"], chapters_by_source[1][0]["id"]],
            [shared_chapter_id, chapters_by_source[1][1]["id"]],
        ]
        topics = [
            _ok(client.put(
                f"/api/workbench/topics/{topic['id']}/chapters",
                json={"chapter_ids": chapter_ids},
            ))
            for topic, chapter_ids in zip(generated[:2], mappings, strict=True)
        ]
        for unused in generated[2:]:
            assert client.delete(f"/api/workbench/topics/{unused['id']}").status_code == 204
        assert all(shared_chapter_id in topic["chapter_ids"] for topic in topics)
        confirmed = _ok(client.post(f"/api/workbench/courses/{course['id']}/topics/confirm"))
        assert len(confirmed) == 2
        assert all(topic["confirmed"] for topic in confirmed)
        for topic in confirmed:
            assert _ok(client.post(
                f"/api/workbench/topics/{topic['id']}/run", json={"executor": "stub"}
            ))["status"] == "COMPLETED"
    finally:
        _stop_server(process, client)

    process, client = _start_server(data_root)
    try:
        restarted = _ok(client.get(f"/api/workbench/courses/{course['id']}/topics"))
        assert len(restarted) == 2
        assert [set(topic["chapter_ids"]) for topic in restarted] == [set(ids) for ids in mappings]
        snapshots = {}
        for topic in restarted:
            blocks = _ok(client.get(f"/api/workbench/topics/{topic['id']}/note-blocks"))
            cards = _ok(client.get(f"/api/workbench/topics/{topic['id']}/cards"))
            runs = _ok(client.get(f"/api/workbench/topics/{topic['id']}/runs"))
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

        assert _ok(client.post(
            f"/api/workbench/chapters/{shared_chapter_id}/confirm"
        ))["status"] == "CONFIRMED"
        assert _ok(client.post(
            f"/api/workbench/chapters/{shared_chapter_id}/run", json={"executor": "stub"}
        ))["status"] == "COMPLETED"
        stale = _ok(client.get(f"/api/workbench/courses/{course['id']}/topics"))
        assert [topic["status"] for topic in stale] == ["STALE", "STALE"]
        for topic in stale:
            assert _ok(client.get(
                f"/api/workbench/topics/{topic['id']}/note-blocks"
            )) == snapshots[topic["id"]][0]
            assert _ok(client.get(
                f"/api/workbench/topics/{topic['id']}/cards"
            )) == snapshots[topic["id"]][1]
            topic_dir = next((course_root / "课程主题").glob(f"{topic['seq'] + 1:02d}-*"))
            current_markdown = (topic_dir / "intensive-note.md").read_text(encoding="utf-8")
            assert current_markdown == snapshots[topic["id"]][2]
    finally:
        _stop_server(process, client)
