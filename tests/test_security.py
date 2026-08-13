import json
import os
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving import serve
from parsing_core.serving.api.deps import session_token_matches
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench.schema import apply_workbench_schema

TEST_SESSION_TOKEN = "test-session-token-0123456789abcdef0123456789abcdef"
AUTH_HEADERS = {
    "Origin": "http://localhost:1420",
    "X-PDF2MD-Session": TEST_SESSION_TOKEN,
}
MAX_REQUEST_BODY_BYTES = 1_048_576


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "serve.db"

    def orch_factory():
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        return Orchestrator(
            repo=Repository(conn),
            fs=FsLayout(base_dir=str(tmp_path / "data")),
            llm=StubLLMClient(),
            db_path=str(db_path),
        )

    return TestClient(
        serve.build_app(
            orch_factory=orch_factory,
            max_global_concurrency=2,
            session_token=TEST_SESSION_TOKEN,
        ),
        headers=AUTH_HEADERS,
    )


def _confirmed_chapter(client: TestClient, root: Path) -> str:
    course = client.post(
        "/api/workbench/courses",
        json={"title": "安全测试", "description": "", "root_dir": str(root)},
    ).json()
    source_path = root / "source.md"
    source_path.write_text("## 第一章\n安全内容。", encoding="utf-8")
    source = client.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_path), "title": "教材"},
    ).json()
    chapter = client.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]
    assert client.post(f"/api/workbench/chapters/{chapter['id']}/confirm").status_code == 200
    return chapter["id"]


def test_source_path_traversal_is_rejected(tmp_path):
    client = _client(tmp_path)
    root = tmp_path / "course"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    course = client.post(
        "/api/workbench/courses",
        json={"title": "安全测试", "description": "", "root_dir": str(root)},
    ).json()

    response = client.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={
            "kind": "main",
            "file_path": str(root / ".." / outside.name),
            "title": "越界文件",
        },
    )

    assert response.status_code == 400
    assert "inside course root_dir" in response.json()["detail"]


def test_source_symlink_escape_is_rejected(tmp_path):
    client = _client(tmp_path)
    root = tmp_path / "course"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "book.md").write_text("outside", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    course = client.post(
        "/api/workbench/courses",
        json={"title": "安全测试", "description": "", "root_dir": str(root)},
    ).json()

    response = client.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(root / "linked" / "book.md"), "title": "链接文件"},
    )

    assert response.status_code == 400
    assert "inside course root_dir" in response.json()["detail"]


def test_oversized_request_body_has_stable_error_code(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/workbench/courses",
        content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": {"code": "request_too_large"}}


def test_malicious_markdown_is_rejected_without_stack_trace(tmp_path):
    client = _client(tmp_path)
    root = tmp_path / "course"
    root.mkdir()
    chapter_id = _confirmed_chapter(client, root)

    response = client.patch(
        f"/api/workbench/chapters/{chapter_id}/note-blocks/concepts",
        json={"body": "[click](javascript:alert(1))<script>alert(1)</script>", "expected_body": ""},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "unsafe_markdown"}}


def test_plain_business_text_containing_data_label_remains_valid(tmp_path):
    client = _client(tmp_path)
    root = tmp_path / "course"
    root.mkdir()
    chapter_id = _confirmed_chapter(client, root)
    assert (
        client.post(
            f"/api/workbench/chapters/{chapter_id}/run", json={"executor": "stub"}
        ).status_code
        == 200
    )
    blocks = client.get(f"/api/workbench/chapters/{chapter_id}/note-blocks").json()
    current = next(block["body"] for block in blocks if block["kind"] == "concepts")

    response = client.patch(
        f"/api/workbench/chapters/{chapter_id}/note-blocks/concepts",
        json={"body": "Data: models support better decisions.", "expected_body": current},
    )

    assert response.status_code == 200


def test_malicious_mermaid_is_rejected_without_stack_trace(tmp_path):
    client = _client(tmp_path)
    root = tmp_path / "course"
    root.mkdir()
    chapter_id = _confirmed_chapter(client, root)

    response = client.patch(
        f"/api/workbench/chapters/{chapter_id}/note-blocks/knowledge_mermaid",
        json={"body": 'flowchart LR\nA-->B\nclick A "javascript:alert(1)"', "expected_body": ""},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_mermaid"}}


def test_ready_payload_has_strict_schema_and_never_serializes_secret():
    payload = serve.build_ready_payload("127.0.0.1", 43127)
    encoded = json.dumps(payload, sort_keys=True)

    assert payload == {
        "schema": "pdf2md.sidecar.ready.v1",
        "host": "127.0.0.1",
        "port": 43127,
    }
    assert TEST_SESSION_TOKEN not in encoded


def test_two_production_starts_use_distinct_random_loopback_endpoints(tmp_path):
    processes: list[subprocess.Popen[str]] = []

    def start(instance: str, token: str) -> tuple[subprocess.Popen[str], dict]:
        env = os.environ.copy()
        env["XDG_DATA_HOME"] = str(tmp_path / instance)
        env["PDF2MD_SESSION_TOKEN"] = token
        process = subprocess.Popen(
            [sys.executable, "-m", "parsing_core.serving.serve", "--port", "0"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 15)
        assert ready, "sidecar did not emit ready JSON"
        line = process.stdout.readline().strip()
        assert token not in line
        payload = json.loads(line)
        assert isinstance(payload.get("port"), int)
        assert 1 <= payload["port"] <= 65_535
        assert payload == {
            "schema": "pdf2md.sidecar.ready.v1",
            "host": "127.0.0.1",
            "port": payload["port"],
        }
        deadline = time.monotonic() + 10
        request = urllib.request.Request(
            f"http://127.0.0.1:{payload['port']}/health",
            headers={"X-PDF2MD-Session": token},
        )
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(request, timeout=0.2) as response:
                    assert response.status == 200
                    return process, payload
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        pytest.fail("sidecar did not become healthy")

    try:
        _first, first = start("first", "a" * 64)
        _second, second = start("second", "b" * 64)
        assert first["port"] != second["port"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_missing_or_weak_production_session_fails_closed_without_echo(monkeypatch):
    monkeypatch.delenv("PDF2MD_SESSION_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="session token is required"):
        serve.session_token_from_environment()

    weak_secret = "secret-that-must-not-be-logged"
    monkeypatch.setenv("PDF2MD_SESSION_TOKEN", weak_secret)
    with pytest.raises(RuntimeError) as exc_info:
        serve.session_token_from_environment()

    assert weak_secret not in str(exc_info.value)


def test_production_session_is_removed_from_child_environment(monkeypatch):
    token = "child-process-isolation-session-token-0123456789abcdef"
    monkeypatch.setenv("PDF2MD_SESSION_TOKEN", token)

    assert serve.session_token_from_environment() == token
    assert "PDF2MD_SESSION_TOKEN" not in os.environ


def test_build_app_rejects_weak_session_token(tmp_path):
    def orch_factory():
        sub_dir = tmp_path / f"task-{time.time_ns()}"
        sub_dir.mkdir()
        conn = init_db(str(tmp_path / "serve.db"))
        apply_serve_schema(conn)
        return Orchestrator(
            Repository(conn),
            FsLayout(base_dir=str(sub_dir)),
            StubLLMClient(),
            str(tmp_path / "serve.db"),
        )

    with pytest.raises(ValueError, match="session token"):
        serve.build_app(orch_factory=orch_factory, session_token="weak")


def test_non_ascii_session_input_is_a_normal_authentication_failure():
    assert not session_token_matches("\xff", TEST_SESSION_TOKEN)
