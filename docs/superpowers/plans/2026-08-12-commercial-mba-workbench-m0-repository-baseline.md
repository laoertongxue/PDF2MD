# M0 仓库与发布基线实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把当前仓库治理为唯一 `main`，固定可重复的工具链，清零现有质量与安全失败，并让正式 Sidecar 基线在干净环境可构建、可测试。

**架构：** 本里程碑不引入新业务能力，只修复阻断商业化的仓库、工具链、状态、安全和发布基线。所有变更用现有 API 兼容测试保护，为 M1 的渐进迁移提供稳定起点。

**技术栈：** Git/GitHub CLI、CPython 3.12、uv、Ruff、mypy、pytest、Node 20、npm audit、ESLint、Prettier、Rust stable、Clippy、GitHub Actions、FastAPI/Tauri。

---

## 已验证基线（2026-08-12）

- Git 为 `master...origin/master [ahead 2]`；产品目标要求迁移为唯一 `main`。
- `.gitignore` 已有一项未提交的 `.codegraph/` 忽略规则；`docs/CODE_SIGNING.md` 是未跟踪用户文件，必须隔离。
- Ruff 当前有 5 个错误：1 个未使用导入、1 个导入排序和 3 个 `E402`。
- 当前 `.venv` 指向已不存在的 Python 3.13 解释器，mypy 无法启动；项目声明仍为 Python 3.11。
- Rust stable 尚未安装 Clippy 组件。
- npm 官方审计当前有 7 个漏洞，其中 5 个 high、2 个 moderate；直接受影响项包括 DOMPurify、Mermaid 和 React Router。
- Release 仅构建 Apple Silicon，使用 Python 3.13、ad-hoc 签名和 Action 标签引用。

每项在对应任务中先复现，再修复；环境缺失与代码失败分别记录，不能用“本机没装工具”掩盖仓库门禁。

## 文件清单

**创建：**

- `.python-version`：固定 Python 3.12 系列。
- `rust-toolchain.toml`：固定 Rust stable，并要求 `rustfmt`、`clippy`。
- `.github/workflows/ci.yml`：PR 与 `main` 的快速质量流水线。
- `scripts/verify-fast.sh`：全仓快速门禁统一入口。
- `scripts/verify-architecture.sh`：架构边界和契约门禁入口。
- `scripts/verify-security.sh`：依赖、Secret、恶意输入和本地 API 安全门禁。
- `tests/test_architecture.py`：Python 依赖方向测试。
- `tests/test_toolchain.py`：Python 与构建版本一致性测试。
- `tests/test_security.py`：恶意输入、路径、Secret 和本地 API 安全测试。
- `parsing-core-app/eslint.config.js`：TypeScript/React 严格 lint。
- `parsing-core-app/.prettierrc.json`：确定性前端格式。
- `parsing-core-app/src-tauri/tests/api_session.rs`：随机令牌和 Sidecar 配置契约测试。

**修改：**

- `.gitignore`：忽略本地 `.codegraph/` 索引。
- `pyproject.toml`、`uv.lock`：Python 3.12、严格静态检查、覆盖率、解析依赖。
- `src/parsing_core/log.py`、`src/parsing_core/orchestrator.py`、`src/parsing_core/serving/scheduler.py`：清零当前 Ruff 错误。
- `src/parsing_core/serving/serve.py`、`src/parsing_core/serving/api/deps.py`、`src/parsing_core/serving/api/routes_ws.py`：全接口会话鉴权和 Origin 校验。
- `src/parsing_core/workbench/ocr/workflow.py`：完成状态恢复只使用一个映射。
- `tests/test_workbench/test_ocr_workflow.py`、`tests/test_serving/test_api_health.py`、`tests/test_serving/test_api_ws.py`：回归测试。
- `parsing-core-app/package.json`、`package-lock.json`、`tsconfig.json`：漏洞修复和严格门禁。
- `parsing-core-app/src/api/runtime.ts`、`client.ts`、`ws.ts`：携带随机会话令牌。
- `parsing-core-app/src-tauri/Cargo.toml`、`Cargo.lock`、`src/state.rs`、`src/main.rs`、`src/sidecar.rs`：令牌传递、最小能力和 Clippy。
- `parsing-core-app/src-tauri/tauri.conf.json`、`capabilities/main.json`：收紧 shell 与文件能力。
- `.github/workflows/release.yml`、`scripts/check-release-sidecar.sh`、`scripts/test-release-sidecar.sh`：统一 3.12 Sidecar 和发布前验证。

