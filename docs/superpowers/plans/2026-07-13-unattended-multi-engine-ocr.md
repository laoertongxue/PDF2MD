# PDF2MD 无人值守多引擎 OCR 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 PDF2MD 增加 Apple Vision、Codex CLI Vision、百度 PP-StructureV3 和 Codex 自动终审组成的无人值守 OCR 流程，并用两本真实扫描教材完成全量 OCR、章节识别、代表章节精读和跨教材融合验收。

**架构：** macOS Swift helper 负责 PDF 页面渲染和 Apple Vision 原始观察；Python OCR 域负责页面状态、三引擎适配、差异检测、百度升级和最终裁决；SQLite 保存可恢复任务和全部审计证据。正式页面块通过单一事务发布并驱动现有章节、DeepSeek 和主题融合流程，前端仅通过 FastAPI 操作设置、队列、证据和结果。

**技术栈：** Swift 6、PDFKit、Vision、Tauri 2、Python 3.13、FastAPI、SQLite、Codex CLI、百度千帆 PP-StructureV3、React 19、TypeScript、Vitest、Playwright、pytest。

---

## 文件结构

### 新建文件

| 文件 | 职责 |
|---|---|
| `parsing-core-app/src-tauri/vision-ocr/main.swift` | PDFKit 页面渲染与 Vision OCR JSONL helper |
| `parsing-core-app/scripts/build-vision-ocr.sh` | 为当前目标架构编译并验证 Swift helper |
| `src/parsing_core/workbench/ocr/models.py` | OCR 页面、观察、差异、裁决和状态类型 |
| `src/parsing_core/workbench/ocr/vision.py` | Swift helper 进程协议适配器 |
| `src/parsing_core/workbench/ocr/codex_vision.py` | Codex CLI 首轮视觉转录和终审适配器 |
| `src/parsing_core/workbench/ocr/baidu.py` | 百度 PP-StructureV3 客户端、缓存和错误映射 |
| `src/parsing_core/workbench/ocr/alignment.py` | 坐标归一化、文本块对齐和升级判断 |
| `src/parsing_core/workbench/ocr/orchestrator.py` | 持久化 lease、断点恢复、三引擎编排和事务发布 |
| `src/parsing_core/workbench/ocr/keychain.py` | 百度 API Key 的 Keychain 薄封装常量 |
| `src/parsing_core/serving/api/routes_ocr.py` | OCR 设置、任务、页面证据和恢复 API |
| `parsing-core-app/src/components/workbench/OcrSettings.tsx` | 百度设置和测试连接 UI |
| `parsing-core-app/src/components/workbench/OcrProgress.tsx` | OCR 队列、费用、冲突和断点状态 UI |
| `tests/test_workbench/test_ocr_*.py` | OCR 域单元、事务、故障注入与网络契约测试 |
| `parsing-core-app/src-tauri/tests/vision-ocr-fixtures/` | Vision helper 的小型公开测试页面 |
| `scripts/run-real-textbook-acceptance.py` | 两本真实教材阶段 A 无人值守验收驱动器 |
| `docs/acceptance/ocr-stage-a/README.md` | 真实教材验收索引与结果说明 |

### 重点修改文件

| 文件 | 修改职责 |
|---|---|
| `src/parsing_core/workbench/schema.py` | OCR 表、索引和幂等迁移 |
| `src/parsing_core/workbench/repository.py` | OCR lease、观察、裁决和正式页面块事务 |
| `src/parsing_core/workbench/source_import.py` | 扫描 PDF 检测和 OCR 任务入口 |
| `src/parsing_core/workbench/task_package.py` | 将正式页面块和 citation ID 送入精读 |
| `src/parsing_core/serving/models/api.py` | OCR API 请求响应模型 |
| `src/parsing_core/serving/api/router.py` | 注册 OCR 路由 |
| `parsing-core-app/src-tauri/tauri.conf.json` | 打包 Vision helper |
| `parsing-core-app/src-tauri/Cargo.toml` | helper 资源和构建配置所需依赖 |
| `parsing-core-app/src/api/workbench.ts` | OCR API 客户端 |
| `parsing-core-app/src/api/workbenchTypes.ts` | OCR 前端类型 |
| `parsing-core-app/src/components/workbench/Settings.tsx` | 组合 DeepSeek 与 OCR 设置 |
| `parsing-core-app/src/components/workbench/ImportTextbooks.tsx` | 扫描 PDF 检测、OCR 启动和进度入口 |
| `parsing-core-app/scripts/accept-task-12-business.mjs` | 删除手写后端，改用真实 FastAPI 验收 |
| `.github/workflows/release.yml` | 编译 helper 并运行 OCR 打包检查 |

