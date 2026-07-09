# 单章混合精读执行器实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 新增单章“混合精读”闭环：DeepSeek 生成基础精读轮次，Codex CLI 生成 Mermaid 和最终自检修订，结果写回现有 workbench 数据结构。

**架构：** 复用现有 `IntensiveReadingPipeline` 的 round、run、note block、card 和 Markdown 同步机制，新增 settings/keychain、DeepSeek executor、Codex CLI executor 和一个 hybrid API 入口。前端只新增设置页和单章运行按钮，不做批量队列或执行器管理页面。

**技术栈：** Python stdlib `urllib.request` / `subprocess` / `json`，macOS `security` CLI，FastAPI，pytest，React + TypeScript + Zustand。

---

## 文件结构

### 后端新增

| 文件 | 职责 |
|---|---|
| `src/parsing_core/workbench/settings.py` | 读写 `workbench-settings.json`，保存 DeepSeek model |
| `src/parsing_core/workbench/keychain.py` | macOS Keychain 薄封装：save/get/delete/mask |
| `src/parsing_core/workbench/deepseek.py` | DeepSeek chat/completions 调用 |
| `src/parsing_core/workbench/codex_cli.py` | Codex CLI 安全调用，只读任务包、只写结果文件 |
| `src/parsing_core/workbench/hybrid.py` | 按 round 分发 DeepSeek / Codex 的 executor |

### 后端修改

| 文件 | 修改 |
|---|---|
| `src/parsing_core/serving/models/api.py` | 增加 settings 请求/响应模型 |
| `src/parsing_core/serving/api/routes_workbench.py` | 增加 settings API 和 `run-hybrid` API |

### 后端测试新增/修改

| 文件 | 职责 |
|---|---|
| `tests/test_workbench/test_settings.py` | settings JSON 和 keychain mock 测试 |
| `tests/test_workbench/test_deepseek.py` | DeepSeek HTTP 成功/失败测试 |
| `tests/test_workbench/test_codex_cli.py` | Codex CLI 命令成功/失败测试 |
| `tests/test_workbench/test_hybrid.py` | round 分发和失败状态测试 |
| `tests/test_workbench/test_api.py` | settings 和 run-hybrid API 测试 |

### 前端新增/修改

| 文件 | 修改 |
|---|---|
| `parsing-core-app/src/api/workbenchTypes.ts` | 增加 settings 类型 |
| `parsing-core-app/src/api/workbench.ts` | 增加 settings、test、runHybrid API |
| `parsing-core-app/src/store/useWorkbenchStore.ts` | 增加 runHybridChapter |
| `parsing-core-app/src/components/workbench/Settings.tsx` | 新增 DeepSeek 设置页 |
| `parsing-core-app/src/components/workbench/ChapterConfirm.tsx` | 增加“运行混合精读”按钮 |
| `parsing-core-app/src/components/workbench/ChapterWorkbench.tsx` | 增加“运行混合精读”按钮 |
| `parsing-core-app/src/App.tsx` | 增加 `/workbench/settings` route |
| `parsing-core-app/src/components/Layout.tsx` | 增加设置入口 |

---

## 任务 1：settings 与 Keychain 基础

**文件：**
- 创建：`src/parsing_core/workbench/settings.py`
- 创建：`src/parsing_core/workbench/keychain.py`
- 测试：`tests/test_workbench/test_settings.py`

- [ ] **步骤 1：编写失败的 settings/keychain 测试**

创建 `tests/test_workbench/test_settings.py`：

```python
import subprocess

import pytest

from parsing_core.workbench.keychain import KeychainError, mask_secret, read_secret, save_secret
from parsing_core.workbench.settings import WorkbenchSettings, load_settings, save_settings


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "workbench-settings.json"
    save_settings(path, WorkbenchSettings(deepseek_model="deepseek-chat"))

    assert load_settings(path).deepseek_model == "deepseek-chat"


def test_settings_default_when_missing(tmp_path):
    assert load_settings(tmp_path / "missing.json").deepseek_model == "deepseek-chat"


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
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_settings.py -q
```