## 任务 1：审计提交并迁移为唯一 main

**文件：**
- 修改：`.gitignore`
- 验证：Git 本地引用、GitHub 默认分支和远程引用

- [ ] **步骤 1：记录不可混入的工作区状态**

运行：

```bash
git status --short --branch
git log --oneline --decorate origin/master..master
git diff -- .gitignore
```

预期：能看到本地领先提交；`docs/CODE_SIGNING.md` 仍为未跟踪用户文件；`.gitignore` 只新增 `.codegraph/`。

- [ ] **步骤 2：提交 CodeGraph 忽略规则**

```bash
git add .gitignore
git commit -m "chore: ignore local codegraph index"
```

预期：提交只包含 `.gitignore`。

- [ ] **步骤 3：创建并推送 main**

```bash
git switch -c main
git push -u origin main
repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
gh api --method PATCH "repos/$repo" -f default_branch=main
git push origin --delete master
git branch -D master
git fetch --prune
git remote set-head origin -a
```

预期：命令全部成功，GitHub 默认分支为 `main`。

- [ ] **步骤 4：验证本地和远程只保留 main**

运行：

```bash
test "$(git branch --format='%(refname:short)')" = "main"
test "$(git branch -r --format='%(refname:short)' | grep -v 'origin/HEAD')" = "origin/main"
test "$(git symbolic-ref --short refs/remotes/origin/HEAD)" = "origin/main"
```

预期：三个命令退出码均为 `0`。若 `docs/CODE_SIGNING.md` 仍未分类，继续保持未暂存，不能混入任何提交。

- [ ] **步骤 5：记录分支治理收据**

运行：

```bash
gh repo view --json nameWithOwner,defaultBranchRef --jq '{repo: .nameWithOwner, default: .defaultBranchRef.name}'
git show --stat --oneline HEAD
git status --short --branch
```

预期：默认分支为 `main`，最近提交只含 `.gitignore`，用户文件仍未暂存。

## 任务 2：固定 Python 与 Rust 工具链

**文件：**
- 创建：`.python-version`
- 创建：`rust-toolchain.toml`
- 创建：`tests/test_toolchain.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`

- [ ] **步骤 1：编写失败的版本一致性测试**

在 `tests/test_toolchain.py` 写入：

```python
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_python_version_is_consistently_312() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (ROOT / ".python-version").read_text().strip() == "3.12"
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"
    assert pyproject["tool"]["ruff"]["target-version"] == "py312"
    assert pyproject["tool"]["mypy"]["python_version"] == "3.12"


def test_rust_toolchain_requires_quality_components() -> None:
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())
    assert toolchain["toolchain"]["channel"] == "stable"
    assert toolchain["toolchain"]["components"] == ["clippy", "rustfmt"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python3 -m pytest tests/test_toolchain.py -q`

预期：FAIL，报告 `.python-version` 或 `rust-toolchain.toml` 不存在。

- [ ] **步骤 3：写入最小工具链配置**

`.python-version`：

```text
3.12
```

`rust-toolchain.toml`：

```toml
[toolchain]
channel = "stable"
components = ["clippy", "rustfmt"]
profile = "minimal"
```

将 `pyproject.toml` 调整为：

```toml
requires-python = ">=3.12,<3.13"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
strict = false
check_untyped_defs = true
warn_return_any = true
warn_unused_configs = true
exclude = ["tests/", "build/", "dist/"]

[[tool.mypy.overrides]]
module = [
  "parsing_core.workbench.domain.*",
  "parsing_core.workbench.application.*",
  "parsing_core.workbench.ports.*",
]
strict = true
```

运行：

```bash
test ! -e .venv || mv .venv .venv.python313-broken
UV_CACHE_DIR=/tmp/pdf2md-uv-cache uv sync --all-extras --python 3.12
UV_CACHE_DIR=/tmp/pdf2md-uv-cache uv lock --check
```