---

### 任务 0：清理并审计中断的验收实验

**文件：**
- 审查：`docs/acceptance/task-12/*`
- 审查：`parsing-core-app/scripts/accept-task-12-business.mjs`
- 审查：`parsing-core-app/scripts/task-12-fixture-server.mjs`
- 审查：`src/parsing_core/serving/api/routes_topics.py`
- 审查：`src/parsing_core/serving/api/routes_workbench.py`
- 审查：`src/parsing_core/serving/models/api.py`
- 审查：`pyproject.toml`

- [ ] **步骤 1：保存中断改动的外部补丁**

运行：

```bash
git diff --binary > /tmp/pdf2md-interrupted-real-acceptance.patch
git status --short
```

预期：补丁文件非空；工作树列出的改动与规格提交时记录一致。

- [ ] **步骤 2：恢复已提交基线并移除生成垃圾**

```bash
git restore docs/acceptance/task-12 \
  parsing-core-app/scripts/accept-task-12-business.mjs \
  parsing-core-app/scripts/task-12-fixture-server.mjs \
  pyproject.toml \
  src/parsing_core/serving/api/routes_topics.py \
  src/parsing_core/serving/api/routes_workbench.py \
  src/parsing_core/serving/models/api.py
rm -f parsing-core-app/src-tauri/.sidecar-runtime.lock.guard uv.lock
git status --short
```

预期：只保留已经批准的规格和计划提交，工作树干净。不要把 `PDF2MD_SERVE_TEST_EXECUTOR` 环境后门恢复到生产路由。

- [ ] **步骤 3：验证当前基线**

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
cd parsing-core-app && npm test -- --run && npm run build
cd src-tauri && cargo test && cargo check --all-targets
```

预期：Python、前端和 Rust 全部通过。

- [ ] **步骤 4：提交清理结果（仅在有受控文件变化时）**

```bash
git diff --check
git status --short
```

预期：若恢复操作没有产生相对 HEAD 的变化，不创建空提交。

---

### 任务 1：定义 OCR 域类型与 SQLite 迁移

**文件：**
- 创建：`src/parsing_core/workbench/ocr/__init__.py`
- 创建：`src/parsing_core/workbench/ocr/models.py`
- 修改：`src/parsing_core/workbench/schema.py`
- 修改：`src/parsing_core/workbench/models.py`
- 测试：`tests/test_workbench/test_ocr_schema.py`

- [ ] **步骤 1：编写失败的幂等迁移测试**

```python
def test_ocr_schema_is_idempotent(tmp_path):
    db = tmp_path / "workbench.db"
    apply_workbench_schema(db)
    apply_workbench_schema(db)
    names = table_names(db)
    assert {"wb_ocr_pages", "wb_ocr_observations", "wb_ocr_diffs",
            "wb_ocr_decisions", "wb_page_blocks", "wb_ocr_leases"} <= names
```

- [ ] **步骤 2：运行测试并确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_schema.py -q
```

预期：FAIL，缺少 OCR 表。

- [ ] **步骤 3：定义不可变域类型**

`models.py` 至少定义：

```python
@dataclass(frozen=True)
class OcrObservation:
    id: str
    page_id: str
    engine: Literal["apple_vision", "codex_vision", "baidu_pp_structure"]
    input_hash: str
    payload_json: str
    created_at: int

@dataclass(frozen=True)
class OcrDecision:
    page_id: str
    status: Literal["direct", "automated_adjudicated", "waiting_resource", "failed"]
    final_blocks_json: str
    evidence_json: str
    confidence: float
```

- [ ] **步骤 4：实现表、外键、唯一约束和索引**

要求：

- `(source_id, page_number, render_config_hash)` 唯一。
- `(page_id, engine, input_hash, engine_config_hash)` 唯一，支持缓存。
- lease 保存 owner、heartbeat、input fingerprint。
- 正式页面块按 `(page_id, seq)` 唯一。
- 删除课程时级联删除本地 OCR 结果。

- [ ] **步骤 5：运行迁移和全仓 schema 测试**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_schema.py tests/test_workbench/test_schema.py -q
```

预期：PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/ocr src/parsing_core/workbench/schema.py \
  src/parsing_core/workbench/models.py tests/test_workbench/test_ocr_schema.py
git commit -m "feat(ocr): add auditable OCR domain schema"
```

