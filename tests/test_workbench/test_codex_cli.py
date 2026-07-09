import subprocess

import pytest

from parsing_core.workbench.codex_cli import CodexCliError, CodexCliExecutor, resolve_codex_path


def test_resolve_codex_path_prefers_env(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_PATH", "/custom/codex")
    monkeypatch.setattr("os.path.isfile", lambda path: path == "/custom/codex")
    monkeypatch.setattr("os.access", lambda path, mode: path == "/custom/codex")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex")

    assert resolve_codex_path() == "/custom/codex"


def test_resolve_codex_path_rejects_missing_env_path(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_PATH", "/missing/codex")
    monkeypatch.setattr("os.path.isfile", lambda path: False)
    monkeypatch.setattr("os.access", lambda path, mode: False)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path()


def test_codex_cli_executor_reads_output_file(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        output_path = cmd[cmd.index("--output-last-message") + 1]
        tmp_path.joinpath(output_path).write_text(
            "# 精读结果\n\n```mermaid\nflowchart TD\n  A --> B\n```",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    executor = CodexCliExecutor("/usr/bin/codex", tmp_path)
    output = executor.run("round-1", "# task")

    assert seen["kwargs"]["input"] == "# task"
    assert seen["cmd"][seen["cmd"].index("--output-last-message") + 1] == str(
        tmp_path / "codex-round-1-output.md"
    )
    assert not (tmp_path / "codex-round-1-input.md").exists()
    assert "flowchart TD" in output


def test_codex_cli_executor_raises_when_output_missing(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    executor = CodexCliExecutor("/usr/bin/codex", tmp_path)
    with pytest.raises(CodexCliError, match="codex output file missing"):
        executor.run("round-1", "# task")