预期：创建有效的 Python 3.12 虚拟环境，锁文件检查通过。

- [ ] **步骤 4：运行测试和静态检查**

运行：

```bash
UV_CACHE_DIR=/tmp/pdf2md-uv-cache uv run pytest tests/test_toolchain.py -q
UV_CACHE_DIR=/tmp/pdf2md-uv-cache uv run ruff check src tests
```

预期：版本测试通过；Ruff 只允许暴露任务 3 将修复的既有 5 个错误。

- [ ] **步骤 5：Commit**

```bash
git add .python-version rust-toolchain.toml pyproject.toml uv.lock tests/test_toolchain.py
git commit -m "build: pin commercial toolchains"
```

## 任务 3：清零 Python 静态检查并建立架构门禁

**文件：**
- 创建：`tests/test_architecture.py`
- 修改：`src/parsing_core/log.py`
- 修改：`src/parsing_core/orchestrator.py`
- 修改：`src/parsing_core/serving/scheduler.py`
- 创建：`scripts/verify-architecture.sh`

- [ ] **步骤 1：编写失败的架构测试**

在 `tests/test_architecture.py` 写入 AST 检查，完整核心如下：

```python
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "domain": ("fastapi", "sqlite3", "requests", "httpx", "pathlib"),
    "application": ("fastapi", "sqlite3", "requests"),
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_clean_architecture_import_direction() -> None:
    root = ROOT / "src/parsing_core/workbench"
    violations: list[str] = []
    for layer, prefixes in FORBIDDEN.items():
        for path in (root / layer).rglob("*.py") if (root / layer).exists() else ():
            for name in imported_modules(path):
                if name.startswith(prefixes):
                    violations.append(f"{path.relative_to(ROOT)} imports {name}")
    assert violations == []
```

- [ ] **步骤 2：运行当前 Ruff 和架构测试确认失败**

运行：

```bash
uv run ruff check src tests
uv run pytest tests/test_architecture.py -q
```

预期：Ruff 报告 `log.py` 未使用导入、`orchestrator.py` 导入顺序和 `scheduler.py` 顶层导入位置；架构测试在新目录尚未创建时通过。

- [ ] **步骤 3：实施最小清理与门禁脚本**

删除 `src/parsing_core/log.py` 未使用的 `time`；整理 `orchestrator.py` 导入；将 `scheduler.py` 的 `log = get_logger(__name__)` 移到所有导入之后。

`scripts/verify-architecture.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
uv run pytest tests/test_architecture.py -q
uv run mypy --strict src/parsing_core/workbench/domain src/parsing_core/workbench/application
```

运行：`chmod +x scripts/verify-architecture.sh`

- [ ] **步骤 4：验证通过**

运行：

```bash
uv run ruff format --check src tests
uv run ruff check src tests
./scripts/verify-architecture.sh
```

