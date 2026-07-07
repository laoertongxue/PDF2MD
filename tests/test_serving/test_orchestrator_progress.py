import os
from pathlib import Path

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def make_orchestrator(tmp_path, on_progress=None):
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    return (
        Orchestrator(
            repo=repo,
            fs=fs,
            llm=StubLLMClient(),
            db_path=str(tmp_path / "x.db"),
            on_progress=on_progress,
        ),
        repo,
        fs,
        conn,
    )


def test_on_progress_none_is_default(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    assert orch.on_progress is None


def test_on_progress_called_on_state_changes(tmp_path):
    events = []

    def cb(task_id, event_kind, payload):
        events.append((event_kind, payload.get("status")))

    orch, *_ = make_orchestrator(tmp_path, on_progress=cb)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    orch.parse_file(sample)
    kinds = [e[0] for e in events]
    assert "TASK_STATE" in kinds
    statuses = [e[1] for e in events if e[0] == "TASK_STATE"]
    assert "PARSING" in statuses
    assert "COMPLETED" in statuses


def test_on_progress_does_not_break_cli(tmp_path):
    """CLI 不传 on_progress，应仍能跑通（兼容 #2 行为）"""
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(tmp_path / "x.db"))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    result = orch.parse_file(sample)
    assert result["status"] == "COMPLETED"