预期：FAIL，报错包含 `ModuleNotFoundError` 或无法导入 `parsing_core.workbench.settings`。

- [ ] **步骤 3：实现 settings.py**

创建 `src/parsing_core/workbench/settings.py`：

```python
from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class WorkbenchSettings:
    deepseek_model: str = "deepseek-chat"


def load_settings(path: str | Path) -> WorkbenchSettings:
    settings_path = Path(path)
    if not settings_path.exists():
        return WorkbenchSettings()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return WorkbenchSettings(deepseek_model=data.get("deepseek_model") or "deepseek-chat")


def save_settings(path: str | Path, settings: WorkbenchSettings) -> None:
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **步骤 4：实现 keychain.py**

创建 `src/parsing_core/workbench/keychain.py`：

```python
import platform
import subprocess


class KeychainError(RuntimeError):
    pass


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise KeychainError("macOS Keychain is required")


def save_secret(service: str, account: str, secret: str) -> None:
    _require_macos()
    cmd = [
        "security",
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        account,
        "-w",
        secret,
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise KeychainError(result.stderr.strip() or "failed to save secret")


def read_secret(service: str, account: str) -> str:
    _require_macos()
    cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise KeychainError(result.stderr.strip() or "secret not found")
    return result.stdout.strip()


def delete_secret(service: str, account: str) -> None:
    _require_macos()
    cmd = ["security", "delete-generic-password", "-s", service, "-a", account]
    subprocess.run(cmd, text=True, capture_output=True, check=False)


def mask_secret(secret: str) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "****"
    return f"{secret[:3]}****{secret[-4:]}"
```

- [ ] **步骤 5：运行测试验证通过**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_settings.py -q
```

预期：PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/settings.py src/parsing_core/workbench/keychain.py tests/test_workbench/test_settings.py
git commit -m "feat(workbench): add settings and keychain storage"
```

---

## 任务 2：DeepSeek executor

**文件：**
- 创建：`src/parsing_core/workbench/deepseek.py`
- 测试：`tests/test_workbench/test_deepseek.py`

- [ ] **步骤 1：编写失败的 DeepSeek 测试**

创建 `tests/test_workbench/test_deepseek.py`：

```python
import json
from urllib.error import HTTPError

import pytest

from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekError, DeepSeekExecutor


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_deepseek_client_returns_message(monkeypatch):
    def fake_urlopen(req, timeout):
        assert req.headers["Authorization"] == "Bearer sk-test"
        return FakeResponse({"choices": [{"message": {"content": "精读结果"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model="deepseek-chat")
    assert client.complete("hello") == "精读结果"


def test_deepseek_client_raises_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 401, "bad key", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model="deepseek-chat")
    with pytest.raises(DeepSeekError):
        client.complete("hello")


def test_deepseek_executor_uses_client():
    class Client:
        def complete(self, prompt: str) -> str:
            assert "## 原文" in prompt
            return "结构理解"

    executor = DeepSeekExecutor(Client())

    assert executor.run("structure", "## 原文\n战略") == "结构理解"
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_deepseek.py -q
```

预期：FAIL，报错无法导入 `parsing_core.workbench.deepseek`。

- [ ] **步骤 3：实现 deepseek.py**

创建 `src/parsing_core/workbench/deepseek.py`：

```python
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com/chat/completions"):
        if not api_key:
            raise DeepSeekError("deepseek api key missing")
        if not model:
            raise DeepSeekError("deepseek model missing")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def complete(self, prompt: str, timeout: int = 120) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的 MBA 课程精读助教。"},
                    {"role": "user", "content": prompt},
                ],
            }
        ).encode()
        req = Request(
            self.base_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as res:
                data = json.loads(res.read().decode())
        except (HTTPError, URLError, TimeoutError) as exc:
            raise DeepSeekError(str(exc)) from exc
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content.strip():
            raise DeepSeekError("deepseek returned empty content")
        return content


class DeepSeekExecutor:
    def __init__(self, client: DeepSeekClient):
        self.client = client

    def run(self, round_key: str, task_package: str) -> str:
        return self.client.complete(task_package)
```

- [ ] **步骤 4：运行测试验证通过**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_deepseek.py -q
```

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/deepseek.py tests/test_workbench/test_deepseek.py
git commit -m "feat(workbench): add deepseek executor"
```

---

## 任务 3：Codex CLI executor

**文件：**
- 创建：`src/parsing_core/workbench/codex_cli.py`
- 测试：`tests/test_workbench/test_codex_cli.py`

- [ ] **步骤 1：编写失败的 Codex CLI 测试**

创建 `tests/test_workbench/test_codex_cli.py`：

```python
import subprocess

import pytest

from parsing_core.workbench.codex_cli import CodexCliError, CodexCliExecutor, resolve_codex_path


def test_resolve_codex_path_prefers_env(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_PATH", "/usr/local/bin/codex")

    assert resolve_codex_path() == "/usr/local/bin/codex"


def test_codex_cli_executor_reads_output_file(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert kwargs["input"] == "# task"
        output_arg = cmd[cmd.index("--output-last-message") + 1]
        with open(output_arg, "w", encoding="utf-8") as f:
            f.write("```mermaid\nflowchart TD\n  A-->B\n```\n\n```mermaid\nflowchart LR\n  C-->D\n```")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    executor = CodexCliExecutor(codex_path="/bin/codex", run_dir=tmp_path)

    output = executor.run("mermaid", "# task")

    assert "flowchart TD" in output


def test_codex_cli_executor_raises_when_output_missing(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    executor = CodexCliExecutor(codex_path="/bin/codex", run_dir=tmp_path)

    with pytest.raises(CodexCliError):
        executor.run("review", "# task")
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_codex_cli.py -q
```

预期：FAIL，报错无法导入 `parsing_core.workbench.codex_cli`。

- [ ] **步骤 3：实现 codex_cli.py**

创建 `src/parsing_core/workbench/codex_cli.py`：

```python
import os
import shutil
import subprocess
from pathlib import Path


class CodexCliError(RuntimeError):
    pass


def resolve_codex_path() -> str:
    env_path = os.environ.get("CODEX_CLI_PATH")
    if env_path:
        return env_path
    found = shutil.which("codex")
    if not found:
        raise CodexCliError("codex cli not found")
    return found


class CodexCliExecutor:
    def __init__(self, codex_path: str, run_dir: str | Path, timeout: int = 300):
        self.codex_path = codex_path
        self.run_dir = Path(run_dir)
        self.timeout = timeout

    def run(self, round_key: str, task_package: str) -> str:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        input_path = self.run_dir / f"codex-{round_key}-input.md"
        output_path = self.run_dir / f"codex-{round_key}-output.md"
        input_path.write_text(task_package, encoding="utf-8")
        if output_path.exists():
            output_path.unlink()
        cmd = [
            self.codex_path,
            "exec",
            "--sandbox",
            "read-only",
            "--cd",
            str(self.run_dir),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        result = subprocess.run(
            cmd,
            input=input_path.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        if result.returncode != 0:
            raise CodexCliError(result.stderr.strip() or "codex cli failed")
        if not output_path.exists():
            raise CodexCliError("codex output file missing")
        output = output_path.read_text(encoding="utf-8")
        if not output.strip():
            raise CodexCliError("codex output file empty")
        return output
```

- [ ] **步骤 4：运行测试验证通过**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_codex_cli.py -q
```

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/codex_cli.py tests/test_workbench/test_codex_cli.py
git commit -m "feat(workbench): add codex cli executor"
```

---

## 任务 4：Hybrid executor 与 pipeline API

**文件：**
- 创建：`src/parsing_core/workbench/hybrid.py`
- 修改：`src/parsing_core/serving/models/api.py`
- 修改：`src/parsing_core/serving/api/routes_workbench.py`
- 测试：`tests/test_workbench/test_hybrid.py`
- 测试：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写失败的 hybrid round 分发测试**

创建 `tests/test_workbench/test_hybrid.py`：

```python
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor


class Recorder:
    def __init__(self, prefix):
        self.prefix = prefix
        self.rounds = []

    def run(self, round_key: str, task_package: str) -> str:
        self.rounds.append(round_key)
        if round_key == "mermaid":
            return "```mermaid\nflowchart TD\n  A-->B\n```\n\n```mermaid\nflowchart LR\n  C-->D\n```"
        return f"{self.prefix}:{round_key}"


def test_hybrid_executor_routes_rounds():
    deepseek = Recorder("deepseek")
    codex = Recorder("codex")
    executor = HybridIntensiveReadingExecutor(deepseek, codex)

    assert executor.run("structure", "# task") == "deepseek:structure"
    assert "flowchart TD" in executor.run("mermaid", "# task")

    assert deepseek.rounds == ["structure"]
    assert codex.rounds == ["mermaid"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_hybrid.py -q
```

预期：FAIL，无法导入 `parsing_core.workbench.hybrid`。

- [ ] **步骤 3：实现 hybrid.py**

创建 `src/parsing_core/workbench/hybrid.py`：

```python
DEEPSEEK_ROUNDS = {"structure", "concepts", "plain_explain", "application", "cards"}
CODEX_ROUNDS = {"mermaid", "review"}


class HybridIntensiveReadingExecutor:
    def __init__(self, deepseek_executor, codex_executor):
        self.deepseek_executor = deepseek_executor
        self.codex_executor = codex_executor

    def run(self, round_key: str, task_package: str) -> str:
        if round_key in DEEPSEEK_ROUNDS:
            return self.deepseek_executor.run(round_key, task_package)
        if round_key in CODEX_ROUNDS:
            return self.codex_executor.run(round_key, task_package)
        raise ValueError(f"unknown round: {round_key}")
```

- [ ] **步骤 4：运行 hybrid 测试验证通过**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_hybrid.py -q
```

预期：PASS。

- [ ] **步骤 5：给 API 测试增加 run-hybrid 配置缺失场景**

在 `tests/test_workbench/test_api.py` 添加：

```python
def test_run_hybrid_requires_deepseek_settings(tmp_path):
    c = client(tmp_path)
    root = course_root(tmp_path)
    course = c.post(
        "/api/workbench/courses",
        json={"title": "战略管理", "description": "", "root_dir": str(root)},
    ).json()
    source_md = root / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(
        f"/api/workbench/courses/{course['id']}/sources",
        json={"kind": "main", "file_path": str(source_md), "title": "战略教材"},
    ).json()
    chapter = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters").json()[0]
    c.post(f"/api/workbench/chapters/{chapter['id']}/confirm")

    res = c.post(f"/api/workbench/chapters/{chapter['id']}/run-hybrid")

    assert res.status_code == 400
    assert "deepseek" in res.json()["detail"].lower()
```

- [ ] **步骤 6：运行 API 测试验证失败**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_api.py::test_run_hybrid_requires_deepseek_settings -q
```

预期：FAIL，返回 404。

- [ ] **步骤 7：增加 API 模型**

在 `src/parsing_core/serving/models/api.py` 追加：

```python
class WorkbenchSettingsResponse(BaseModel):
    deepseek_model: str
    deepseek_key_masked: str | None = None


class DeepSeekSettingsRequest(BaseModel):
    api_key: str
    model: str = "deepseek-chat"
```

- [ ] **步骤 8：实现最小 run-hybrid API**

在 `src/parsing_core/serving/api/routes_workbench.py`：

1. 增加 imports：

```python
from parsing_core.workbench.codex_cli import CodexCliExecutor, resolve_codex_path
from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekExecutor
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
from parsing_core.workbench.keychain import KeychainError, read_secret
from parsing_core.workbench.settings import load_settings
```

2. 增加 helper：

```python
KEYCHAIN_SERVICE = "pdf2md.deepseek"
KEYCHAIN_ACCOUNT = "api-key"


def _settings_path(sch: SchedulerDep) -> Path:
    return Path(sch._query_orch.fs.base_dir) / "workbench-settings.json"
```

3. 增加 endpoint：

```python
@router.post("/chapters/{chapter_id}/run-hybrid", response_model=ChapterResponse)
async def run_hybrid_chapter(chapter_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    if chapter.status not in {"CONFIRMED", "FAILED"}:
        raise HTTPException(409, "chapter must be CONFIRMED or FAILED before hybrid reading")
    settings = load_settings(_settings_path(sch))
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeychainError as exc:
        raise HTTPException(400, f"deepseek api key missing: {exc}") from exc
    try:
        codex_path = resolve_codex_path()
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    run_dir = Path(sch._query_orch.fs.base_dir) / "workbench-runs"
    executor = HybridIntensiveReadingExecutor(
        DeepSeekExecutor(DeepSeekClient(api_key=api_key, model=settings.deepseek_model)),
        CodexCliExecutor(codex_path=codex_path, run_dir=run_dir),
    )
    try:
        IntensiveReadingPipeline(repo, executor, run_dir).run_all(chapter_id)
    except Exception as exc:
        repo.update_chapter_status(chapter_id, "FAILED")
        raise HTTPException(500, str(exc)) from exc
    repo.update_chapter_status(chapter_id, "COMPLETED")
    return _chapter_response(repo.get_chapter(chapter_id))
```

- [ ] **步骤 9：运行 API 测试验证通过**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_api.py::test_run_hybrid_requires_deepseek_settings -q
```

预期：PASS。

- [ ] **步骤 10：Commit**

```bash
git add src/parsing_core/workbench/hybrid.py src/parsing_core/serving/models/api.py src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_hybrid.py tests/test_workbench/test_api.py
git commit -m "feat(workbench): add hybrid run endpoint"
```

---

## 任务 5：settings API

**文件：**
- 修改：`src/parsing_core/serving/api/routes_workbench.py`
- 测试：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写 settings API 测试**

在 `tests/test_workbench/test_api.py` 添加：

```python
def test_workbench_settings_save_and_get(tmp_path, monkeypatch):
    saved = {}

    def fake_save(service, account, secret):
        saved[(service, account)] = secret

    def fake_read(service, account):
        return saved[(service, account)]

    monkeypatch.setattr("parsing_core.serving.api.routes_workbench.save_secret", fake_save)
    monkeypatch.setattr("parsing_core.serving.api.routes_workbench.read_secret", fake_read)
    c = client(tmp_path)

    res = c.post("/api/workbench/settings/deepseek", json={"api_key": "sk-1234567890", "model": "deepseek-chat"})
    assert res.status_code == 200

    res = c.get("/api/workbench/settings")
    assert res.status_code == 200
    assert res.json()["deepseek_model"] == "deepseek-chat"
    assert res.json()["deepseek_key_masked"] == "sk-****7890"


def test_workbench_settings_test_connection(tmp_path, monkeypatch):
    monkeypatch.setattr("parsing_core.serving.api.routes_workbench.save_secret", lambda service, account, secret: None)
    monkeypatch.setattr("parsing_core.serving.api.routes_workbench.read_secret", lambda service, account: "sk-test")

    class Client:
        def __init__(self, api_key, model):
            assert api_key == "sk-test"
            assert model == "deepseek-chat"

        def complete(self, prompt: str) -> str:
            return "ok"

    monkeypatch.setattr("parsing_core.serving.api.routes_workbench.DeepSeekClient", Client)
    c = client(tmp_path)
    c.post("/api/workbench/settings/deepseek", json={"api_key": "sk-test", "model": "deepseek-chat"})

    res = c.post("/api/workbench/settings/deepseek/test")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_api.py::test_workbench_settings_save_and_get tests/test_workbench/test_api.py::test_workbench_settings_test_connection -q
```

预期：FAIL，返回 404。

- [ ] **步骤 3：实现 settings API**

在 `src/parsing_core/serving/api/routes_workbench.py`：

1. 增加 imports：

```python
from parsing_core.serving.models.api import DeepSeekSettingsRequest, WorkbenchSettingsResponse
from parsing_core.workbench.keychain import mask_secret, save_secret
from parsing_core.workbench.settings import WorkbenchSettings, save_settings
```

2. 增加 endpoint：

```python
@router.get("/settings", response_model=WorkbenchSettingsResponse)
async def get_workbench_settings(sch: SchedulerDep):
    settings = load_settings(_settings_path(sch))
    try:
        masked = mask_secret(read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT))
    except KeychainError:
        masked = None
    return WorkbenchSettingsResponse(deepseek_model=settings.deepseek_model, deepseek_key_masked=masked)


@router.post("/settings/deepseek", response_model=WorkbenchSettingsResponse)
async def save_deepseek_settings(req: DeepSeekSettingsRequest, sch: SchedulerDep):
    save_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, req.api_key)
    settings = WorkbenchSettings(deepseek_model=req.model or "deepseek-chat")
    save_settings(_settings_path(sch), settings)
    return WorkbenchSettingsResponse(deepseek_model=settings.deepseek_model, deepseek_key_masked=mask_secret(req.api_key))


@router.post("/settings/deepseek/test")
async def test_deepseek_settings(sch: SchedulerDep):
    settings = load_settings(_settings_path(sch))
    try:
        api_key = read_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeychainError as exc:
        raise HTTPException(400, f"deepseek api key missing: {exc}") from exc
    DeepSeekClient(api_key=api_key, model=settings.deepseek_model).complete("请只回复 ok")
    return {"status": "ok"}
```

- [ ] **步骤 4：运行测试验证通过**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest tests/test_workbench/test_api.py::test_workbench_settings_save_and_get tests/test_workbench/test_api.py::test_workbench_settings_test_connection -q
```

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_api.py
git commit -m "feat(workbench): add deepseek settings api"
```

---

## 任务 6：前端设置页和混合运行入口

**文件：**
- 修改：`parsing-core-app/src/api/workbenchTypes.ts`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 修改：`parsing-core-app/src/store/useWorkbenchStore.ts`
- 创建：`parsing-core-app/src/components/workbench/Settings.tsx`
- 修改：`parsing-core-app/src/components/workbench/ChapterConfirm.tsx`
- 修改：`parsing-core-app/src/components/workbench/ChapterWorkbench.tsx`
- 修改：`parsing-core-app/src/App.tsx`
- 修改：`parsing-core-app/src/components/Layout.tsx`

- [ ] **步骤 1：增加前端 API 类型**

在 `parsing-core-app/src/api/workbenchTypes.ts` 追加：

```ts
export interface WorkbenchSettings {
  deepseek_model: string;
  deepseek_key_masked: string | null;
}
```

- [ ] **步骤 2：增加前端 API 方法**

在 `parsing-core-app/src/api/workbench.ts`：

1. import 增加 `WorkbenchSettings`。
2. 追加：

```ts
export function getWorkbenchSettings(): Promise<WorkbenchSettings> {
  return request<WorkbenchSettings>("/api/workbench/settings");
}

export function saveDeepSeekSettings(api_key: string, model: string): Promise<WorkbenchSettings> {
  return post<WorkbenchSettings>("/api/workbench/settings/deepseek", { api_key, model });
}

export function testDeepSeekSettings(): Promise<{ status: string }> {
  return post<{ status: string }>("/api/workbench/settings/deepseek/test");
}

export function runHybridChapter(chapterId: string): Promise<Chapter> {
  return post<Chapter>(`/api/workbench/chapters/${chapterId}/run-hybrid`);
}
```

- [ ] **步骤 3：增加 store 方法**

在 `parsing-core-app/src/store/useWorkbenchStore.ts` 的 `WorkbenchState` 增加：

```ts
runHybridChapter: (chapterId: string) => Promise<void>;
```

在 store return object 中增加：

```ts
runHybridChapter: async (chapterId) => {
  let updated: Chapter | null = null;
  try {
    updated = await api.runHybridChapter(chapterId);
  } catch (error) {
    updated = await api.getChapter(chapterId).catch(() => null);
    if (updated) updateChapter(updated);
    throw error;
  }
  updateChapter(updated);
  const blocks = await api.listChapterNoteBlocks(chapterId);
  const chapter = Object.values(get().chapters).flat().find((item) => item.id === chapterId) ?? updated;
  await get().loadCourseCards(chapter.course_id);
  set((state) => ({ noteBlocksByChapter: { ...state.noteBlocksByChapter, [chapterId]: blocks } }));
},
```

- [ ] **步骤 4：创建 Settings.tsx**

创建 `parsing-core-app/src/components/workbench/Settings.tsx`：

```tsx
import { FormEvent, useEffect, useState } from "react";
import { Loader2, Save, Wifi } from "lucide-react";
import { getWorkbenchSettings, saveDeepSeekSettings, testDeepSeekSettings } from "../../api/workbench";

export default function Settings() {
  const [model, setModel] = useState("deepseek-chat");
  const [apiKey, setApiKey] = useState("");
  const [masked, setMasked] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    getWorkbenchSettings()
      .then((settings) => {
        setModel(settings.deepseek_model);
        setMasked(settings.deepseek_key_masked);
      })
      .catch((err: unknown) => setMessage(err instanceof Error ? err.message : "设置加载失败"));
  }, []);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!apiKey.trim() || !model.trim()) return;
    setSaving(true);
    setMessage(null);
    try {
      const settings = await saveDeepSeekSettings(apiKey.trim(), model.trim());
      setMasked(settings.deepseek_key_masked);
      setApiKey("");
      setMessage("已保存");
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    setTesting(true);
    setMessage(null);
    try {
      await testDeepSeekSettings();
      setMessage("连接正常");
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : "连接失败");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="max-w-xl space-y-6 animate-in">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">工作台设置</h1>
        <p className="mt-1 text-sm text-zinc-500">DeepSeek API Key 保存在 macOS Keychain。</p>
      </div>
      <form onSubmit={submit} className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
        <label className="block">
          <span className="text-xs text-zinc-500">DeepSeek API Key</span>
          <input
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={masked ?? "sk-..."}
            className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">Model</span>
          <input
            value={model}
            onChange={(event) => setModel(event.target.value)}
            className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
        </label>
        {masked && <p className="text-xs text-zinc-500">当前 Key：{masked}</p>}
        {message && <p className="text-sm text-zinc-600">{message}</p>}
        <button
          type="submit"
          disabled={saving || !apiKey.trim() || !model.trim()}
          className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
          保存
        </button>
        <button
          type="button"
          onClick={testConnection}
          disabled={testing}
          className="ml-2 inline-flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 disabled:opacity-50"
        >
          {testing ? <Loader2 size={15} className="animate-spin" /> : <Wifi size={15} />}
          测试连接
        </button>
      </form>
    </div>
  );
}
```

- [ ] **步骤 5：接入 route 和导航**

在 `parsing-core-app/src/App.tsx`：

```tsx
import Settings from "./components/workbench/Settings";
```

并新增 route：

```tsx
<Route path="/workbench/settings" element={<Settings />} />
```

在 `parsing-core-app/src/components/Layout.tsx` 增加一个 nav item：

```tsx
{ to: "/workbench/settings", label: "精读设置", icon: SettingsIcon }
```

如果已 import 的 icon 没有 `SettingsIcon`，从 `lucide-react` import `Settings` 并 alias：

```tsx
import { Settings as SettingsIcon } from "lucide-react";
```

- [ ] **步骤 6：在章节页增加混合运行按钮**

在 `ChapterConfirm.tsx` 中从 store 取 `runHybridChapter`，新增 handler：

```tsx
const runHybrid = async (chapterId: string) => {
  setBusyId(chapterId);
  setError(null);
  try {
    await runHybridChapter(chapterId);
  } catch (err: unknown) {
    setError(err instanceof Error ? err.message : "混合精读失败");
  } finally {
    setBusyId(null);
  }
};
```

在操作区增加按钮，启用条件为 `chapter.status === "CONFIRMED" || chapter.status === "FAILED"`：

```tsx
<button
  type="button"
  onClick={() => runHybrid(chapter.id)}
  disabled={busy || !(chapter.status === "CONFIRMED" || chapter.status === "FAILED")}
  className="inline-flex items-center gap-1.5 rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
>
  混合精读
</button>
```

在 `ChapterWorkbench.tsx` 对 active chapter 增加同样按钮，运行后保留当前 `chapterId`。

- [ ] **步骤 7：运行前端构建验证**

运行：

```bash
cd parsing-core-app && npm run build
```

预期：PASS。允许已有 Vite chunk size warning。

- [ ] **步骤 8：Commit**

```bash
git add parsing-core-app/src/api/workbenchTypes.ts parsing-core-app/src/api/workbench.ts parsing-core-app/src/store/useWorkbenchStore.ts parsing-core-app/src/components/workbench/Settings.tsx parsing-core-app/src/components/workbench/ChapterConfirm.tsx parsing-core-app/src/components/workbench/ChapterWorkbench.tsx parsing-core-app/src/App.tsx parsing-core-app/src/components/Layout.tsx
git commit -m "feat(webui): add hybrid reading controls"
```

---

## 任务 7：最终验证与对抗式审查

**文件：**
- 修改：无功能文件，必要时只修复审查发现的问题。

- [ ] **步骤 1：运行后端完整测试**

运行：

```bash
PYTHONPATH=src /Users/laoer/Documents/PDF2MD/.venv/bin/pytest -q
```

预期：PASS。

- [ ] **步骤 2：运行前端构建**

运行：

```bash
cd parsing-core-app && npm run build
```

预期：PASS。允许已有 Vite chunk size warning。

- [ ] **步骤 3：运行 Rust check**

运行：

```bash
cd parsing-core-app/src-tauri && cargo check
```

预期：PASS。允许当前已有 unused/dead_code warning。

- [ ] **步骤 4：对抗式审查清单**

人工或子代理按以下清单审查：

- Keychain 不向前端返回明文 key。
- `workbench-settings.json` 不包含 API Key。
- Codex CLI 只读取 task package，只写 output file。
- `run-hybrid` 不允许 `DRAFT` 或 `COMPLETED` 章节运行。
- DeepSeek 缺 key、Codex 不存在、Codex 输出缺失都能返回可理解错误。
- 失败后章节状态为 `FAILED`，前端能展示错误并刷新状态。
- 成功后章节状态为 `COMPLETED`，note blocks/cards/Markdown 已同步。

- [ ] **步骤 5：Commit 修复或记录无修复**

如果审查发现问题，修复后 commit：

```bash
git add src/parsing_core/workbench src/parsing_core/serving/api/routes_workbench.py src/parsing_core/serving/models/api.py tests/test_workbench parsing-core-app/src
git commit -m "fix(workbench): address hybrid reading review"
```

如果无问题，不需要 commit。
