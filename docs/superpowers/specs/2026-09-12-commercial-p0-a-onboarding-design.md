# P0-A 开箱可用设计（环境自检 / Provider 配置 / 百度页级隔离 / 可操作错误）

## 1. 文档状态

- 日期：2026-09-12
- 状态：设计已确认，待实现计划
- 范围：商业化 P0 清单中的 P0-1（核心依赖配置闭环、首启引导）与 P0-6（可操作错误）
- 上游依据：`docs/superpowers/specs/2026-08-12-commercial-mba-reading-workbench-design.md`；商业化差距审计（2026-09-12）

本设计不涉及托管 Provider（P0-2/E）、账号与支付（P0-8/F）、签名公证（P0-7/D）、法律文书（P0-3/C）与本项目的 i18n/暗色（P1）。

## 2. 背景与问题

正式打包应用启动 sidecar 时 `PATH` 被固定为 `/usr/bin:/bin`（`parsing-core-app/scripts/sidecar_runtime.py:4403`），Finder 启动的 App 无法通过 PATH 找到 Homebrew/npm 安装的 Codex CLI；`WorkbenchSettings.codex_cli_path` 字段存在但没有写入路由、运行时也不读取。百度 OCR Key 仅从环境变量 `PDF2MD_BAIDU_API_KEY` 读取，缺失时 OCR factory 直接抛错阻断整本书（`src/parsing_core/serving/api/routes_workbench.py:337-340`），与 README“可选配置”的承诺矛盾。同时后端明确错误（缺 Key、缺 CLI）被前端统一映射为通用文案，用户无法自助修复。

本设计让正式包在无开发者环境的前提下完成：**探测依赖 → 引导配置 → 校验保存 → 按状态运行 → 失败可操作**，并让无百度 Key 时仍能产出（冲突页隔离为待复核）。

## 3. 范围

### 3.1 包含

1. 环境自检 API（`GET /api/workbench/environment`）。
2. Codex 路径与百度 Key 的配置端点；运行时接线（settings/Keychain 优先，env 兼容）。
3. 首启环境自检卡（非阻塞、可折叠）与空状态引导。
4. 百度缺失时的页级隔离（`REVIEW_PENDING`）、批次 `REVIEW_REQUIRED` 终态、带待复核清单的发布，以及“继续复核”端点。
5. 结构化错误码与前端动作映射（覆盖本次涉及路径）。

### 3.2 不包含

- 自动更新、崩溃上报、帮助中心、导出、i18n、暗色（P1）。
- 托管 Provider、账号/支付/配额（P0-E/F）。
- Keychain argv 硬化（P1-11，本次沿用现有实现并保持接口兼容）。
- 百度 Key 的真实联网测试调用（避免费用与合规面；仅做存在性与格式校验）。

## 4. 用户故事

- 作为首次启动的用户，我在首页看到“环境就绪”卡：DeepSeek 已就绪、Codex 未配置、百度可选；我点“选择 Codex 路径”并在文件选择器里选中可执行文件，状态变为已就绪。
- 作为未配置代码环境但已配百度/DeepSeek 的用户，启动 OCR 时若 Codex 不可用，我得到的不是通用失败，而是“Codex 不可用”加一个“去配置”按钮。
- 作为没有百度 Key 的用户，我照常完成整本 OCR；冲突/复杂/抽检页被隔离为“待复核 N 页”，其余内容正常发布且成果标注 `review_pending: N`。
- 作为事后配置了百度 Key 的用户，我点击“继续复核”，系统只重跑隔离页并把任务升级为完成。

## 5. 设计

### 5.1 环境自检 API

`GET /api/workbench/environment` 返回：

```json
{
  "app_version": "0.1.4",
  "data_dir": {"path": "/Users/x/Library/...", "writable": true},
  "deepseek": {"state": "ready", "model": "deepseek-v4-pro", "last_test_ok": true, "detail_code": null},
  "codex": {"state": "missing", "path": null, "source": null, "detail_code": "codex_not_found"},
  "baidu": {"state": "optional", "masked": null},
  "vision": {"state": "ready"}
}
```

- `state ∈ {ready, missing, invalid, optional}`。百度未配置为 `optional`（不阻断）。
- `detail_code` 为机器可读原因：`codex_not_found`、`codex_not_regular_file`、`codex_is_symlink`、`codex_not_executable`、`codex_version_mismatch`、`codex_layout_unsupported`；DeepSeek：`deepseek_key_missing`。
- Codex 来源（`source`）为 `settings | environment | detected`，按该优先级解析。
- 探测目录（仅用于 `detected`）：`/opt/homebrew/bin`、`/usr/local/bin`、`~/.local/bin`、`~/.npm-global/bin`、`~/Library/pnpm`、`~/.bun/bin`，以及设置中保存的路径。
- 该端点只做无副作用检查：不执行 `codex exec`，不发起联网请求；Codex 版本检查执行 `codex --version` 并短超时（2 秒）。
- 校验复用 `secure_codex.py` 的结构检查（真实文件、非符号链接、属主、权限、可执行）与已钉版本比较；SHA/布局强校验仍保留在真正启动时（fail-closed 不变）。

