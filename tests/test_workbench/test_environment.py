from __future__ import annotations

from pathlib import Path

from parsing_core.workbench import codex_cli
from parsing_core.workbench import environment as environment_module
from parsing_core.workbench.keychain import KeychainError
from parsing_core.workbench.settings import WorkbenchSettings


def _fake_codex(tmp_path: Path) -> Path:
    tool = tmp_path / "codex"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def test_resolve_baidu_api_key_prefers_keychain(monkeypatch):
    monkeypatch.setattr(
        environment_module,
        "read_secret",
        lambda _service, _account: "keychain-secret",
    )
    monkeypatch.setenv("PDF2MD_BAIDU_API_KEY", "env-secret")
    assert environment_module.resolve_baidu_api_key() == "keychain-secret"


def test_resolve_baidu_api_key_falls_back_to_environment(monkeypatch):
    def missing(_service: str, _account: str) -> str:
        raise KeychainError("not found")

    monkeypatch.setattr(environment_module, "read_secret", missing)
    monkeypatch.setenv("PDF2MD_BAIDU_API_KEY", "env-secret")
    assert environment_module.resolve_baidu_api_key() == "env-secret"


def test_codex_state_ready_for_valid_settings_path(monkeypatch, tmp_path):
    tool = _fake_codex(tmp_path)
    monkeypatch.setattr(codex_cli, "resolve_codex_path", lambda path=None: str(path or "codex"))
    state = environment_module.codex_state(WorkbenchSettings(codex_cli_path=str(tool)))
    assert state == {
        "state": "ready",
        "path": str(tool),
        "source": "settings",
        "detail_code": None,
    }


def test_codex_state_reports_symlink(monkeypatch, tmp_path):
    real = _fake_codex(tmp_path)
    link = tmp_path / "codex-link"
    link.symlink_to(real)
    state = environment_module.codex_state(WorkbenchSettings(codex_cli_path=str(link)))
    assert state["state"] == "invalid"
    assert state["detail_code"] == "codex_is_symlink"


def test_codex_state_reports_layout_unsupported(monkeypatch, tmp_path):
    tool = _fake_codex(tmp_path)

    def reject(_path=None):
        raise codex_cli.CodexCliError("codex cli not found")

    monkeypatch.setattr(codex_cli, "resolve_codex_path", reject)
    state = environment_module.codex_state(WorkbenchSettings(codex_cli_path=str(tool)))
    assert state["state"] == "missing"
    assert state["detail_code"] == "codex_layout_unsupported"


def test_codex_state_detects_common_directories(monkeypatch, tmp_path):
    tool = _fake_codex(tmp_path)
    monkeypatch.setattr(environment_module, "CODEX_DETECTION_DIRECTORIES", (str(tmp_path),))
    monkeypatch.setattr(codex_cli, "resolve_codex_path", lambda path=None: str(path or "codex"))
    state = environment_module.codex_state(WorkbenchSettings())
    assert state["state"] == "ready"
    assert state["source"] == "detected"
    assert state["path"] == str(tool)


def test_build_environment_report_shape(monkeypatch, tmp_path):
    tool = _fake_codex(tmp_path)
    monkeypatch.setattr(codex_cli, "resolve_codex_path", lambda path=None: str(path or "codex"))
    monkeypatch.setattr(environment_module, "read_secret", lambda *_args: "deepseek-key-1234")
    report = environment_module.build_environment_report(
        settings=WorkbenchSettings(codex_cli_path=str(tool)),
        app_version="0.1.4",
        data_dir=tmp_path,
        vision_available=True,
        last_deepseek_test_ok=True,
    )
    assert report["app_version"] == "0.1.4"
    assert report["deepseek"]["state"] == "ready"
    assert report["deepseek"]["last_test_ok"] is True
    assert report["codex"]["state"] == "ready"
    assert report["baidu"]["state"] in {"optional", "ready"}
    assert report["vision"]["state"] == "ready"
    assert report["data_dir"]["writable"] is True