---

### 任务 2：实现 Apple Vision Swift helper

**文件：**
- 创建：`parsing-core-app/src-tauri/vision-ocr/main.swift`
- 创建：`parsing-core-app/scripts/build-vision-ocr.sh`
- 创建：`parsing-core-app/src-tauri/tests/vision-ocr-fixtures/`
- 修改：`parsing-core-app/src-tauri/tauri.conf.json`
- 修改：`parsing-core-app/scripts/tauri.sh`
- 测试：`parsing-core-app/src-tauri/tests/test-vision-ocr.sh`

- [ ] **步骤 1：创建 helper 协议测试夹具**

输入 JSONL：

```json
{"command":"render_and_recognize","pdf_path":"fixture.pdf","page":1,"dpi":300,"languages":["zh-Hans","en-US"]}
```

断言输出包含：`page`、`image_path`、`image_sha256`、`width`、`height`、`supported_languages`、`observations`，且每个 observation 有 `text`、`confidence`、`bounding_box` 和 `candidates`。

- [ ] **步骤 2：运行测试确认 helper 缺失**

```bash
bash parsing-core-app/src-tauri/tests/test-vision-ocr.sh
```

预期：FAIL，helper 不存在。

- [ ] **步骤 3：实现 PDFKit 渲染和 Vision OCR**

要求：

- `PDFDocument` 只读打开 PDF。
- `CGContext` 按 DPI 渲染，不使用低分辨率 thumbnail。
- `VNRecognizeTextRequest.recognitionLevel = .accurate`。
- 运行时调用 supported languages，缺少 `zh-Hans` 时返回明确错误。
- 坐标统一转换为左上原点、0-1 归一化矩形。
- helper 不读取网络、不写课程数据库。

- [ ] **步骤 4：实现可重复构建脚本**

```bash
xcrun swiftc -O -framework Vision -framework PDFKit -framework AppKit \
  vision-ocr/main.swift -o binaries/vision-ocr-aarch64-apple-darwin
file binaries/vision-ocr-aarch64-apple-darwin
```

构建脚本按 `TAURI_ENV_TARGET_TRIPLE` 命名，拒绝架构不匹配。

- [ ] **步骤 5：运行 helper 测试与签名检查**

```bash
bash parsing-core-app/src-tauri/tests/test-vision-ocr.sh
codesign --verify --strict parsing-core-app/src-tauri/binaries/vision-ocr-aarch64-apple-darwin
```

预期：fixture 识别结果稳定，helper 可被 App bundle 签名。

- [ ] **步骤 6：Commit**

```bash
git add parsing-core-app/src-tauri/vision-ocr parsing-core-app/src-tauri/tests \
  parsing-core-app/scripts/build-vision-ocr.sh parsing-core-app/scripts/tauri.sh \
  parsing-core-app/src-tauri/tauri.conf.json
git commit -m "feat(ocr): bundle Apple Vision page helper"
```

---

### 任务 3：实现 Vision helper Python 适配器和页面缓存

**文件：**
- 创建：`src/parsing_core/workbench/ocr/vision.py`
- 创建：`src/parsing_core/workbench/ocr/page_cache.py`
- 测试：`tests/test_workbench/test_ocr_vision.py`

- [ ] **步骤 1：编写协议、超时和缓存失败测试**

```python
def test_vision_result_is_cached_by_page_and_render_hash(tmp_path, fake_helper):
    client = VisionClient(fake_helper, tmp_path)
    first = client.recognize("book.pdf", page=1, dpi=300)
    second = client.recognize("book.pdf", page=1, dpi=300)
    assert first == second
    assert fake_helper.calls == 1
```

同时覆盖 helper 非零退出、非法 JSON、超时、输出图片哈希不匹配和路径逃逸。

- [ ] **步骤 2：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_vision.py -q
```

- [ ] **步骤 3：实现固定 argv 子进程调用**

禁止 `shell=True`。helper 路径必须来自打包资源或测试依赖注入；输入 PDF 必须是已注册 source 的规范绝对路径。

- [ ] **步骤 4：实现内容寻址缓存**

缓存 key：`pdf_sha256 + page + dpi + helper_version + language_config`。先写临时文件，校验哈希后原子 rename。

- [ ] **步骤 5：运行测试并 Commit**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_vision.py -q
git add src/parsing_core/workbench/ocr/vision.py \
  src/parsing_core/workbench/ocr/page_cache.py tests/test_workbench/test_ocr_vision.py
git commit -m "feat(ocr): add Vision protocol and page cache"
```