预期：全部退出码为 `0`。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/log.py src/parsing_core/orchestrator.py src/parsing_core/serving/scheduler.py tests/test_architecture.py scripts/verify-architecture.sh
git commit -m "chore: enforce python quality boundaries"
```

## 任务 4：修复前端漏洞并启用严格检查

**文件：**
- 创建：`parsing-core-app/eslint.config.js`
- 创建：`parsing-core-app/.prettierrc.json`
- 修改：`parsing-core-app/package.json`
- 修改：`parsing-core-app/package-lock.json`
- 修改：`parsing-core-app/tsconfig.json`

- [ ] **步骤 1：记录失败基线**

运行：

```bash
cd parsing-core-app
npm audit --audit-level=moderate
```

预期：FAIL；2026-08-12 基线包含 7 个漏洞，其中 5 个 high，涉及 `dompurify <=3.4.12`、`mermaid <11.16.1`、`react-router-dom <7.18.2` 及传递依赖。

- [ ] **步骤 2：升级直接依赖并刷新锁文件**

运行：

```bash
cd parsing-core-app
npm install --save-exact dompurify@3.4.13 mermaid@11.16.1 react-router-dom@7.18.2
npm audit fix --package-lock-only
npm install --save-dev --save-exact eslint@9 @eslint/js@9 typescript-eslint@8 eslint-plugin-react-hooks@5 eslint-plugin-react-refresh@0.4 prettier@3
```

预期：`package.json` 与 `package-lock.json` 只包含可解释的安全和质量工具更新。

- [ ] **步骤 3：写入严格配置和脚本**

`tsconfig.json` 的 `compilerOptions` 必须包含：

```json
{
  "strict": true,
  "noUncheckedIndexedAccess": true,
  "exactOptionalPropertyTypes": true,
  "noFallthroughCasesInSwitch": true,
  "noImplicitReturns": true
}
```

`package.json` 增加：

```json
{
  "scripts": {
    "format:check": "prettier --check src",
    "lint": "eslint src --max-warnings=0",
    "typecheck": "tsc --noEmit"
  }
}
```

`eslint.config.js` 使用 `@eslint/js`、`typescript-eslint`、React Hooks 和 React Refresh 推荐规则；忽略 `dist`、`src-tauri/target`，禁止显式 `any` 和空 `catch`。

- [ ] **步骤 4：验证漏洞和质量门禁**

运行：

```bash
cd parsing-core-app
npm audit --audit-level=moderate
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
```

预期：所有命令退出码为 `0`，审计报告 `0 vulnerabilities`。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/package.json parsing-core-app/package-lock.json parsing-core-app/tsconfig.json parsing-core-app/eslint.config.js parsing-core-app/.prettierrc.json
git commit -m "build: enforce frontend security gates"
```

## 任务 5：统一 OCR 完成状态并保护恢复路径

**文件：**
- 修改：`tests/test_workbench/test_ocr_workflow.py`
- 修改：`src/parsing_core/workbench/ocr/workflow.py`
- 修改：`parsing-core-app/src/api/ocrStatus.test.ts`
- 修改：`parsing-core-app/src/api/workbench.ts`

- [ ] **步骤 1：编写失败的进程重启回归测试**

在 `tests/test_workbench/test_ocr_workflow.py` 增加：

```python
def test_completed_workflow_remains_completed_after_process_restart(completed_state_root, source_pdf):
    workflow = OcrWorkflow(
        source_path=source_pdf,
        state_root=completed_state_root,
        orchestrator_factory=lambda _: pytest.fail("completed work must not rerun"),
    )

    assert workflow.status()["status"] == "completed"
    assert workflow.detect_chapters()["chapters"]
```

在 `parsing-core-app/src/api/ocrStatus.test.ts` 增加后端状态透传测试，断言 `completed` 不被前端转换为 `blocked`。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_workbench/test_ocr_workflow.py -q
cd parsing-core-app && npm test -- src/api/ocrStatus.test.ts
```

预期：至少一个测试因持久状态映射或前端二次归一化失败。

- [ ] **步骤 3：实施单一状态映射**

在 `workflow.py` 增加一个纯函数并让 `status()`、`detect_chapters()` 共用：

```python
def restored_workflow_status(payload: dict[str, object]) -> tuple[WorkflowStatus, str | None]:
    raw = payload.get("status")
    try:
        status = WorkflowStatus(str(raw))
    except ValueError:
        return WorkflowStatus.BLOCKED, "ocr_state_invalid"
    error = payload.get("error")
    return status, str(error) if error else None
```

前端删除与后端不同的完成态推断，仅校验允许值并原样返回。

- [ ] **步骤 4：验证通过和恢复幂等**

运行：

```bash
uv run pytest tests/test_workbench/test_ocr_workflow.py tests/test_workbench/test_api.py -q
cd parsing-core-app && npm test -- src/api/ocrStatus.test.ts
```

预期：全部通过，已完成 OCR 重启后仍可识别章节且不会重新调用 Provider。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/ocr/workflow.py tests/test_workbench/test_ocr_workflow.py parsing-core-app/src/api/workbench.ts parsing-core-app/src/api/ocrStatus.test.ts
git commit -m "fix: preserve completed ocr state"
```

## 任务 6：为全部本地 API 和 WebSocket 增加会话鉴权

