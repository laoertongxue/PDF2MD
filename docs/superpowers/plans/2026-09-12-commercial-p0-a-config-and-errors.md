# P0-A 开箱可用（配置闭环与可操作错误）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让正式打包的 macOS 应用可以探测、配置并校验 DeepSeek / Codex CLI / 百度 OCR 依赖，首启提供非阻塞环境自检卡，核心失败返回可操作的结构化错误。

**架构：** 新增 `workbench/environment.py` 聚合依赖探测与百度 Key 解析；在 FastAPI 增加 `GET /api/workbench/environment` 与 Codex/百度配置端点；OCR/主题 factory 读取设置中的 Codex 路径与 Keychain 中的百度 Key；前端新增环境自检卡、设置区块与错误码到文案/动作的映射层。

**技术栈：** Python 3.12 + FastAPI + Pydantic v2 + macOS Keychain（`security`）+ React 18 + TypeScript + Vitest + Testing Library。

**范围：** 本计划覆盖设计文档 `docs/superpowers/specs/2026-09-12-commercial-p0-a-onboarding-design.md` 的 5.1、5.2、5.3、5.5。5.4（百度页级隔离）是独立子系统，另见后续计划 `2026-09-12-commercial-p0-a-ocr-review.md`。

**约定：**
- 后端测试命令：`.venv/bin/python -m pytest <path> -q`
- 前端测试命令：`npm test --prefix parsing-core-app -- --run <file>`（或 `cd parsing-core-app && npx vitest run <file>`）
- 每次 commit 前跑对应文件测试与 `ruff`。
- 所有新增代码遵循现有模式；不要重构无关代码。

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `src/parsing_core/workbench/environment.py` | 创建 | 依赖探测（Codex/DeepSeek/百度/Vision/数据目录）、百度 Key 解析（Keychain → env）、环境报告构造 |
| `tests/test_workbench/test_environment.py` | 创建 | 上述模块单测 |
| `src/parsing_core/serving/models/api.py` | 修改 | 新增 `EnvironmentStatusItem`、`EnvironmentReport` 响应模型 |
| `src/parsing_core/serving/api/routes_workbench.py` | 修改 | environment/settings 路由；OCR factory 接线；错误码 |
| `src/parsing_core/serving/api/routes_topics.py` | 修改 | 主题 factory 接线 Codex 设置 |
| `src/parsing_core/serving/api/errors.py` | 创建 | 结构化错误 helper |
| `tests/test_workbench/test_api.py` | 修改 | 路由测试（环境、配置、错误码） |
| `parsing-core-app/src/api/workbenchTypes.ts` | 修改 | `WorkbenchSettings` 补字段；环境报告类型 |
| `parsing-core-app/src/api/workbench.ts` | 修改 | 环境/配置 API；错误 detail 结构化解析 |
| `parsing-core-app/src/api/errorMessages.ts` | 创建 | 错误码 → 文案 + 动作映射 |
| `parsing-core-app/src/api/workbench.test.ts` | 修改 | 新 API 与错误解析测试 |
| `parsing-core-app/src/components/workbench/Settings.tsx` | 修改 | Codex 路径与百度 Key 配置区块 |
| `parsing-core-app/src/components/workbench/Settings.test.tsx` | 创建 | 设置页测试 |
| `parsing-core-app/src/components/workbench/EnvironmentCard.tsx` | 创建 | 环境自检卡 |
| `parsing-core-app/src/components/workbench/EnvironmentCard.test.tsx` | 创建 | 自检卡测试 |
| `parsing-core-app/src/components/workbench/CourseList.tsx` | 修改 | 挂载自检卡 |

---

## 任务 1：环境探测模块

**文件：**
- 创建：`src/parsing_core/workbench/environment.py`
- 测试：`tests/test_workbench/test_environment.py`

- [x] **步骤 1：编写失败的测试**

创建 `tests/test_workbench/test_environment.py`：

```python
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from parsing_core.workbench import environment as environment_module
from parsing_core.workbench import codex_cli
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
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_environment.py -q`

预期：FAIL，`ModuleNotFoundError: No module named 'parsing_core.workbench.environment'`

- [x] **步骤 3：编写最少实现代码**

创建 `src/parsing_core/workbench/environment.py`：

```python
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
        info = path.lstat()
    except OSError:
        return {"state": MISSING, "path": str(path), "source": None, "detail_code": "codex_not_found"}
    if stat.S_ISLNK(info.st_mode):
        return {"state": INVALID, "path": str(path), "source": source, "detail_code": "codex_is_symlink"}
    if not stat.S_ISREG(info.st_mode):
        return {
            "state": INVALID,
            "path": str(path),
            "source": source,
            "detail_code": "codex_not_regular_file",
        }
    if not stat.S_IMODE(info.st_mode) & 0o111:
        return {
            "state": INVALID,
            "path": str(path),
            "source": source,
            "detail_code": "codex_not_executable",
        }
    try:
        codex_cli.resolve_codex_path(str(path))
    except codex_cli.CodexCliError:
        return {
            "state": MISSING,
            "path": str(path),
            "source": source,
            "detail_code": "codex_layout_unsupported",
        }
    return {"state": READY, "path": str(path), "source": source, "detail_code": None}


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
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_environment.py -q`
预期：PASS（7 个用例）

- [x] **步骤 5：Lint 与类型检查**

运行：`.venv/bin/ruff check src/parsing_core/workbench/environment.py tests/test_workbench/test_environment.py && .venv/bin/mypy src/parsing_core`
预期：无错误