---

### 任务 4：实现 Codex CLI 视觉转录与终审

**文件：**
- 创建：`src/parsing_core/workbench/ocr/codex_vision.py`
- 创建：`src/parsing_core/workbench/ocr/schemas/page-transcription.json`
- 创建：`src/parsing_core/workbench/ocr/schemas/page-adjudication.json`
- 修改：`src/parsing_core/workbench/codex_cli.py`
- 测试：`tests/test_workbench/test_ocr_codex.py`

- [ ] **步骤 1：编写 argv 和 schema 测试**

断言实际命令包含：

```text
codex exec --ephemeral --ignore-user-config --sandbox read-only \
  --image page.png --output-schema page-transcription.json \
  --output-last-message result.json -
```

禁止复用 session、`resume`、危险 sandbox 或工作区写权限。

- [ ] **步骤 2：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_codex.py -q
```

- [ ] **步骤 3：实现首轮转录 Schema**

必须输出页面区域、block 类型、原文、候选、不确定原因、表格矩阵、公式 LaTeX 和阅读顺序；禁止只有 Markdown 字符串。

- [ ] **步骤 4：实现终审 Schema**

终审输入包含原图、三方 observation 和 diff。输出字段与规格中的 `final_blocks`、`resolved_conflicts`、`decision_evidence`、`confidence`、`status` 完全一致。

- [ ] **步骤 5：实现资源边界**

- 单次最多 4 个裁剪图。
- 每个页面独立临时目录。
- 超时后 TERM、等待、KILL、wait。
- stdout/stderr 不记录图片或 Keychain 数据。
- JSON Schema 验证失败可重试一次，第二次失败进入可恢复错误。

- [ ] **步骤 6：运行测试并 Commit**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_codex.py tests/test_workbench/test_codex_cli.py -q
git add src/parsing_core/workbench/ocr/codex_vision.py \
  src/parsing_core/workbench/ocr/schemas src/parsing_core/workbench/codex_cli.py \
  tests/test_workbench/test_ocr_codex.py
git commit -m "feat(ocr): add ephemeral Codex vision executors"
```

---

### 任务 5：实现文本块对齐、冲突分类和百度升级规则

**文件：**
- 创建：`src/parsing_core/workbench/ocr/alignment.py`
- 测试：`tests/test_workbench/test_ocr_alignment.py`

- [ ] **步骤 1：编写表驱动测试**

覆盖：空格、全半角标点、简繁差异、漏行、数字冲突、公式运算符、表格行列、页码、脚注和多栏阅读顺序。

```python
@pytest.mark.parametrize(("apple", "codex", "reason"), [
    ("利润为 10%", "利润为 10%", None),
    ("利润为 10%", "利润为 40%", "numeric_conflict"),
    ("x <= 3", "x >= 3", "formula_operator_conflict"),
])
def test_escalation_reasons(apple, codex, reason): ...
```

- [ ] **步骤 2：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_alignment.py -q
```

- [ ] **步骤 3：实现坐标匹配和规范化**

文本规范化只用于比较；正式结果保留原始字符。数字、变量、单位和公式符号禁止模糊匹配。

- [ ] **步骤 4：实现升级判断**

`needs_baidu(page_hash, observations, sample_seed)` 必须是确定性的。5% 抽样由稳定哈希决定，重启后不能改变样本。

- [ ] **步骤 5：运行测试并 Commit**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_alignment.py -q
git add src/parsing_core/workbench/ocr/alignment.py tests/test_workbench/test_ocr_alignment.py
git commit -m "feat(ocr): classify OCR conflicts deterministically"
```

---

### 任务 6：实现百度 PP-StructureV3 设置与客户端

**文件：**
- 创建：`src/parsing_core/workbench/ocr/keychain.py`
- 创建：`src/parsing_core/workbench/ocr/baidu.py`
- 修改：`src/parsing_core/workbench/settings.py`
- 修改：`src/parsing_core/serving/api/routes_ocr.py`
- 修改：`src/parsing_core/serving/models/api.py`
- 测试：`tests/test_workbench/test_ocr_baidu.py`
- 测试：`tests/test_workbench/test_ocr_settings_api.py`

- [ ] **步骤 1：编写 Keychain、脱敏和 HTTP 错误测试**

覆盖 401、402/余额不足、429、5xx、超时、响应过大、非法 JSON、Key 泄漏扫描和缓存命中。

