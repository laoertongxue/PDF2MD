from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from parsing_core.workbench import codex_cli
from parsing_core.workbench.keychain import KeychainError, mask_secret, read_secret
from parsing_core.workbench.settings import WorkbenchSettings

BAIDU_KEYCHAIN_SERVICE = "pdf2md.baidu"
BAIDU_KEYCHAIN_ACCOUNT = "api-key"
DEEPSEEK_KEYCHAIN_SERVICE = "pdf2md.deepseek"
DEEPSEEK_KEYCHAIN_ACCOUNT = "api-key"
BAIDU_ENVIRONMENT_VARIABLE = "PDF2MD_BAIDU_API_KEY"
CODEX_ENVIRONMENT_VARIABLE = "CODEX_CLI_PATH"
CODEX_DETECTION_DIRECTORIES = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.npm-global/bin",
    "~/Library/pnpm",
    "~/.bun/bin",
)

READY = "ready"
MISSING = "missing"
INVALID = "invalid"
OPTIONAL = "optional"


def resolve_baidu_api_key() -> str | None:
    try:
        secret = read_secret(BAIDU_KEYCHAIN_SERVICE, BAIDU_KEYCHAIN_ACCOUNT).strip()
    except KeychainError:
        secret = ""
    if secret:
        return secret
    fallback = os.environ.get(BAIDU_ENVIRONMENT_VARIABLE, "").strip()
    return fallback or None


def masked_baidu_key() -> str | None:
    return mask_secret(resolve_baidu_api_key() or "")


def _codex_candidates(configured: str | None) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if configured:
        candidates.append(("settings", configured))
    environment_path = os.environ.get(CODEX_ENVIRONMENT_VARIABLE, "").strip()
    if environment_path:
        candidates.append(("environment", environment_path))
    for directory in CODEX_DETECTION_DIRECTORIES:
        candidates.append(("detected", str(Path(directory).expanduser() / "codex")))
    return candidates


def _codex_candidate_state(source: str, candidate: str) -> dict[str, Any]:
    path = Path(candidate).expanduser()
    try:
        codex_cli.resolve_codex_path(candidate)
    except codex_cli.CodexCliError:
        pass
    else:
        return {"state": READY, "path": str(path), "source": source, "detail_code": None}

    try:
        info = path.lstat()
    except OSError:
        return {
            "state": MISSING,
            "path": str(path),
            "source": None,
            "detail_code": "codex_not_found",
        }
    if stat.S_ISLNK(info.st_mode):
        detail_code = "codex_is_symlink"
    elif not stat.S_ISREG(info.st_mode):
        detail_code = "codex_not_regular_file"
    elif not stat.S_IMODE(info.st_mode) & 0o111:
        detail_code = "codex_not_executable"
    else:
        detail_code = "codex_layout_unsupported"
    return {
        "state": MISSING if detail_code == "codex_layout_unsupported" else INVALID,
        "path": str(path),
        "source": source,
        "detail_code": detail_code,
    }


def codex_state(settings: WorkbenchSettings) -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "state": MISSING,
        "path": None,
        "source": None,
        "detail_code": "codex_not_found",
    }
    for source, candidate in _codex_candidates(settings.codex_cli_path):
        state = _codex_candidate_state(source, candidate)
        if state["state"] is READY:
            return state
        fallback = state
        if state["detail_code"] != "codex_not_found":
            return state
    return fallback


def _deepseek_state(*, last_test_ok: bool | None) -> dict[str, Any]:
    try:
        secret = read_secret(DEEPSEEK_KEYCHAIN_SERVICE, DEEPSEEK_KEYCHAIN_ACCOUNT).strip()
    except KeychainError:
        secret = ""
    if not secret:
        return {"state": MISSING, "last_test_ok": None, "detail_code": "deepseek_key_missing"}
    return {"state": READY, "last_test_ok": last_test_ok, "detail_code": None}


def _baidu_state() -> dict[str, Any]:
    key = resolve_baidu_api_key()
    if key is None:
        return {"state": OPTIONAL, "masked": None}
    return {"state": READY, "masked": mask_secret(key)}


def _data_dir_state(data_dir: Path) -> dict[str, Any]:
    return {
        "path": str(data_dir),
        "writable": os.access(data_dir, os.W_OK),
    }


def build_environment_report(
    *,
    settings: WorkbenchSettings,
    app_version: str,
    data_dir: Path,
    vision_available: bool,
    last_deepseek_test_ok: bool | None = None,
) -> dict[str, Any]:
    return {
        "app_version": app_version,
        "data_dir": _data_dir_state(data_dir),
        "deepseek": _deepseek_state(last_test_ok=last_deepseek_test_ok),
        "codex": codex_state(settings),
        "baidu": _baidu_state(),
        "vision": {"state": READY if vision_available else MISSING},
    }