### 5.2 配置端点

| 端点 | 请求 | 行为 |
|---|---|---|
| `POST /api/workbench/settings/codex` | `{"path": "/abs/path" \| null}` | 结构校验 + 版本校验；通过后写入 workbench settings（`codex_cli_path`），否则 422 `codex_invalid` + `params.reason` |
| `DELETE /api/workbench/settings/codex` | — | 清除自定义路径，回退探测 |
| `POST /api/workbench/settings/baidu` | `{"api_key": "..."}` | 写入 macOS Keychain（service `pdf2md`，account `baidu_api_key`），返回掩码；空串等价删除 |
| `DELETE /api/workbench/settings/baidu` | — | 清除 Keychain 中的百度 Key |
| `GET /api/workbench/environment` | — | 见 5.1；保存任一配置后前端重新拉取刷新 |

运行时接线：
- `resolve_codex_path(configured)`：优先 settings、其次 `CODEX_CLI_PATH` 环境变量、最后探测；OCR 与主题生成的 factory 均传入设置值（当前调用无参，需改）。
- 百度 Key 读取顺序：Keychain → `PDF2MD_BAIDU_API_KEY`（兼容）。由独立函数 `resolve_baidu_api_key()` 提供，OCR factory 与设置状态共用。

### 5.3 首启环境自检卡

- 位置：工作台首页顶部（`parsing-core-app/src/components/workbench/` 新组件 `EnvironmentCard.tsx`）。
- 行为：首次进入自动展开；全部 `ready/optional` 后折叠为单行（“环境就绪 · 3/3”）；状态实时来自 environment API；“重新检测”按钮手动刷新。
- 每项：状态图标、名称、`detail_code` 映射说明、主按钮：
  - DeepSeek：`去配置` → 精读设置；
  - Codex：`选择路径`（Tauri dialog）→ 保存 → 刷新；
  - 百度：`配置（可选）`；已配置显示掩码 + `清除`；
  - 数据目录：路径 + `在 Finder 中显示`。
- 无课程时卡片内嵌“创建第一门课程”引导（复用现有课程创建表单）。
- 文件选择使用现有 `@tauri-apps/plugin-dialog`；若 capability 未声明 `dialog:allow-open` 需补充（`parsing-core-app/src-tauri/capabilities/main.json`）。

### 5.4 百度页级隔离与继续复核

**页级状态**：`PageStatus` 新增终态 `REVIEW_PENDING = "review_pending"`。

**行为变更**：`orchestrator._adjudicate` 中 `needs_baidu(...)` 为真且百度引擎不可用时：
- 不再抛 `ValueError("Baidu OCR engine is unavailable")`；
- 将页面状态持久化为 `REVIEW_PENDING`，记录 `review_reason ∈ {conflict, complex, sampled}` 与当时 `alignment_status`；
- 跳过该页终审（**不猜测、不放行**），继续处理后续页。

**批次终态**：`BatchStatus` 新增 `REVIEW_REQUIRED = "review_required"`。当所有非隔离页完成终审且终审发布闸门通过、存在隔离页时，批次以 `REVIEW_REQUIRED` 收尾并写 final 产物。

**final 产物（schema_version 升级）**：
- `status = "review_required"`；
- `review_pages: [{"page": n, "reason": "...", "alignment_status": "..."}]`；
- 其余字段与非隔离页证据保持现有结构；新增校验 `_review_final_is_valid`：隔离页必须全部处于 `REVIEW_PENDING`，非隔离页必须满足现有 COMPLETED 证据校验。

**发布语义**：
- 非隔离页正常进入合并 Markdown（现有 `_write_task_text`/发布路径）；
- 隔离页位置写入注释占位 `<!-- pdf2md: review pending page N -->`；
- OCR 合并 Markdown 顶部写入 `<!-- pdf2md: review_pending=N -->`，final 产物与发布回执同时记录 `review_pending: N`；
- `status_payload` 对 `REVIEW_REQUIRED` 返回 `publishable: true` 与 `review_pages` 清单；`WorkflowStatus` 新增 `REVIEW_REQUIRED`。

**继续复核**：`POST /api/workbench/sources/{source_id}/ocr/review`：
- 前置：存在 `REVIEW_REQUIRED` 的 final 且百度 Key 已配置；否则 409（`ocr_review_not_ready`）。
- 行为：仅重跑 `review_pages`（复用现有页级断点/尝试计数路径），其余页不重跑；全部通过后写 `COMPLETED` final、清除 review 标记；任一页仍失败则保持 `REVIEW_REQUIRED` 并更新清单。
- 并发：同一 source 复用现有 running/claim 机制，避免与整书运行冲突。

**不变量**：
- 待复核页永远不计入 `COMPLETED`、不进入最终正文、不满足完成校验；
- 发布物必须显式携带 review 清单与 `review_pending: N`；
- 现有“失败页不发布”为硬约束保持：`REVIEW_PENDING` 不属于失败，不允许被当作通过。