- [ ] **步骤 2：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_baidu.py \
  tests/test_workbench/test_ocr_settings_api.py -q
```

- [ ] **步骤 3：实现固定请求**

请求必须固定：

```json
{
  "model": "pp-structurev3",
  "fileType": 1,
  "useRegionDetection": true,
  "useTableRecognition": true,
  "useFormulaRecognition": true,
  "useOcrResultsWithTableCells": true
}
```

图片以 Base64 发送，但日志过滤 `file` 和 Authorization。单图超过官方限制时，先本地裁剪，禁止静默降分辨率。

- [ ] **步骤 4：实现 Keychain 设置 API**

API：

- `GET /api/workbench/ocr/settings`
- `POST /api/workbench/ocr/settings/baidu`
- `POST /api/workbench/ocr/settings/baidu/test`

响应只返回 `baidu_key_masked`、固定模型、调用上限和测试状态。

- [ ] **步骤 5：实现有限重试和 WAITING_FOR_RESOURCE 映射**

429/5xx 使用带抖动的有限退避；401 立即失败；余额不足和调用上限转为 `WAITING_FOR_RESOURCE`，不得切换为单模型发布。

- [ ] **步骤 6：运行测试并 Commit**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_baidu.py \
  tests/test_workbench/test_ocr_settings_api.py -q
git add src/parsing_core/workbench/ocr/baidu.py \
  src/parsing_core/workbench/ocr/keychain.py src/parsing_core/workbench/settings.py \
  src/parsing_core/serving/api/routes_ocr.py src/parsing_core/serving/models/api.py \
  tests/test_workbench/test_ocr_baidu.py tests/test_workbench/test_ocr_settings_api.py
git commit -m "feat(ocr): add bounded Baidu adjudication client"
```

---

### 任务 7：实现持久化 OCR orchestrator 和事务发布

**文件：**
- 创建：`src/parsing_core/workbench/ocr/orchestrator.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 测试：`tests/test_workbench/test_ocr_orchestrator.py`
- 测试：`tests/test_workbench/test_ocr_publication_atomicity.py`

- [ ] **步骤 1：编写 lease 和重启恢复测试**

验证相同页面并发只有一个 owner；heartbeat 过期后原任务标记 interrupted；新 owner 从缺失步骤继续，不重复 Apple、Codex 或百度缓存调用。

- [ ] **步骤 2：编写所有发布步骤故障注入测试**

在 observation、diff、decision、page block、source OCR status 和正式 Markdown 同步各步骤注入异常，断言数据库和文件均保持旧版本。

- [ ] **步骤 3：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_orchestrator.py \
  tests/test_workbench/test_ocr_publication_atomicity.py -q
```

- [ ] **步骤 4：实现显式状态机**

```text
PENDING -> RENDERING -> PRIMARY_OCR -> DIFFING
DIFFING -> DIRECT_ACCEPTED
DIFFING -> BAIDU_PENDING -> ADJUDICATING -> ADJUDICATED
任意可恢复错误 -> INTERRUPTED
余额/上限 -> WAITING_FOR_RESOURCE
```

状态更新必须使用 owner 和 input fingerprint 条件更新。

- [ ] **步骤 5：实现三引擎编排**

Apple 与首轮 Codex 可并行；百度只由确定性升级规则触发；终审必须使用新的 Codex invocation。复杂页的局部二次裁剪最多两轮，超过后记录 `automated_adjudicated` 的低置信证据，不得删除冲突记录。

- [ ] **步骤 6：实现单一发布事务**

内部写方法不得自行 commit。正式 `wb_page_blocks`、source OCR 状态、decision 和章节输入失效标记由最外层事务一次提交；文件使用备份加原子交换并支持补偿。

- [ ] **步骤 7：运行测试并 Commit**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_orchestrator.py \
  tests/test_workbench/test_ocr_publication_atomicity.py -q
git add src/parsing_core/workbench/ocr/orchestrator.py \
  src/parsing_core/workbench/repository.py tests/test_workbench/test_ocr_orchestrator.py \
  tests/test_workbench/test_ocr_publication_atomicity.py
git commit -m "feat(ocr): orchestrate resumable multi-engine decisions"
```

---

### 任务 8：接入扫描 PDF 导入、章节和 citation

**文件：**
- 修改：`src/parsing_core/workbench/source_import.py`
- 修改：`src/parsing_core/workbench/chapter_detection.py`
- 修改：`src/parsing_core/workbench/task_package.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 修改：`src/parsing_core/serving/api/routes_ocr.py`
- 测试：`tests/test_workbench/test_ocr_source_flow.py`
- 测试：`tests/test_workbench/test_ocr_citations.py`