**文件：**
- 创建：`tests/test_security.py`
- 修改：`tests/test_serving/test_api_health.py`
- 修改：`tests/test_serving/test_api_ws.py`
- 修改：`src/parsing_core/serving/api/deps.py`
- 修改：`src/parsing_core/serving/serve.py`
- 修改：`src/parsing_core/serving/api/routes_ws.py`
- 修改：`parsing-core-app/src/api/runtime.ts`
- 修改：`parsing-core-app/src/api/client.ts`
- 修改：`parsing-core-app/src/api/ws.ts`
- 修改：`parsing-core-app/src-tauri/src/state.rs`
- 修改：`parsing-core-app/src-tauri/src/main.rs`
- 修改：`parsing-core-app/src-tauri/src/sidecar.rs`

- [ ] **步骤 1：编写失败的未授权测试**

Python 测试必须覆盖：

```python
def test_all_business_routes_require_session_token(client):
    assert client.get("/api/workbench/courses").status_code == 401


def test_wrong_origin_is_rejected(authenticated_client):
    response = authenticated_client.get(
        "/api/workbench/courses", headers={"Origin": "https://attacker.example"}
    )
    assert response.status_code == 403
```

WebSocket 测试断言缺少令牌关闭码为 `4401`，序号补拉接口同样需要令牌。

Rust/运行时测试连续启动两次 Sidecar，断言都只监听 `127.0.0.1`、端口均由操作系统随机分配且互不相同；前端在 ready 事件前不能获得 API 配置。

`tests/test_security.py` 同时覆盖路径穿越、符号链接逃逸、超限请求体、恶意 Markdown/Mermaid 和日志 Secret 脱敏，错误必须是稳定代码而不是堆栈正文。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_serving/test_api_health.py tests/test_serving/test_api_ws.py -q
```

预期：业务 API 当前返回 `200` 或 WebSocket 接受无令牌连接，测试失败。

- [ ] **步骤 3：实施统一认证依赖和启动契约**

`deps.py` 定义：

```python
SESSION_HEADER = "X-PDF2MD-Session"


def require_local_session(request: Request) -> None:
    expected = request.app.state.session_token
    supplied = request.headers.get(SESSION_HEADER, "")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "session_required"})
```

`build_app()` 对 `/api` 路由统一挂载该依赖，只允许 Tauri 自身 Origin 和无 Origin 的 Sidecar 健康探测。Sidecar 绑定 `127.0.0.1:0`，从实际 socket 获取随机端口后输出一条有 Schema 的 ready JSON；Tauri 启动时生成 32 字节随机值，作为环境变量传给 Sidecar，解析 ready JSON 后通过 `get_api_config` 返回 `{ apiBase, sessionToken }`；HTTP 和 WebSocket 客户端统一携带令牌。

- [ ] **步骤 4：验证 HTTP、WebSocket 和 Tauri 契约**

运行：

```bash
uv run pytest tests/test_serving/test_api_health.py tests/test_serving/test_api_ws.py tests/test_serving/test_serve_e2e.py -q
(cd parsing-core-app && npm test -- src/api/runtime.test.ts)
(cd parsing-core-app/src-tauri && cargo test)
```

预期：未授权和错误 Origin 全部拒绝；正确令牌可访问；日志不打印令牌。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/serving parsing-core-app/src/api parsing-core-app/src-tauri/src tests/test_serving
git commit -m "fix: authenticate local desktop api"
```

## 任务 7：收紧 Tauri 能力并建立 Rust 门禁

**文件：**
- 创建：`parsing-core-app/src-tauri/tests/api_session.rs`
- 修改：`parsing-core-app/src-tauri/Cargo.toml`
- 修改：`parsing-core-app/src-tauri/Cargo.lock`
- 修改：`parsing-core-app/src-tauri/capabilities/main.json`
- 修改：`parsing-core-app/src-tauri/tauri.conf.json`
- 修改：`parsing-core-app/src-tauri/src/main.rs`
- 修改：`parsing-core-app/src-tauri/src/sidecar.rs`

- [ ] **步骤 1：编写失败的 Rust 配置测试**

