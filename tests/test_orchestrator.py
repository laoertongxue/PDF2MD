import os
from pathlib import Path

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def make_orchestrator(tmp_path):
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(tmp_path / "x.db"))
    return orch, repo, fs, conn


def test_parse_file_creates_merged_md(tmp_path):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    assert result["status"] == "COMPLETED"
    merged = Path(result["merged_md_path"])
    assert merged.exists()
    text = merged.read_text()
    assert "▸ AI 解读" in text
    assert "```mermaid" in text


def test_parse_file_returns_task_id(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    assert "task_id" in result
    assert len(result["task_id"]) == 36  # uuid


def test_parse_file_records_sections_count(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    md = "## A\n\nfoo\n\n## B\n\nbar\n"
    f = tmp_path / "in.md"
    f.write_text(md)
    result = orch.parse_file(str(f))
    assert result["sections"] >= 2


def test_file_cache_hit_second_parse(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    r1 = orch.parse_file(str(sample))
    r2 = orch.parse_file(str(sample))
    assert r2["cached"] is True
    assert r1["task_id"] == r2["task_id"]


def test_resume_completes_pending_sections(tmp_path):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    task_id = result["task_id"]
    # 人为破坏：把第一节标记为 PENDING 并清空 ai_artifact
    sections = repo.list_sections(task_id)
    if sections:
        repo.update_section_ai_status(sections[0].id, "PENDING")
        conn.execute("DELETE FROM ai_artifacts WHERE section_id = ?", (sections[0].id,))
        conn.commit()
    orch.resume(task_id)
    sections2 = repo.list_sections(task_id)
    assert all(s.ai_status == "COMPLETED" for s in sections2)