- [ ] **步骤 1：编写扫描 PDF 检测测试**

真实最小 PDF fixture 必须有图像页且无文本层。导入后 source 状态为 `OCR_REQUIRED`，不能生成空的“全文”章节。

- [ ] **步骤 2：编写正式页面块章节测试**

用 OCR page blocks 构造目录、H1 文档标题和 H2 章节标题，断言章节边界、PDF 页码和 block ID 正确。

- [ ] **步骤 3：编写 citation 白名单测试**

任务包只允许 `[source_id:pdf_page:block_id]`；未知页、旧输入指纹和已替换 block 必须拒绝发布。

- [ ] **步骤 4：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_source_flow.py \
  tests/test_workbench/test_ocr_citations.py -q
```

- [ ] **步骤 5：实现扫描检测与 OCR 入口**

检测至少抽样首页、目录候选页和中部页的文本字符数；纯扫描 PDF 不进入 MarkItDown 空文本路径。文本 PDF 保留现有快速路径。

- [ ] **步骤 6：实现章节快照和下游失效**

章节确认保存 page/block 边界和 OCR decision 指纹。OCR 重裁决、章节调整或附件变化时，章节精读和主题融合进入 `STALE`。

- [ ] **步骤 7：运行测试并 Commit**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_source_flow.py \
  tests/test_workbench/test_ocr_citations.py tests/test_workbench/test_source_import.py \
  tests/test_workbench/test_task_package.py -q
git add src/parsing_core/workbench/source_import.py \
  src/parsing_core/workbench/chapter_detection.py src/parsing_core/workbench/task_package.py \
  src/parsing_core/workbench/repository.py src/parsing_core/serving/api/routes_ocr.py \
  tests/test_workbench/test_ocr_source_flow.py tests/test_workbench/test_ocr_citations.py
git commit -m "feat(workbench): build chapters from adjudicated OCR blocks"
```

---

### 任务 9：实现 OCR API、设置页和进度工作台

**文件：**
- 修改：`src/parsing_core/serving/api/routes_ocr.py`
- 修改：`src/parsing_core/serving/api/router.py`
- 修改：`src/parsing_core/serving/models/api.py`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 修改：`parsing-core-app/src/api/workbenchTypes.ts`
- 创建：`parsing-core-app/src/components/workbench/OcrSettings.tsx`
- 创建：`parsing-core-app/src/components/workbench/OcrProgress.tsx`
- 修改：`parsing-core-app/src/components/workbench/Settings.tsx`
- 修改：`parsing-core-app/src/components/workbench/ImportTextbooks.tsx`
- 测试：`tests/test_workbench/test_ocr_api.py`
- 测试：`parsing-core-app/src/components/workbench/Ocr*.test.tsx`

- [ ] **步骤 1：编写真实 FastAPI 契约测试**

覆盖开始、暂停、恢复、状态、页面证据、调用统计、百度设置和测试连接。重复开始同一 source 返回 409，不创建第二 lease。

- [ ] **步骤 2：编写前端失败测试**

断言：

- 扫描 PDF 显示 OCR 需要页数。
- 可查看 direct、adjudicated、waiting 和 failed 数量。
- 余额不足显示百度设置入口和恢复按钮。
- Key 仅显示脱敏值。
- 页面证据能打开本地 PDF 对应页和区域。

- [ ] **步骤 3：运行测试确认失败**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_api.py -q
cd parsing-core-app && npm test -- --run Ocr
```

- [ ] **步骤 4：实现最小 API**

API：

- `POST /api/workbench/sources/{id}/ocr/start`
- `POST /api/workbench/sources/{id}/ocr/pause`
- `POST /api/workbench/sources/{id}/ocr/resume`
- `GET /api/workbench/sources/{id}/ocr/status`
- `GET /api/workbench/ocr/pages/{page_id}`
- `GET /api/workbench/ocr/pages/{page_id}/evidence`

- [ ] **步骤 5：实现前端状态和设置**

使用现有表单、状态条和错误组件。不要把 API Key 放入 Zustand 持久化。数值显示调用上限、已调用、缓存命中和重试次数。

- [ ] **步骤 6：运行测试、build 和 1024/1440 视觉检查**

```bash
.venv/bin/pytest tests/test_workbench/test_ocr_api.py -q
cd parsing-core-app && npm test -- --run && npm run build
```

使用 Playwright 检查两个视口无横向溢出、错误状态可操作、长文件名和大页数不遮挡。

- [ ] **步骤 7：Commit**

```bash
git add src/parsing_core/serving/api/routes_ocr.py \
  src/parsing_core/serving/api/router.py src/parsing_core/serving/models/api.py \
  parsing-core-app/src/api parsing-core-app/src/components/workbench/OcrSettings.tsx \
  parsing-core-app/src/components/workbench/OcrProgress.tsx \
  parsing-core-app/src/components/workbench/Settings.tsx \
  parsing-core-app/src/components/workbench/ImportTextbooks.tsx \
  tests/test_workbench/test_ocr_api.py