测试读取 capability 和 Tauri 配置，断言不允许任意 shell open，Sidecar 仅能执行已打包二进制：

```rust
#[test]
fn capability_does_not_grant_arbitrary_shell_open() {
    let value: serde_json::Value = serde_json::from_str(include_str!(
        "../capabilities/main.json"
    )).unwrap();
    let permissions = value["permissions"].as_array().unwrap();
    assert!(!permissions.iter().any(|p| p.as_str() == Some("shell:allow-open")));
}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app/src-tauri && cargo test --test api_session`

预期：FAIL，当前 capability 含宽泛 shell 权限。

- [ ] **步骤 3：移除宽泛权限并修复 Clippy**

只保留窗口、对话框、托盘和声明过的 Sidecar 执行权限；所有外链通过后端校验 `https` 白名单后调用系统浏览器。安装工具组件并格式化：

```bash
rustup component add clippy rustfmt
cd parsing-core-app/src-tauri
cargo fmt
```

- [ ] **步骤 4：验证 Rust 门禁**

运行：

```bash
cd parsing-core-app/src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

预期：全部退出码为 `0`。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src-tauri
git commit -m "security: minimize desktop capabilities"
```

## 任务 8：建立可重复 CI 和统一验证脚本

**文件：**
- 创建：`.github/workflows/ci.yml`
- 创建：`scripts/verify-fast.sh`
- 创建：`scripts/verify-security.sh`
- 修改：`.github/workflows/release.yml`
- 修改：`scripts/check-release-sidecar.sh`
- 修改：`scripts/test-release-sidecar.sh`

- [ ] **步骤 1：编写失败的工作流契约测试**

在 `tests/test_release_workflow.py` 增加：

```python
def test_ci_uses_python_312_and_full_sha_actions(repo_root: Path) -> None:
    text = (repo_root / ".github/workflows/ci.yml").read_text()
    assert 'python-version: "3.12"' in text
    for line in text.splitlines():
        if "uses:" in line:
            ref = line.split("@", 1)[1].strip()
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_release_workflow.py -q`

预期：FAIL，因为 `ci.yml` 不存在，Release 仍使用标签引用和 Python 3.13。

- [ ] **步骤 3：创建统一脚本与 CI**

`scripts/verify-fast.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src/parsing_core
uv run pytest -q --cov=parsing_core --cov-fail-under=85
(cd parsing-core-app && npm ci && npm run format:check && npm run lint && npm run typecheck && npm test && npm run build)
(cd parsing-core-app/src-tauri && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test)
```

`scripts/verify-security.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
(cd parsing-core-app && npm audit --audit-level=moderate)
uv run pytest tests/test_serving/test_api_health.py tests/test_serving/test_api_ws.py tests/test_security.py -q
git grep -nE '(sk-[A-Za-z0-9]{20,}|AKID[A-Za-z0-9]{12,})' -- ':!docs/superpowers' && exit 1 || true
```

CI 使用锁文件安装、并发取消、最小权限和完整 SHA 固定 Action；Release 统一 Python 3.12。

- [ ] **步骤 4：执行完整基线验证**

运行：

```bash
chmod +x scripts/verify-fast.sh scripts/verify-security.sh
./scripts/verify-fast.sh
./scripts/verify-architecture.sh
./scripts/verify-security.sh
git status --short
```

预期：三个脚本全部通过；工作区只允许显示明确保留的用户文件 `docs/CODE_SIGNING.md`。

- [ ] **步骤 5：Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/release.yml scripts tests/test_release_workflow.py
git commit -m "ci: establish commercial quality baseline"
```

## M0 完成收据

- [ ] GitHub 默认分支、本地分支和远程普通分支均只剩 `main`。
- [ ] `./scripts/verify-fast.sh`、`verify-architecture.sh`、`verify-security.sh` 全绿。
- [ ] npm 审计为 0 个 moderate/high/critical 漏洞。
- [ ] 未授权 HTTP、WebSocket、错误 Origin 和宽泛 Tauri shell 权限均被拒绝。
- [ ] OCR 完成态在重启后保持完成且不重复调用 Provider。
- [ ] 干净克隆可按锁文件构建 Sidecar 和前端生产包。
