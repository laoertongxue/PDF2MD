import json
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(args):
    cmd = [sys.executable, "-m", "parsing_core.cli", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=".")
    return r


def test_parse_md(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = run_cli(["parse", sample])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "COMPLETED"
    assert Path(out["merged_md_path"]).exists()


def test_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample])
    tid = json.loads(r1.stdout)["task_id"]
    r2 = run_cli(["status", tid])
    out = json.loads(r2.stdout)
    assert out["status"] == "COMPLETED"


def test_list(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    run_cli(["parse", sample])
    r = run_cli(["list"])
    out = json.loads(r.stdout)
    assert len(out) >= 1
    assert out[0]["status"] == "COMPLETED"


def test_resume(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample])
    tid = json.loads(r1.stdout)["task_id"]
    r2 = run_cli(["resume", tid])
    out = json.loads(r2.stdout)
    assert out["status"] in ("COMPLETED", "ALREADY_COMPLETED")


def test_purge(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample])
    tid = json.loads(r1.stdout)["task_id"]
    r2 = run_cli(["purge", tid])
    out = json.loads(r2.stdout)
    assert out["purged"] is True


def test_main_dispatches_subcommands_in_process(monkeypatch, capsys):
    import parsing_core.cli as cli_module

    calls = []

    class FakeOrchestrator:
        def parse_file(self, file_path, force=False):
            calls.append(("parse", file_path, force))
            return {"task_id": "t1", "status": "COMPLETED"}

        def resume(self, task_id):
            calls.append(("resume", task_id))
            return {"task_id": task_id, "status": "COMPLETED"}

        def status(self, task_id):
            calls.append(("status", task_id))
            return {"task_id": task_id, "status": "COMPLETED"}

        def list_all(self):
            calls.append(("list",))
            return []

        def purge(self, task_id):
            calls.append(("purge", task_id))
            return {"task_id": task_id, "purged": True}

    monkeypatch.setattr(cli_module, "_build_orchestrator", lambda: FakeOrchestrator())

    cases = [
        (["parse", "/tmp/book.md"], {"task_id": "t1", "status": "COMPLETED"}),
        (["parse", "/tmp/book.md", "--force"], {"task_id": "t1", "status": "COMPLETED"}),
        (["resume", "t1"], {"task_id": "t1", "status": "COMPLETED"}),
        (["status", "t1"], {"task_id": "t1", "status": "COMPLETED"}),
        (["list"], []),
        (["purge", "t1"], {"task_id": "t1", "purged": True}),
    ]
    for argv, expected in cases:
        monkeypatch.setattr(sys, "argv", ["parsing-core", *argv])
        assert cli_module.main() == 0
        assert json.loads(capsys.readouterr().out) == expected

    assert calls == [
        ("parse", "/tmp/book.md", False),
        ("parse", "/tmp/book.md", True),
        ("resume", "t1"),
        ("status", "t1"),
        ("list",),
        ("purge", "t1"),
    ]


def test_main_rejects_unknown_subcommand(monkeypatch):
    import parsing_core.cli as cli_module

    monkeypatch.setattr(sys, "argv", ["parsing-core", "unknown"])
    with pytest.raises(SystemExit):
        cli_module.main()