### 5.5 结构化错误与动作映射

后端：错误响应统一为 `detail = {"code": "<snake_case>", "params": {...}}`；字符串 detail 视为 `{"code": "<detail>"}` 兼容。本期覆盖：

| code | 触发 | 前端动作 |
|---|---|---|
| `deepseek_key_missing` | 精读/主题启动前 | 去配置 DeepSeek |
| `codex_unavailable` / `codex_invalid` | OCR/主题 factory | 选择 Codex 路径 / 查看原因 |
| `baidu_key_missing` | 继续复核前置不满足 | 去配置百度 |
| `ocr_review_pending` | 打开待复核页 | 查看待复核清单 |
| `ocr_review_not_ready` | 继续复核前置不满足 | 去配置百度 |
| `path_escape` 等既有码 | 既有路径 | 保持 |

`ocr_review_not_ready` 通过 `params.reason` 细分触发原因（`baidu_key_missing` 表示未配置 Key；`ocr_review_not_ready` 表示不存在待复核 final），前端统一映射"去配置百度"。

前端：新增 `parsing-core-app/src/api/errorMessages.ts`，映射 `code → {title, description, action}`；`action ∈ {open_deepseek_settings, pick_codex, open_baidu_settings, view_review, retry, open_logs, none}`。`workbench.ts` 解析结构化 detail 并生成 `WorkbenchApiError(code, params, status)`；组件展示映射文案与动作按钮，配置类错误一键跳转设置/自检卡。

## 6. 数据与兼容

- workbench settings 无需新增字段（`codex_cli_path` 已有）；百度 Key 不入 settings 文件。
- OCR final schema 版本升级：旧 `COMPLETED` final 不变；“无 review 字段”的旧 final 视为无隔离页；`REVIEW_REQUIRED` 仅由新版本产生。
- 已有 `PageStatus.BAIDU_PENDING` 保留（升级调用前的持久化），新增 `REVIEW_PENDING` 为终态。
- 错误 detail 结构向后兼容：旧字符串 detail 由前端归一为 `code`。

## 7. 测试策略

- 后端单测：
  - environment 聚合：各依赖 ready/missing/invalid/optional 组合、探测优先级、`detail_code` 映射；
  - Codex 保存校验：合法文件、符号链接、不可执行、版本不符、非文件；保存后运行时实际使用该路径；
  - 百度 Key：Keychain 存取清（mock `security`）、掩码、env 回退、写空串即删除；
  - review 状态机：无百度时冲突/复杂/抽检页隔离、final schema 与校验、发布标注、继续复核只跑隔离页、复核失败保持终态、隔离页不被计为完成；
  - 错误结构：主要路由返回 `{code, params}`。
- 前端单测：`EnvironmentCard` 四种状态与动作、`errorMessages` 映射、设置页 Codex/百度输入校验。
- 回归约束：现有 2807 用例全绿；涉及“整书阻断”的旧断言按新语义更新并新增 review 用例；覆盖率 ≥85%。
- 手工验收（本机）：
  1. 无 Codex 环境：卡片显示未配置，选择路径后变就绪；未配置即启动 OCR 得到可操作错误；
  2. 无百度 Key：整书完成，待复核清单出现，发布物带 `review_pending: N` 与占位符；
  3. 配置百度 Key 后“继续复核”成功，任务升级为完成。

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| OCR 状态机/final 契约改动触及大量恢复与故障注入测试 | A.3 单独成阶段；以“待复核 ≠ 通过”为硬不变量并先写测试；旧 BLOCKED 语义用例逐个迁移 |
| Codex 自定义构建无法通过 SHA/布局校验 | environment 明确 `codex_layout_unsupported` 提示仅支持官方 npm 安装；运行时强校验不变 |
| Keychain argv 暴露（P1-11） | 本期沿用并在文档标注；接口预留 `delete/clear`，便于后续硬化 |
| 前端超时仍影响长任务（P0-5/B） | A 不解决；错误映射保证超时提示可重试并链接 B 的进度方案 |

## 9. 实施顺序（供实现计划展开）

1. A.1/A.2 后端：environment 端点、Codex/百度配置端点、运行时接线。
2. A.4 结构化错误（与 1 同批，涉及路由较多）。
3. A.2 前端：自检卡、设置页 Codex/百度、错误动作映射。
4. A.3 OCR 隔离：页/批次状态、final schema、发布标注、继续复核端点与 UI。
5. 测试与验收（含旧断言迁移）。

## 10. 验收标准

- 无 Codex 的干净用户可通过 UI 完成 Codex 路径配置并使 `environment.codex.state == "ready"`；
- 未配置依赖时，任何核心动作的失败都可一键定位到修复入口；
- 无百度 Key 时整本 OCR 可产出，隔离清单与 `review_pending: N` 准确；
- 配置百度 Key 后“继续复核”仅重跑隔离页并最终 `COMPLETED`；
- 隔离页永不满足完成校验；
- 现有质量门禁全绿且覆盖率不低于 85%。