git commit -m "feat(app): expose unattended OCR workflow"
```

---

### 任务 10：打包、升级和 Release 门禁

**文件：**
- 修改：`parsing-core-app/scripts/prepare-sidecar-python.sh`
- 修改：`parsing-core-app/scripts/check-release-sidecar.sh`
- 修改：`parsing-core-app/scripts/test-release-sidecar.sh`
- 修改：`.github/workflows/release.yml`
- 修改：`README.md`
- 测试：`parsing-core-app/scripts/test-release-sidecar.sh`
- 测试：`tests/test_version_consistency.py`

- [ ] **步骤 1：编写 bundle 失败检查**

检查 App 内存在、架构正确且已签名的 Vision helper；内置 Python 能导入新增 OCR Python 模块；正式 bundle 不包含测试 Stub 环境变量或百度 Key。

- [ ] **步骤 2：运行检查确认失败**

```bash
bash parsing-core-app/scripts/check-release-sidecar.sh \
  parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app
```

- [ ] **步骤 3：更新构建和 Release workflow**

顺序：Swift helper 测试 -> Python/前端/Rust 测试 -> helper 编译 -> runtime 准备 -> Tauri build -> strict codesign -> 受限 PATH 冷启动 -> Vision fixture OCR -> 再验签 -> artifact/attestation -> Release。

- [ ] **步骤 4：验证升级不删除课程 OCR 数据**

安装旧版、创建 Application Support fixture、覆盖安装新版、冷启动，断言 SQLite 迁移成功且页面缓存仍存在。

- [ ] **步骤 5：运行本地完整打包**

```bash
cd parsing-core-app && npm run tauri build
cd .. && bash parsing-core-app/scripts/check-release-sidecar.sh \
  parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app
bash parsing-core-app/scripts/test-release-sidecar.sh \
  parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app
```

- [ ] **步骤 6：Commit**

```bash
git add parsing-core-app/scripts .github/workflows/release.yml README.md \
  tests/test_version_consistency.py
