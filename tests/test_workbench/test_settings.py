import subprocess

import pytest

from parsing_core.workbench.keychain import KeychainError, mask_secret, read_secret, save_secret
from parsing_core.workbench.settings import WorkbenchSettings, load_settings, save_settings


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "workbench-settings.json"
    save_settings(path, WorkbenchSettings(deepseek_model="deepseek-v4-pro"))

    assert load_settings(path).deepseek_model == "deepseek-v4-pro"


def test_settings_default_when_missing(tmp_path):
    assert load_settings(tmp_path / "missing.json").deepseek_model == "deepseek-v4-pro"


def test_mask_secret():
    assert mask_secret("sk-1234567890") == "sk-****7890"
    assert mask_secret("") is None


def test_keychain_save_and_read(monkeypatch):
    calls = []
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "find-generic-password" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="sk-test\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    save_secret("pdf2md.deepseek", "api-key", "sk-test")
    assert read_secret("pdf2md.deepseek", "api-key") == "sk-test"
    assert any("add-generic-password" in cmd for cmd in calls)


def test_keychain_read_missing(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(KeychainError):
        read_secret("pdf2md.deepseek", "api-key")