- [x] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/environment.py tests/test_workbench/test_environment.py
git commit -m "feat(workbench): add provider environment probe module"
```

---

## 任务 2：环境与配置 API 路由

**文件：**
- 修改：`src/parsing_core/serving/models/api.py`（追加在 `BaiduSettingsRequest` 之后）
- 修改：`src/parsing_core/serving/api/routes_workbench.py`（追加在 `/settings/deepseek/test` 路由之后）
- 测试：`tests/test_workbench/test_api.py`（文件末尾追加）

- [x] **步骤 1：编写失败的测试**

在 `tests/test_workbench/test_api.py` 末尾追加（沿用文件内已有的 `client(tmp_path)`、`AUTH_HEADERS`、`course_root` helper）：

```python
def test_environment_reports_codex_and_baidu_states(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    monkeypatch.setattr(
        routes_workbench.environment_module,
        "read_secret",
        lambda *_args: "deepseek-key-1234",
    )
    response = test_client.get("/api/workbench/environment", headers=AUTH_HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["deepseek"]["state"] == "ready"
    assert payload["baidu"]["state"] in {"optional", "ready"}
    assert payload["codex"]["state"] in {"ready", "missing", "invalid"}


def test_codex_settings_rejects_symlink_and_accepts_valid_file(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    real = tmp_path / "codex"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    link = tmp_path / "codex-link"
    link.symlink_to(real)
    monkeypatch.setattr(
        routes_workbench,
        "resolve_codex_path",
        lambda path=None: str(path or "codex"),
    )
    rejected = test_client.post(
        "/api/workbench/settings/codex",
        json={"path": str(link)},
        headers=AUTH_HEADERS,
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "codex_invalid"
    saved = test_client.post(
        "/api/workbench/settings/codex",
        json={"path": str(real)},
        headers=AUTH_HEADERS,
    )
    assert saved.status_code == 200
    assert saved.json()["codex_cli_path"] == str(real)
    cleared = test_client.delete("/api/workbench/settings/codex", headers=AUTH_HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["codex_cli_path"] is None


def test_baidu_settings_store_and_clear(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        routes_workbench,
        "save_secret",
        lambda service, account, secret: stored.setdefault("value", secret),
    )
    monkeypatch.setattr(
        routes_workbench.environment_module,
        "read_secret",
        lambda *_args: stored.get("value", ""),
    )
    saved = test_client.post(
        "/api/workbench/settings/baidu",
        json={"api_key": "baidu-key-1234"},
        headers=AUTH_HEADERS,
    )
    assert saved.status_code == 200
    assert saved.json()["baidu_key_masked"] == "bai****1234"
    cleared = test_client.delete("/api/workbench/settings/baidu", headers=AUTH_HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["baidu_key_masked"] is None
```

说明：若文件内 helper 名称与上述不同，按文件现状调整（不得新增重复 helper）。

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py -q -k "environment_reports or codex_settings_rejects or baidu_settings_store"`

预期：FAIL（404 / AttributeError）

- [x] **步骤 3：新增响应模型**

在 `src/parsing_core/serving/models/api.py` 的 `BaiduSettingsRequest` 类之后追加：

```python
class EnvironmentStatusItem(BaseModel):
    state: str
    detail_code: str | None = None


class CodexStatusItem(EnvironmentStatusItem):
    path: str | None = None
    source: str | None = None


class DeepSeekStatusItem(EnvironmentStatusItem):
    last_test_ok: bool | None = None


class BaiduStatusItem(EnvironmentStatusItem):
    masked: str | None = None


class DataDirStatus(BaseModel):
    path: str
    writable: bool


class EnvironmentReport(BaseModel):
    app_version: str
    data_dir: DataDirStatus
    deepseek: DeepSeekStatusItem
    codex: CodexStatusItem
    baidu: BaiduStatusItem
    vision: EnvironmentStatusItem
```

- [x] **步骤 4：新增路由**

在 `src/parsing_core/serving/api/routes_workbench.py` 顶部 import 区补充：

```python
from parsing_core import __version__
from parsing_core.workbench import environment as environment_module
from parsing_core.workbench.environment import (
    BAIDU_KEYCHAIN_ACCOUNT,
    BAIDU_KEYCHAIN_SERVICE,
    build_environment_report,
    resolve_baidu_api_key,
)
from parsing_core.workbench.settings import SettingsError, update_settings_fields
```

在 `test_deepseek_settings` 路由之后追加：

```python
def _settings_response(settings: WorkbenchSettings) -> WorkbenchSettingsResponse:
    return WorkbenchSettingsResponse(
        deepseek_model=settings.deepseek_model,
        deepseek_key_masked=_read_masked_deepseek_key(),
        codex_cli_path=settings.codex_cli_path,
        baidu_key_masked=environment_module.masked_baidu_key(),
    )


def _vision_available() -> bool:
    try:
        _find_vision_helper()
    except Exception:
        return False
    return True


@router.get("/environment", response_model=EnvironmentReport)
async def get_environment(sch: SchedulerDep) -> EnvironmentReport:
    def collect() -> EnvironmentReport:
        settings = load_settings(_settings_root(sch))
        return EnvironmentReport.model_validate(
            build_environment_report(
                settings=settings,
                app_version=__version__,
                data_dir=Path(_settings_root(sch).base_dir),
                vision_available=_vision_available(),
            )
        )

    return await run_in_threadpool(collect)


@router.post("/settings/codex", response_model=WorkbenchSettingsResponse)
async def save_codex_settings(
    req: CodexSettingsRequest, sch: SchedulerDep
) -> WorkbenchSettingsResponse:
    def save() -> WorkbenchSettingsResponse:
        try:
            resolve_codex_path(req.path)
        except CodexCliError as exc:
            raise HTTPException(
                422,
                {"code": "codex_invalid", "params": {"reason": "codex_layout_unsupported"}},
            ) from exc
        try:
            settings = update_settings_fields(_settings_root(sch), codex_cli_path=req.path)
        except SettingsError as exc:
            raise HTTPException(
                422, {"code": "settings_invalid", "params": {"reason": "codex_cli_path"}}
            ) from exc
        return _settings_response(settings)

    return await run_in_threadpool(save)


@router.delete("/settings/codex", response_model=WorkbenchSettingsResponse)
async def clear_codex_settings(sch: SchedulerDep) -> WorkbenchSettingsResponse:
    def clear() -> WorkbenchSettingsResponse:
        settings = update_settings_fields(_settings_root(sch), codex_cli_path=None)
        return _settings_response(settings)

    return await run_in_threadpool(clear)


@router.post("/settings/baidu", response_model=WorkbenchSettingsResponse)
async def save_baidu_settings(
    req: BaiduSettingsRequest, sch: SchedulerDep
) -> WorkbenchSettingsResponse:
    api_key = (req.api_key or "").strip()
    if not api_key:
        raise HTTPException(422, {"code": "settings_invalid", "params": {"reason": "api_key"}})

    def save() -> WorkbenchSettingsResponse:
        try:
            save_secret(BAIDU_KEYCHAIN_SERVICE, BAIDU_KEYCHAIN_ACCOUNT, api_key)
        except KeychainError as exc:
            raise HTTPException(500, {"code": "storage", "params": {}}) from exc
        return _settings_response(load_settings(_settings_root(sch)))

    return await run_in_threadpool(save)


@router.delete("/settings/baidu", response_model=WorkbenchSettingsResponse)
async def clear_baidu_settings(sch: SchedulerDep) -> WorkbenchSettingsResponse:
    def clear() -> WorkbenchSettingsResponse:
        delete_secret(BAIDU_KEYCHAIN_SERVICE, BAIDU_KEYCHAIN_ACCOUNT)
        return _settings_response(load_settings(_settings_root(sch)))

    return await run_in_threadpool(clear)
```

注意：
- 需要 `from parsing_core.workbench.keychain import delete_secret`（若未导入）。
- 三个既有 settings 响应（GET deepseek、POST deepseek）改用 `_settings_response(settings)`，删除重复构造。
- `_read_masked_baidu_key()` 旧函数删除，统一用 `environment_module.masked_baidu_key()`。
- `EnvironmentReport`、`CodexSettingsRequest`、`BaiduSettingsRequest` 需要加入现有 models import 列表。

- [x] **步骤 5：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py -q -k "environment_reports or codex_settings_rejects or baidu_settings_store"`
预期：PASS

再跑全文件：`.venv/bin/python -m pytest tests/test_workbench/test_api.py -q`
预期：全部 PASS（若既有 settings 测试断言旧响应形状，同步更新为四字段）

- [x] **步骤 6：Commit**

```bash
git add src/parsing_core/serving/models/api.py src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_api.py
git commit -m "feat(api): expose environment report and codex/baidu settings"
```

---

## 任务 3：运行时接线（Codex 路径与百度 Key 解析）

**文件：**
- 修改：`src/parsing_core/serving/api/routes_workbench.py`（`_ocr_workflow` 与调用点）
- 修改：`src/parsing_core/serving/api/routes_topics.py`
- 修改：`src/parsing_core/workbench/ocr/workflow.py`（错误码透传）
- 测试：`tests/test_workbench/test_api.py`

说明：本任务只把解析来源改为"设置 + Keychain"；**百度缺失仍阻断**（`baidu_key_missing`），页级隔离在 Plan 2 落地后移除该阻断。

- [x] **步骤 1：编写失败的测试**

在 `tests/test_workbench/test_api.py` 末尾追加：

```python
def test_ocr_uses_configured_codex_path_from_settings(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    root = course_root(tmp_path)
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    _course, source = _registered_pdf_source(test_client, root, pdf)
    real = tmp_path / "codex"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    monkeypatch.setattr(
        routes_workbench,
        "resolve_codex_path",
        lambda path=None: str(path or "codex"),
    )
    saved = test_client.post(
        "/api/workbench/settings/codex",
        json={"path": str(real)},
        headers=AUTH_HEADERS,
    )
    assert saved.status_code == 200
    observed: list[str | None] = []
    monkeypatch.setattr(
        routes_workbench,
        "resolve_codex_path",
        lambda path=None: observed.append(path) or str(real),
    )
    test_client.post(
        f"/api/workbench/sources/{source['id']}/ocr",
        json={},
        headers=AUTH_HEADERS,
    )
    assert observed == [str(real)]


def test_ocr_without_provider_reports_structured_error(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    root = course_root(tmp_path)
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    _course, source = _registered_pdf_source(test_client, root, pdf)
    monkeypatch.setattr(routes_workbench.environment_module, "read_secret", lambda *_args: "")
    monkeypatch.delenv("PDF2MD_BAIDU_API_KEY", raising=False)
    response = test_client.post(
        f"/api/workbench/sources/{source['id']}/ocr",
        json={},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["error"] == "baidu_key_missing"
```

说明：OCR 的 provider 失败经由工作线程写入状态并在 `status` 载荷的 `error` 字段返回（HTTP 200），不是 HTTP 错误。

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py -q -k "configured_codex_path or structured_error"`
预期：FAIL（Codex 未读取设置；百度缺失时 error 为 `ocr_provider_unavailable`）

- [x] **步骤 3：修改 factory、调用点与错误码透传**

`routes_workbench.py`：

1. `_ocr_workflow` 签名改为 `def _ocr_workflow(source: Source, course: Course, settings: WorkbenchSettings) -> OcrWorkflow:`。
2. factory 内：

```python
                helper = _find_vision_helper()
                codex_path = resolve_codex_path(settings.codex_cli_path)
                baidu_key = resolve_baidu_api_key()
                if not baidu_key:
                    raise WorkflowBlockedError("baidu_key_missing")
```

并把 `baidu = BaiduOcrClient(api_key=baidu_key)` 保持为必填（Plan 2 再改为可选）。
3. `except` 分支改为按类型给稳定错误码（并在模块顶部或 factory 内导入 `WorkflowBlockedError`，替换掉原来在 except 内才 import 的写法）：

```python
            except WorkflowBlockedError:
                raise
            except Exception as exc:
                if cancel_signal.is_set():
                    raise RuntimeError("OCR cancelled") from None
                from parsing_core.workbench.ocr.workflow import WorkflowBlockedError

                if isinstance(exc, CodexCliError):
                    raise WorkflowBlockedError("codex_unavailable") from exc
                raise WorkflowBlockedError("ocr_provider_unavailable") from exc
```

4. 6 处调用点先加载设置再传入（逐个替换）：

```python
        settings = load_settings(_settings_root(sch))
        workflow = _ocr_workflow(source, course, settings)
```

`routes_topics.py` 的 `_hybrid_executor`：

```python
    try:
        settings = load_settings(_settings_root(sch))
        codex_path = resolve_codex_path(settings.codex_cli_path)
    except CodexCliError as exc:
        raise api_error(400, "codex_unavailable") from exc
```

并在 `routes_topics.py` 顶部按同包内导入 `from parsing_core.serving.api.routes_workbench import _settings_root`（如遇循环导入则改为函数内导入）。

`workflow.py` 约 6228 行的 provider 错误码映射改为保留稳定码：

```python
def _provider_error_code(exc: BaseException) -> str:
    if isinstance(exc, WorkflowBlockedError):
        message = str(exc)
        if re.fullmatch(r"[a-z][a-z0-9_]+", message):
            return message
        return "ocr_provider_unavailable"
    return "ocr_workflow_failed"
```

（以文件中现有函数名/位置为准；只改映射，不动其他逻辑。）

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py tests/test_workbench/test_ocr_workflow.py -q | tail -3`
预期：PASS。既有断言 `ocr_provider_unavailable` 的用例（如 `test_ocr_workflow.py` 中合成 WorkflowBlockedError 的用例）需要按新映射更新：仅当消息不是稳定码时才回落该值。

- [x] **步骤 5：Commit**

```bash
git add src/parsing_core/serving/api/routes_workbench.py src/parsing_core/serving/api/routes_topics.py src/parsing_core/workbench/ocr/workflow.py tests/test_workbench/test_api.py
git commit -m "feat(api): wire codex path and keychain baidu key into runners"
```

---

## 任务 4：前端类型与 API

**文件：**
- 修改：`parsing-core-app/src/api/workbenchTypes.ts`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 测试：`parsing-core-app/src/api/workbench.test.ts`

- [x] **步骤 1：编写失败的测试**

在 `parsing-core-app/src/api/workbench.test.ts` 追加（沿用文件内 mock `apiFetch` 的既有方式）：

```typescript
it("fetchEnvironment returns structured dependency states", async () => {
  apiFetchMock.mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      app_version: "0.1.4",
      data_dir: { path: "/tmp/data", writable: true },
      deepseek: { state: "ready", last_test_ok: null, detail_code: null },
      codex: { state: "missing", path: null, source: null, detail_code: "codex_not_found" },
      baidu: { state: "optional", masked: null, detail_code: null },
      vision: { state: "ready", detail_code: null },
    }),
  });
  const report = await fetchEnvironment();
  expect(report.codex.detail_code).toBe("codex_not_found");
});

it("saveCodexPath posts the selected path", async () => {
  apiFetchMock.mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      deepseek_model: "deepseek-v4-pro",
      deepseek_key_masked: null,
      codex_cli_path: "/opt/homebrew/bin/codex",
      baidu_key_masked: null,
    }),
  });
  const settings = await saveCodexPath("/opt/homebrew/bin/codex");
  expect(settings.codex_cli_path).toBe("/opt/homebrew/bin/codex");
});
```

若文件没有 `apiFetchMock`，按文件现有 mock 变量名替换。

- [x] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npx vitest run src/api/workbench.test.ts`
预期：FAIL（`fetchEnvironment is not defined`）

- [x] **步骤 3：类型与实现**

`workbenchTypes.ts` 修改 `WorkbenchSettings`：

```typescript
export interface WorkbenchSettings {
  deepseek_model: string;
  deepseek_key_masked: string | null;
  codex_cli_path: string | null;
  baidu_key_masked: string | null;
}

export interface EnvironmentStatusItem {
  state: "ready" | "missing" | "invalid" | "optional";
  detail_code: string | null;
}

export interface CodexStatusItem extends EnvironmentStatusItem {
  path: string | null;
  source: "settings" | "environment" | "detected" | null;
}

export interface EnvironmentReport {
  app_version: string;
  data_dir: { path: string; writable: boolean };
  deepseek: EnvironmentStatusItem & { last_test_ok: boolean | null };
  codex: CodexStatusItem;
  baidu: EnvironmentStatusItem & { masked: string | null };
  vision: EnvironmentStatusItem;
}
```

`workbench.ts` 追加：

```typescript
export async function fetchEnvironment(): Promise<EnvironmentReport> {
  return request<EnvironmentReport>("/api/workbench/environment");
}

export async function saveCodexPath(path: string): Promise<WorkbenchSettings> {
  return post<WorkbenchSettings>("/api/workbench/settings/codex", { path });
}

export async function clearCodexPath(): Promise<WorkbenchSettings> {
  return request<WorkbenchSettings>("/api/workbench/settings/codex", { method: "DELETE" });
}

export async function saveBaiduKey(apiKey: string): Promise<WorkbenchSettings> {
  return post<WorkbenchSettings>("/api/workbench/settings/baidu", { api_key: apiKey });
}

export async function clearBaiduKey(): Promise<WorkbenchSettings> {
  return request<WorkbenchSettings>("/api/workbench/settings/baidu", { method: "DELETE" });
}
```

注意：`request` 为非导出函数，新 API 必须写在 `workbench.ts` 内；`WorkbenchSettings`、`EnvironmentReport` 加入类型 import。

- [x] **步骤 4：运行测试验证通过**

运行：`cd parsing-core-app && npx vitest run src/api/workbench.test.ts`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add parsing-core-app/src/api/workbenchTypes.ts parsing-core-app/src/api/workbench.ts parsing-core-app/src/api/workbench.test.ts
git commit -m "feat(web): add environment and provider settings api"
```

---

## 任务 5：后端结构化错误码

**文件：**
- 创建：`src/parsing_core/serving/api/errors.py`
- 修改：`src/parsing_core/serving/api/routes_workbench.py`、`routes_topics.py`
- 测试：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写失败的测试**

追加：

```python
def test_missing_deepseek_key_returns_actionable_code(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    root = course_root(tmp_path)
    _course, _source, chapter = confirmed_chapter(test_client, root)
    monkeypatch.setattr(routes_workbench, "read_secret", lambda *_args: "")
    response = test_client.post(
        f"/api/workbench/chapters/{chapter['id']}/run-hybrid",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "deepseek_key_missing"


def test_missing_codex_maps_to_stable_error_code(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    root = course_root(tmp_path)
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    _course, source = _registered_pdf_source(test_client, root, pdf)

    def missing(_path=None):
        raise CodexCliError("codex cli not found")

    monkeypatch.setattr(routes_workbench, "resolve_codex_path", missing)
    response = test_client.post(
        f"/api/workbench/sources/{source['id']}/ocr",
        json={},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["error"] == "codex_unavailable"
```

说明：`confirmed_chapter`、`_registered_pdf_source` 若文件名不同，使用文件内既有等价 helper；DeepSeek 走 HTTP detail，OCR 走状态载荷 `error`。

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py -q -k "actionable_code or stable_error_code"`
预期：FAIL（DeepSeek detail 为字符串；Codex 未映射为稳定码）

- [ ] **步骤 3：实现错误 helper 与替换 raise 点**

创建 `src/parsing_core/serving/api/errors.py`：

```python
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def api_error(status_code: int, code: str, **params: Any) -> HTTPException:
    return HTTPException(status_code, {"code": code, "params": params})
```

替换（`routes_workbench.py`）：
- `_read_configured_deepseek_key` 两处 `HTTPException(400, "deepseek api key not configured")` → `api_error(400, "deepseek_key_missing")`
- 若 `run-hybrid` 中还有字符串形态的 DeepSeek 缺失分支，一并改为 `api_error(400, "deepseek_key_missing")`
- OCR 的 provider 稳定码在任务 3 已实现（`codex_unavailable` / `baidu_key_missing` / `ocr_provider_unavailable` 回落），本任务只验证

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py tests/test_workbench/test_topic_api.py -q | tail -3`
预期：PASS。若有旧断言匹配字符串 detail，同步更新为结构化断言。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/serving/api/errors.py src/parsing_core/serving/api/routes_workbench.py src/parsing_core/serving/api/routes_topics.py tests/test_workbench/test_api.py
git commit -m "feat(api): return structured actionable error codes"
```

---

## 任务 6：前端错误映射层

**文件：**
- 创建：`parsing-core-app/src/api/errorMessages.ts`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 测试：`parsing-core-app/src/api/workbench.test.ts`

- [ ] **步骤 1：编写失败的测试**

追加：

```typescript
it("parses structured detail into a coded SafeApiError", async () => {
  apiFetchMock.mockResolvedValueOnce({
    ok: false,
    status: 400,
    json: async () => ({ detail: { code: "deepseek_key_missing", params: {} } }),
  });
  await expect(fetchSettings()).rejects.toMatchObject({
    code: "deepseek_key_missing",
    status: 400,
  });
});

it("maps coded errors to an action", () => {
  const info = apiErrorInfo(new SafeApiError("invalid_request", "codex_unavailable"));
  expect(info?.action).toBe("pick_codex");
  expect(info?.title).toContain("Codex");
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npx vitest run src/api/workbench.test.ts`
预期：FAIL

- [ ] **步骤 3：扩展 SafeApiError 与解析**

`workbench.ts`：

```typescript
export class SafeApiError extends Error {
  constructor(
    readonly category: SafeApiErrorCategory,
    readonly code?: string,
    readonly params?: Record<string, unknown>,
    readonly status?: number,
  ) {
    super(SAFE_ERROR_MESSAGES[category]);
    this.name = "SafeApiError";
  }
}
```

`request` 的 `!res.ok` 分支改为：

```typescript
      if (!res.ok) {
        const categories: Record<number, SafeApiErrorCategory> = {
          400: "invalid_request",
          404: "not_found",
          409: "conflict",
          422: "invalid_format",
          502: "model_unavailable",
          507: "storage",
        };
        let code: string | undefined;
        let params: Record<string, unknown> | undefined;
        try {
          const body = (await res.json()) as { detail?: unknown };
          const detail = body?.detail;
          if (typeof detail === "string") {
            code = /^[a-z][a-z0-9_]{2,64}$/.test(detail) ? detail : undefined;
          } else if (detail && typeof detail === "object" && "code" in detail) {
            const structured = detail as { code?: unknown; params?: unknown };
            if (typeof structured.code === "string") code = structured.code;
            if (structured.params && typeof structured.params === "object") {
              params = structured.params as Record<string, unknown>;
            }
          }
        } catch {
          code = undefined;
        }
        throw new SafeApiError(
          statusCategories[res.status] ?? categories[res.status] ?? "service_unavailable",
          code,
          params,
          res.status,
        );
      }
```

创建 `parsing-core-app/src/api/errorMessages.ts`：

```typescript
import { SafeApiError } from "./workbench";

export type ApiErrorAction =
  | "open_deepseek_settings"
  | "pick_codex"
  | "open_baidu_settings"
  | "retry"
  | "open_logs"
  | "none";

interface ApiErrorInfo {
  title: string;
  description: string;
  action: ApiErrorAction;
}

const ERROR_MESSAGES: Record<string, ApiErrorInfo> = {
  deepseek_key_missing: {
    title: "DeepSeek API Key 未配置",
    description: "请在精读设置中保存 DeepSeek API Key 后重试。",
    action: "open_deepseek_settings",
  },
  codex_unavailable: {
    title: "Codex CLI 不可用",
    description: "请在精读设置或环境自检中选择可执行的 Codex CLI 路径。",
    action: "pick_codex",
  },
  codex_invalid: {
    title: "Codex CLI 路径无效",
    description: "所选文件不是受支持的 Codex CLI，请选择官方 npm 安装的可执行文件。",
    action: "pick_codex",
  },
  baidu_key_missing: {
    title: "百度 OCR Key 未配置",
    description: "请在精读设置中保存百度 OCR Key 后继续复核。",
    action: "open_baidu_settings",
  },
  ocr_review_pending: {
    title: "存在待复核页面",
    description: "配置百度 OCR Key 后可继续复核隔离页面。",
    action: "open_baidu_settings",
  },
  ocr_review_not_ready: {
    title: "暂时无法继续复核",
    description: "请先配置百度 OCR Key 并确认任务存在待复核页面。",
    action: "open_baidu_settings",
  },
};

export function apiErrorInfo(error: unknown): ApiErrorInfo | null {
  if (!(error instanceof SafeApiError) || !error.code) return null;
  return ERROR_MESSAGES[error.code] ?? null;
}

export function ocrErrorInfo(code: string | null | undefined): ApiErrorInfo | null {
  if (!code) return null;
  return ERROR_MESSAGES[code] ?? null;
}
```

- [ ] **步骤 4：接入 OCR 面板**

修改 `parsing-core-app/src/components/workbench/OcrWorkflowPanel.tsx`：
- import `{ ocrErrorInfo }` 自 `"../../api/errorMessages"`，import `useNavigate`。
- 将 `status.error` 展示块（约 127-131 行）替换为：

```tsx
      {status?.error && (
        <div role="alert" className="mt-3 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-xs text-red-700">
          <p className="font-medium">{ocrErrorInfo(status.error)?.title ?? status.error}</p>
          {ocrErrorInfo(status.error) && (
            <p className="mt-1">{ocrErrorInfo(status.error)?.description}</p>
          )}
          {ocrErrorInfo(status.error)?.action !== undefined &&
            ocrErrorInfo(status.error)?.action !== "none" && (
              <button
                type="button"
                className="mt-2 underline"
                onClick={() => navigate("/workbench/settings")}
              >
                去精读设置
              </button>
            )}
        </div>
      )}
```

- 在组件内声明 `const navigate = useNavigate();`。

- [ ] **步骤 5：运行测试验证通过**

运行：`cd parsing-core-app && npx vitest run src/api/workbench.test.ts src/api/client.test.ts src/components/workbench/ChapterWorkbench.test.tsx`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add parsing-core-app/src/api/errorMessages.ts parsing-core-app/src/api/workbench.ts parsing-core-app/src/api/workbench.test.ts parsing-core-app/src/components/workbench/OcrWorkflowPanel.tsx
git commit -m "feat(web): map structured api errors to actionable messages"
```

---

## 任务 7：设置页 Codex 与百度配置

**文件：**
- 修改：`parsing-core-app/src/components/workbench/Settings.tsx`
- 创建：`parsing-core-app/src/components/workbench/Settings.test.tsx`

- [ ] **步骤 1：编写失败的测试**

创建 `parsing-core-app/src/components/workbench/Settings.test.tsx`：

```typescript
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import Settings from "./Settings";

const fetchSettings = vi.fn().mockResolvedValue({
  deepseek_model: "deepseek-v4-pro",
  deepseek_key_masked: "sk-****1234",
  codex_cli_path: null,
  baidu_key_masked: null,
});
const fetchEnvironment = vi.fn().mockResolvedValue({
  app_version: "0.1.4",
  data_dir: { path: "/tmp", writable: true },
  deepseek: { state: "ready", last_test_ok: null, detail_code: null },
  codex: { state: "missing", path: null, source: null, detail_code: "codex_not_found" },
  baidu: { state: "optional", masked: null, detail_code: null },
  vision: { state: "ready", detail_code: null },
});
const saveCodexPath = vi.fn().mockResolvedValue(undefined);
const saveBaiduKey = vi.fn().mockResolvedValue(undefined);

vi.mock("../../api/workbench", () => ({
  fetchSettings: () => fetchSettings(),
  fetchEnvironment: () => fetchEnvironment(),
  saveCodexPath: (...args: unknown[]) => saveCodexPath(...args),
  saveBaiduKey: (...args: unknown[]) => saveBaiduKey(...args),
  clearCodexPath: vi.fn(),
  clearBaiduKey: vi.fn(),
  saveDeepSeekSettings: vi.fn(),
  testDeepSeekSettings: vi.fn(),
}));

it("shows codex and baidu configuration sections", async () => {
  render(<Settings />);
  expect(await screen.findByText("Codex CLI")).toBeInTheDocument();
  expect(screen.getByText("百度 OCR（可选）")).toBeInTheDocument();
});

it("saves the baidu key", async () => {
  render(<Settings />);
  const input = await screen.findByLabelText("百度 OCR Key");
  await userEvent.type(input, "baidu-key-1234");
  await userEvent.click(screen.getByRole("button", { name: "保存百度 Key" }));
  expect(saveBaiduKey).toHaveBeenCalledWith("baidu-key-1234");
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npx vitest run src/components/workbench/Settings.test.tsx`
预期：FAIL（找不到 "Codex CLI"）

- [ ] **步骤 3：实现设置区块**

在 `Settings.tsx` 中：
- 加载时并行调用 `fetchSettings()` 与 `fetchEnvironment()`；新增 state：`codexPath`、`baiduKey`、`environment`。
- DeepSeek 区块之下新增两个卡片：

```tsx
      <section aria-label="Codex CLI 配置" className="...">
        <h2 className="...">Codex CLI</h2>
        <p className="...">
          用于教材页面的视觉复核。当前状态：
          {environment?.codex.state === "ready" ? "已就绪" : "未配置"}
        </p>
        <input
          aria-label="Codex CLI 路径"
          value={codexPath ?? environment?.codex.path ?? ""}
          onChange={(event) => setCodexPath(event.target.value)}
          className="..."
        />
        <button type="button" onClick={handleSaveCodex} className="...">
          保存 Codex 路径
        </button>
        <button type="button" onClick={handlePickCodex} className="...">
          选择文件
        </button>
      </section>

      <section aria-label="百度 OCR 配置" className="...">
        <h2 className="...">百度 OCR（可选）</h2>
        <p className="...">
          {environment?.baidu.state === "ready"
            ? `已配置：${environment.baidu.masked}`
            : "未配置时，冲突页会隔离为待复核，不影响其余页面产出。"}
        </p>
        <input
          aria-label="百度 OCR Key"
          value={baiduKey}
          onChange={(event) => setBaiduKey(event.target.value)}
          className="..."
        />
        <button type="button" onClick={handleSaveBaidu} className="...">
          保存百度 Key
        </button>
      </section>
```

- 保存处理函数调用 `saveCodexPath` / `saveBaiduKey`，成功后 `await refresh()` 重新拉取 settings 与 environment，失败时用 `apiErrorInfo` 展示文案。
- `handlePickCodex` 使用 `@tauri-apps/plugin-dialog` 的 `open({ multiple: false })`；无 `__TAURI_INTERNALS__` 时提示"仅桌面应用可浏览文件，可手动输入路径"。
- 保存按钮在输入为空时禁用。

样式类沿用文件内既有 `className` 模式，不引入新样式系统。

- [ ] **步骤 4：运行测试与既有设置测试**

运行：`cd parsing-core-app && npx vitest run src/components/workbench/Settings.test.tsx src/components/workbench/ChapterWorkbench.test.tsx`
预期：PASS

- [ ] **步骤 5：Tauri 能力检查**

确认 `parsing-core-app/src-tauri/capabilities/main.json` 的 `permissions` 包含 `dialog:allow-open`；若缺失则加入（保持 JSON 排序）：

```json
  "permissions": [
    "core:event:allow-listen",
    "core:event:allow-unlisten",
    "dialog:allow-open"
  ]
```

若测试或构建提示 dialog 权限未注册，运行 `cd parsing-core-app/src-tauri && cargo test --locked` 复核。

- [ ] **步骤 6：Commit**

```bash
git add parsing-core-app/src/components/workbench/Settings.tsx parsing-core-app/src/components/workbench/Settings.test.tsx parsing-core-app/src-tauri/capabilities/main.json
git commit -m "feat(web): configure codex path and baidu key in settings"
```

---

## 任务 8：环境自检卡与挂载

**文件：**
- 创建：`parsing-core-app/src/components/workbench/EnvironmentCard.tsx`
- 创建：`parsing-core-app/src/components/workbench/EnvironmentCard.test.tsx`
- 修改：`parsing-core-app/src/components/workbench/CourseList.tsx`

- [ ] **步骤 1：编写失败的测试**

创建 `EnvironmentCard.test.tsx`：

```typescript
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import EnvironmentCard from "./EnvironmentCard";

const fetchEnvironment = vi.fn();

vi.mock("../../api/workbench", () => ({
  fetchEnvironment: () => fetchEnvironment(),
}));

it("lists each dependency with its state", async () => {
  fetchEnvironment.mockResolvedValueOnce({
    app_version: "0.1.4",
    data_dir: { path: "/tmp/data", writable: true },
    deepseek: { state: "ready", last_test_ok: true, detail_code: null },
    codex: { state: "missing", path: null, source: null, detail_code: "codex_not_found" },
    baidu: { state: "optional", masked: null, detail_code: null },
    vision: { state: "ready", detail_code: null },
  });
  render(
    <MemoryRouter>
      <EnvironmentCard />
    </MemoryRouter>,
  );
  expect(await screen.findByText("环境就绪")).toBeInTheDocument();
  expect(screen.getByText("DeepSeek")).toBeInTheDocument();
  expect(screen.getByText("Codex CLI")).toBeInTheDocument();
  expect(screen.getByText("百度 OCR")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "配置 Codex" })).toBeInTheDocument();
});

it("collapses when every dependency is ready or optional", async () => {
  fetchEnvironment.mockResolvedValueOnce({
    app_version: "0.1.4",
    data_dir: { path: "/tmp/data", writable: true },
    deepseek: { state: "ready", last_test_ok: true, detail_code: null },
    codex: { state: "ready", path: "/opt/homebrew/bin/codex", source: "detected", detail_code: null },
    baidu: { state: "optional", masked: null, detail_code: null },
    vision: { state: "ready", detail_code: null },
  });
  render(
    <MemoryRouter>
      <EnvironmentCard />
    </MemoryRouter>,
  );
  expect(await screen.findByText("环境就绪 · 3/3")).toBeInTheDocument();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npx vitest run src/components/workbench/EnvironmentCard.test.tsx`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现自检卡**

创建 `EnvironmentCard.tsx`：

```tsx
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchEnvironment } from "../../api/workbench";
import type { EnvironmentReport } from "../../api/workbenchTypes";

const DETAIL_LABELS: Record<string, string> = {
  deepseek_key_missing: "未配置 API Key",
  codex_not_found: "未找到可执行文件",
  codex_is_symlink: "符号链接不受支持，请选择真实文件",
  codex_not_regular_file: "不是普通文件",
  codex_not_executable: "没有执行权限",
  codex_layout_unsupported: "不是受支持的官方 Codex CLI",
};

type ItemState = "ready" | "missing" | "invalid" | "optional";

function StateBadge({ state }: { state: ItemState }) {
  const label = state === "ready" ? "已就绪" : state === "optional" ? "可选" : "待配置";
  const tone =
    state === "ready" ? "text-emerald-600" : state === "optional" ? "text-zinc-400" : "text-amber-600";
  return <span className={`text-xs ${tone}`}>{label}</span>;
}

export default function EnvironmentCard() {
  const navigate = useNavigate();
  const [report, setReport] = useState<EnvironmentReport | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [error, setError] = useState(false);

  const refresh = () => {
    fetchEnvironment()
      .then((value) => {
        setReport(value);
        setError(false);
      })
      .catch(() => setError(true));
  };

  useEffect(refresh, []);

  if (error) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        环境检测失败
        <button type="button" className="ml-2 underline" onClick={refresh}>
          重新检测
        </button>
      </div>
    );
  }
  if (report === null) {
    return <div className="rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-400">正在检测环境…</div>;
  }

  const states = [report.deepseek.state, report.codex.state, report.baidu.state, report.vision.state];
  const readyCount = states.filter((state) => state === "ready" || state === "optional").length;
  const allReady = readyCount === states.length;

  return (
    <section aria-label="环境自检" className="rounded-lg border border-zinc-200 bg-white">
      <button
        type="button"
        className="flex w-full items-center justify-between px-3 py-2 text-left"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className="text-sm font-medium">{allReady ? `环境就绪 · ${readyCount}/${states.length}` : "环境就绪"}</span>
        <span className="text-xs text-zinc-400">{expanded ? "收起" : "展开"}</span>
      </button>
      {expanded && (
        <ul className="space-y-2 border-t border-zinc-100 px-3 py-2 text-sm">
          <li className="flex items-center justify-between">
            <span>DeepSeek<StateBadge state={report.deepseek.state} /></span>
            <button type="button" className="text-xs text-emerald-700 underline" onClick={() => navigate("/workbench/settings")}>
              去配置
            </button>
          </li>
          <li className="flex items-center justify-between">
            <span>Codex CLI<StateBadge state={report.codex.state} /></span>
            <button type="button" className="text-xs text-emerald-700 underline" onClick={() => navigate("/workbench/settings")}>
              配置 Codex
            </button>
          </li>
          <li className="flex items-center justify-between">
            <span>百度 OCR<StateBadge state={report.baidu.state} /></span>
            <button type="button" className="text-xs text-emerald-700 underline" onClick={() => navigate("/workbench/settings")}>
              去配置
            </button>
          </li>
          <li className="flex items-center justify-between">
            <span>Apple Vision<StateBadge state={report.vision.state} /></span>
          </li>
          <li className="flex items-center justify-between text-xs text-zinc-400">
            <span>{report.data_dir.path}</span>
            <button type="button" className="underline" onClick={refresh}>
              重新检测
            </button>
          </li>
        </ul>
      )}
    </section>
  );
}
```

说明：`detail_code` 的 `DETAIL_LABELS` 映射在列表项中作为辅助说明展示（实现时挂在对应项下，测试只断言核心文案）。

- [ ] **步骤 4：挂载到工作台首页**

在 `CourseList.tsx` 顶部（标题/创建表单上方）渲染 `<EnvironmentCard />`，import 路径 `./EnvironmentCard`。

- [ ] **步骤 5：运行测试验证通过**

运行：`cd parsing-core-app && npx vitest run src/components/workbench/EnvironmentCard.test.tsx src/components/workbench/CourseList.test.tsx`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add parsing-core-app/src/components/workbench/EnvironmentCard.tsx parsing-core-app/src/components/workbench/EnvironmentCard.test.tsx parsing-core-app/src/components/workbench/CourseList.tsx
git commit -m "feat(web): add environment readiness card"
```

---

## 任务 9：全量验证

- [ ] **步骤 1：后端门禁**

```bash
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/mypy src/parsing_core
.venv/bin/python -m pytest tests/ -q --cov=parsing_core --cov-fail-under=85
```

- [ ] **步骤 2：前端与 Rust 门禁**

```bash
cd parsing-core-app && npm run format:check && npm run lint && npm run typecheck && npm test && npm run build
cd src-tauri && cargo fmt --check && cargo clippy --locked --all-targets -- -D warnings && cargo test --locked
```

- [ ] **步骤 3：手工验收**

1. 在未配置 Codex 的环境启动应用：首页自检卡显示 Codex 待配置；进入设置选择文件并保存后状态变为已就绪。
2. 清空百度 Key：自检卡百度显示"可选"；保存任意 Key 后显示掩码。
3. 未配置 DeepSeek 时触发章节精读：错误提示包含"去配置 DeepSeek"按钮。
4. 记录结果；若失败，回到对应任务修复。

- [ ] **步骤 4：Commit（如手工验收有修正）**

```bash
git add -A
git commit -m "test: verify p0-a environment configuration flow"
```

---

## 规格自检

- **覆盖度：** 5.1 → 任务 1/2；5.2 → 任务 2/3；5.3 → 任务 7/8；5.5 → 任务 5/6；5.4 明确拆分为独立计划。
- **占位符：** 无 TODO/待定；所有步骤含实际代码或明确替换指令。
- **类型一致性：** `environment.resolve_baidu_api_key` / `masked_baidu_key` / `codex_state` / `build_environment_report` 在任务 1 定义，任务 2/3 与测试一致；前端 `EnvironmentReport` 字段与后端 `EnvironmentReport` 模型一致；`SafeApiError(code, params, status)` 顺序在任务 6 前后端一致。
- **错误面一致性：** DeepSeek/Codex（HTTP 路径）走 `detail = {code, params}`；OCR provider 走状态载荷 `error = <code>`；前端两处都映射到同一 `ERROR_MESSAGES` 表。任务 3 的 `_provider_error_code` 保留稳定码，任务 5/6 分别覆盖两端。
