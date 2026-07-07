# tests/test_integration.py
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(args, env):
    r = subprocess.run(
        [sys.executable, "-m", "parsing_core.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=".",
    )
    return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return dict(os.environ)


def test_end_to_end_md(env):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = run_cli(["parse", sample], env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    merged = Path(out["merged_md_path"]).read_text(encoding="utf-8")
    assert "▸ AI 解读" in merged
    assert "```mermaid" in merged
    assert "---" in merged


def test_cache_hit_second_call(env):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample], env)
    r2 = run_cli(["parse", sample], env)
    assert json.loads(r1.stdout)["cached"] is False
    assert json.loads(r2.stdout)["cached"] is True
    assert json.loads(r1.stdout)["task_id"] == json.loads(r2.stdout)["task_id"]


def test_resume_after_partial(env):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample], env)
    tid = json.loads(r1.stdout)["task_id"]
    # 模拟中断：直接调 resume（任务已完成，应返回 ALREADY_COMPLETED）
    r2 = run_cli(["resume", tid], env)
    out = json.loads(r2.stdout)
    assert out["status"] in ("COMPLETED", "ALREADY_COMPLETED")
