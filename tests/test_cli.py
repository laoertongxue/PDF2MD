import json
import subprocess
import sys
from pathlib import Path


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