git commit -m "build(app): package native OCR runtime"
```

---

### 任务 11：真实教材阶段 A 自动验收

**文件：**
- 创建：`scripts/run-real-textbook-acceptance.py`
- 创建：`tests/fixtures/ocr/acceptance-schema.json`
- 创建：`docs/acceptance/ocr-stage-a/README.md`
- 生成：`docs/acceptance/ocr-stage-a/*.json`
- 生成：`docs/acceptance/ocr-stage-a/*.png`

- [ ] **步骤 1：实现不可变输入清单**

脚本接受两个绝对 PDF 路径，记录 SHA-256、页数和文件大小；路径和书名不发送给百度。若哈希变化，拒绝复用旧报告。

- [ ] **步骤 2：实现每本约 10 页的基准选择**

使用固定规则选择封面、目录、首章正文、公式、表格、中段和低对比页。选择结果写入 JSON，不能由运行时随机漂移。

- [ ] **步骤 3：运行基准并生成报告**

```bash
.venv/bin/python scripts/run-real-textbook-acceptance.py benchmark \
  --book-a '/Users/laoer/Documents/管理运筹学 (韩伯棠.pdf' \
  --book-b '/Users/laoer/Documents/数据、模型与决策  基于电子表格的建模和案例研究方法  原书第5版.PDF'
```

报告包含 direct rate、百度触发率、重试、冲突类别、耗时和缓存命中。不得声称人工字符准确率。

- [ ] **步骤 4：运行两本教材全量 OCR**

```bash
.venv/bin/python scripts/run-real-textbook-acceptance.py full-ocr --resume
```

预期：约 1158 页全部进入 direct 或 automated_adjudicated；WAITING_FOR_RESOURCE 可恢复；无页面静默跳过。

- [ ] **步骤 5：生成并校验章节目录**

目录页码、正文标题和 PDF 页面连续性自动交叉验证。最终章节快照包含两本教材各自的名称、顺序和页面边界。

- [ ] **步骤 6：真实运行对应章节精读**

选择两本书的线性规划对应章节，使用已配置 `deepseek-v4-pro` 和 Codex CLI。断言固定正文块、两张 Mermaid、review 通过、citation 均命中有效 OCR block。

- [ ] **步骤 7：真实运行跨教材融合**

生成“线性规划：从数学模型到电子表格管理决策”，验证理论、实现、案例、敏感性分析、边界、综合案例、两张 Mermaid 和卡片。

- [ ] **步骤 8：执行桌面故障矩阵**

覆盖关闭重启、断网、百度 429/余额不足、Codex TERM、DeepSeek 错误、重复提交、SQLite/文件发布故障。恢复后不得重复计费或覆盖已发布结果。

- [ ] **步骤 9：保存可审计证据并 Commit**

只提交报告、脱敏 JSON、截图和哈希；不得提交教材、页面原图、API Key、完整 OCR 教材正文或付费响应临时 URL。

```bash
git add scripts/run-real-textbook-acceptance.py tests/fixtures/ocr \
  docs/acceptance/ocr-stage-a
git commit -m "test(ocr): verify two real MBA textbooks"
```

---

### 任务 12：最终全量验证、对抗式审查和修正版发布

**文件：**
- 修改：根据审查发现精确限定
- 修改：`CHANGELOG.md` 或 Release notes 来源文件（若仓库已有）

- [ ] **步骤 1：运行全量质量门禁**

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
cd parsing-core-app && npm test -- --run && npm run build
cd src-tauri && cargo test && cargo check --all-targets
cd .. && npm run accept:task-12
```

预期：全部 PASS，无 skip 掩盖 release App 或 OCR helper 检查。

- [ ] **步骤 2：运行 OCR 安全与数据可靠性审查**

重点攻击：路径逃逸、图片/Base64 日志泄漏、Keychain 明文、百度无限重试、Codex session 污染、lease 并发、文件/DB 混合版本、citation 伪造、测试 executor 进入正式 bundle。

- [ ] **步骤 3：运行真实视觉验收**

在 `/Applications/PDF2MD.app` 上验证 1024x768 和 1440x900：导入、OCR 进度、等待资源、页面证据、章节目录、精读、融合、Mermaid 和卡片。截图必须来自真实后端和已安装 App。

- [ ] **步骤 4：修复全部 Critical/Important 并复审**

每个修复使用独立提交；复审直到没有 Critical/Important。Minor 记录但不阻断，除非影响用户数据或发布真实性。

- [ ] **步骤 5：构建并安装修正版 App**

```bash
cd parsing-core-app && npm run tauri build
codesign --verify --deep --strict src-tauri/target/release/bundle/macos/PDF2MD.app
```

备份当前 `/Applications/PDF2MD.app` 后安装，冷启动并验证 Vision、Codex、DeepSeek 和百度测试连接。

- [ ] **步骤 6：发布修正版**

版本号按语义化版本递增，不覆盖 `v0.1.2`。推送 `master` 和新 tag，等待 GitHub Actions 成功后再公开 Release；Release 上传 DMG、App ZIP、SHA-256 和 provenance，并明确 Apple Developer ID/notary 的外部状态。

- [ ] **步骤 7：最终结果说明**

报告本地 App 路径、Release URL、提交、测试数量、真实教材 OCR 页数、百度调用统计、代表章节和融合产物路径，以及所有未解决外部风险。

---

## 规格覆盖自检

| 规格要求 | 计划任务 |
|---|---|
| Apple Vision 本地 OCR | 2、3 |
| Codex CLI 首轮和终审 | 4、7 |
| 百度 PP-StructureV3 疑难页仲裁 | 5、6、7 |
| 5% 已确认页抽检 | 5、7 |
| 无人值守 lease、恢复和费用上限 | 6、7、9 |
| 原始观察、差异、裁决证据 | 1、7、9 |
| 表格、公式和坐标 | 2、4、5、6 |
| 章节快照与 citation | 8 |
| Keychain、隐私和日志脱敏 | 6、12 |
| 桌面 App 打包 | 2、10 |
| 两本真实教材约 1158 页 | 11 |
| DeepSeek V4 Pro 与 Codex 精读 | 11 |
| 跨教材融合、Mermaid、卡片 | 11 |
| 对抗式审查与修正版 Release | 12 |

