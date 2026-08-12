# M3 多引擎 OCR 与质量裁决实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 建立 Apple Vision、托管视觉模型、Codex CLI Vision 和百度 PP-StructureV3 的分级 OCR 闭环，在无人值守前提下保证页级完整、证据可追溯，无法确认时隔离而不猜测。

**架构：** 页面分析器先决定是否需要 OCR，再并行产生本地候选并计算结构化差异。策略引擎只对不确定页面升级云端 Provider，独立裁决器基于候选、裁剪图和规则生成最终块；仍低于门槛的页面进入 `QUARANTINED`，阻断相关章节但允许其他教材继续。

**技术栈：** Apple Vision Swift helper、Codex CLI、DeepSeek/托管视觉 API、百度 PP-StructureV3、Pydantic JSON Schema、asyncio、令牌桶、内容哈希缓存、pytest。

---

## 文件清单

**创建：**

- `src/parsing_core/workbench/domain/quality.py`：完整性、文本、数字、表格、公式和来源评分。
- `src/parsing_core/workbench/ports/ocr.py`：OCRCandidateProvider、PageAdjudicator、QualityGate。
- `src/parsing_core/workbench/ports/models.py`：结构化模型调用和 Capability。
- `src/parsing_core/workbench/application/services/page_analysis.py`：页面类型和风险识别。
- `src/parsing_core/workbench/application/services/ocr_policy.py`：分级升级决策。
- `src/parsing_core/workbench/application/services/ocr_service.py`：候选、裁决、隔离协调。
- `src/parsing_core/workbench/infrastructure/providers/apple_vision.py`：Swift helper 适配器。
- `src/parsing_core/workbench/infrastructure/providers/__init__.py`：Provider 适配器包。
- `src/parsing_core/workbench/infrastructure/providers/codex_cli.py`：可信 CLI 适配器。
- `src/parsing_core/workbench/infrastructure/providers/managed_vision.py`：托管视觉适配器。
- `src/parsing_core/workbench/infrastructure/providers/baidu_ocr.py`：百度 PP-StructureV3 适配器。
- `src/parsing_core/workbench/infrastructure/providers/rate_limit.py`：供应商级并发、退避和令牌桶。
- `src/parsing_core/workbench/infrastructure/persistence/sql/0004_ocr_quality.sql`：候选、裁决、指标和费用记录。
- `tests/test_workbench/domain/test_quality.py`：评分属性测试。
- `tests/test_workbench/application/test_page_analysis.py`、`test_ocr_policy.py`、`test_ocr_service.py`：策略测试。
- `tests/test_workbench/providers/test_apple_vision.py`、`test_codex_cli_provider.py`、`test_managed_vision.py`、`test_baidu_ocr_provider.py`：Provider 契约。
- `tests/test_workbench/integration/test_ocr_escalation.py`：全链路升级测试。
- `tests/benchmarks/ocr/evaluate.py`、`manifest.private.example.json`：金标准评估工具和私有清单格式。

**修改：**

- `src/parsing_core/workbench/ocr/vision.py`、`codex_vision.py`、`baidu.py`、`alignment.py`、`orchestrator.py`、`page_cache.py`：迁入统一端口并保留兼容门面。
- `src/parsing_core/workbench/application/pipelines/book.py`：接入 OCR 阶段。
- `parsing-core-app/src-tauri/vision-ocr/main.swift`：稳定 JSONL 契约和语言选项。
- `parsing-core-app/scripts/build-vision-ocr.sh`、`src-tauri/tests/test-vision-ocr.sh`：发布 helper 验证。
- `pyproject.toml`、`uv.lock`：HTTP、重试和指标依赖。

## 任务 1：定义质量维度与自动放行门槛

**文件：**
- 创建：`src/parsing_core/workbench/domain/quality.py`
- 创建：`tests/test_workbench/domain/test_quality.py`

- [ ] **步骤 1：编写失败的质量属性测试**

```python
from hypothesis import given, strategies as st

from parsing_core.workbench.domain.quality import QualityScores, decide_quality
from parsing_core.workbench.domain.states import QualityDisposition


@given(st.floats(min_value=0, max_value=1), st.floats(min_value=0, max_value=1))
def test_any_critical_dimension_below_threshold_cannot_be_accepted(text, numeric):
    scores = QualityScores(
        completeness=1.0, text=text, numeric=numeric, table=1.0,
        formula=1.0, source_mapping=1.0,
    )
    disposition = decide_quality(scores, has_conflict=False)
    if text < 0.998 or numeric < 0.999:
        assert disposition is not QualityDisposition.ACCEPTED
```

另测页缺失、来源缺失、表格/公式存在却无对应评分时必须隔离。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/domain/test_quality.py -q`

预期：FAIL，质量模块不存在。

- [ ] **步骤 3：实现纯领域评分**

`QualityScores` 验证每项在 `[0,1]`；`decide_quality` 只根据显式阈值和冲突标志返回 `ACCEPTED`、`ESCALATE` 或 `QUARANTINED`。初始自动放行要求完整性和来源映射为 1、正文不低于 0.998、关键数字/表格字段/公式符号不低于 0.999，且没有未解释冲突。

- [ ] **步骤 4：验证属性测试和变异目标**

运行：`uv run pytest tests/test_workbench/domain/test_quality.py -q`

预期：全部通过；阈值比较符号任一反转都会使测试失败。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/domain/quality.py tests/test_workbench/domain/test_quality.py
git commit -m "feat: define ocr quality gates"
```

## 任务 2：实现页面分析与分级策略

**文件：**
- 创建：`src/parsing_core/workbench/ports/ocr.py`
- 创建：`src/parsing_core/workbench/application/services/page_analysis.py`
- 创建：`src/parsing_core/workbench/application/services/ocr_policy.py`
- 创建：`tests/test_workbench/application/test_page_analysis.py`
- 创建：`tests/test_workbench/application/test_ocr_policy.py`

- [ ] **步骤 1：编写失败的分流表测试**

```python
@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (native_text_page(confidence=1.0), OcrRoute.NATIVE_ONLY),
        (scan_page(has_table=False), OcrRoute.APPLE_AND_VISION),
        (scan_page(has_table=True), OcrRoute.APPLE_AND_VISION),
        (conflicted_page(), OcrRoute.BAIDU_THEN_ADJUDICATE),
    ],
)
def test_ocr_route_is_deterministic(page, expected):
    assert choose_route(page) is expected
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_page_analysis.py tests/test_workbench/application/test_ocr_policy.py -q`

预期：FAIL，服务不存在。

- [ ] **步骤 3：实现页面特征和纯策略**

页面分析输出文本密度、乱码比例、图像占比、布局复杂度、表格/公式/脚注信号和语言；策略只消费这些特征、候选差异、Provider 可用性和隐私授权，不读取环境变量或发起网络调用。

- [ ] **步骤 4：验证策略矩阵**

运行：`uv run pytest tests/test_workbench/application/test_page_analysis.py tests/test_workbench/application/test_ocr_policy.py -q`

预期：全部通过，Provider 缺失时返回明确阻断原因，不能静默放行。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/ports/ocr.py src/parsing_core/workbench/application/services/page_analysis.py src/parsing_core/workbench/application/services/ocr_policy.py tests/test_workbench/application/test_page_analysis.py tests/test_workbench/application/test_ocr_policy.py
git commit -m "feat: route pages by ocr risk"
```

## 任务 3：封装 Apple Vision 并稳定 helper 契约

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/providers/apple_vision.py`
- 创建：`tests/test_workbench/providers/test_apple_vision.py`
- 修改：`parsing-core-app/src-tauri/vision-ocr/main.swift`
- 修改：`parsing-core-app/src-tauri/tests/test-vision-ocr.sh`
- 修改：`parsing-core-app/scripts/build-vision-ocr.sh`

- [ ] **步骤 1：编写失败的坐标和超时契约测试**

测试向假 helper 发送一页，断言请求包含图像 SHA-256、页号、语言和 300 DPI；响应必须包含逐行文本、置信度、归一化 bbox 和 helper 版本。损坏 JSON、超时和 SHA 不一致映射为不同错误码。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_workbench/providers/test_apple_vision.py -q
./parsing-core-app/src-tauri/tests/test-vision-ocr.sh
```

预期：Python 适配器不存在或 helper 响应缺少版本/哈希字段。

- [ ] **步骤 3：实现 JSONL Provider 契约**

每个请求一行 JSON，每个响应一行 JSON；Swift 使用 Vision `accurate` 模式、语言校正和候选置信度，输出 bbox 坐标统一为页面左上原点。Python 端限制单行 16 MiB、60 秒超时，并在退出时终止子进程组。

- [ ] **步骤 4：验证真实 helper 和发布脚本**

运行：

```bash
./parsing-core-app/scripts/build-vision-ocr.sh
./parsing-core-app/src-tauri/tests/test-vision-ocr.sh
uv run pytest tests/test_workbench/providers/test_apple_vision.py -q
```

预期：双语 fixture 通过，坐标与哈希一致。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/providers/apple_vision.py tests/test_workbench/providers/test_apple_vision.py parsing-core-app/src-tauri/vision-ocr/main.swift parsing-core-app/src-tauri/tests/test-vision-ocr.sh parsing-core-app/scripts/build-vision-ocr.sh
git commit -m "feat: adapt apple vision ocr"
```

## 任务 4：修复 Codex CLI 可信执行并接入统一 Provider

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/providers/codex_cli.py`
- 创建：`src/parsing_core/workbench/infrastructure/providers/__init__.py`
- 创建：`tests/test_workbench/providers/test_codex_cli_provider.py`
- 修改：`src/parsing_core/workbench/ocr/codex_vision.py`
- 修改：`tests/test_workbench/test_ocr_codex.py`

- [ ] **步骤 1：编写失败的安全符号链接测试**

```python
def test_homebrew_symlink_is_resolved_then_verified(tmp_path, trusted_codex_target):
    link = tmp_path / "codex"
    link.symlink_to(trusted_codex_target)
    provider = CodexCliProvider.probe(link)
    assert provider.executable == trusted_codex_target.resolve()
    assert provider.version.startswith("codex-cli ")


def test_world_writable_codex_target_is_rejected(world_writable_executable):
    with pytest.raises(ProviderSecurityError, match="provider.executable_untrusted"):
        CodexCliProvider.probe(world_writable_executable)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/providers/test_codex_cli_provider.py tests/test_workbench/test_ocr_codex.py -q`

预期：Homebrew 符号链接被现有验证拒绝。

- [ ] **步骤 3：实施解析后验证和指纹锁定**

先对用户选择路径执行 `resolve(strict=True)`，再对最终常规文件验证 owner 为当前用户或 root、group/other 不可写、路径不在临时目录；读取版本和 SHA-256 后保存指纹。每次调用前重读 inode、mtime、size 和 SHA；变化即要求重新授权。能力探测必须证明 CLI 支持 `--image`、`--output-schema`、`--ephemeral`、`--ignore-user-config`、`--ignore-rules`、`--strict-config` 和只读 sandbox。实际命令使用参数数组、禁用 shell、隔离临时工作目录、清理环境并限制输出：

```python
args = [
    executable,
    "exec",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--strict-config",
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "--output-schema",
    str(schema_path),
    "--image",
    str(explicit_page_image),
]
```

隔离目录只包含显式页图、裁剪图和输出 Schema，不挂载课程工作区；若当前 CLI 版本缺少任一安全参数，Provider 探测失败而不是删减约束运行。

- [ ] **步骤 4：验证真实 Codex 探测和替换攻击**

运行：

```bash
uv run pytest tests/test_workbench/providers/test_codex_cli_provider.py tests/test_workbench/test_ocr_codex.py -q
CODEX_CLI_PATH=/opt/homebrew/bin/codex uv run pytest tests/test_workbench/providers/test_codex_cli_provider.py -m local_cli -q
```

预期：测试替身和已登录真实 CLI 探测通过；调用间替换文件被拒绝。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/providers/codex_cli.py src/parsing_core/workbench/ocr/codex_vision.py tests/test_workbench/providers/test_codex_cli_provider.py tests/test_workbench/test_ocr_codex.py
git commit -m "security: trust verified codex cli targets"
```

## 任务 5：实现托管视觉与百度 PP-StructureV3 Provider

**文件：**
- 创建：`src/parsing_core/workbench/ports/models.py`
- 创建：`src/parsing_core/workbench/infrastructure/providers/managed_vision.py`
- 创建：`src/parsing_core/workbench/infrastructure/providers/baidu_ocr.py`
- 创建：`src/parsing_core/workbench/infrastructure/providers/rate_limit.py`
- 创建：`tests/test_workbench/providers/test_managed_vision.py`
- 创建：`tests/test_workbench/providers/test_baidu_ocr_provider.py`
- 修改：`src/parsing_core/workbench/ocr/baidu.py`

- [ ] **步骤 1：编写失败的 Provider 契约测试**

两类 Provider 共用契约：认证头不出现在日志；429 遵循 `Retry-After`；超时可重试；4xx 认证错误不可重试；响应必须匹配 Pydantic Schema；`provider_call_id` 相同不再次发送。

```python
async def test_baidu_upload_contains_only_requested_page(http, provider, page):
    await provider.recognize(page, call_id="call-1")
    request = http.requests[0]
    assert request.json["page_number"] == page.number
    assert request.json["image_sha256"] == page.image_sha256
    assert "source_path" not in request.text
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/providers/test_managed_vision.py tests/test_workbench/providers/test_baidu_ocr_provider.py -q`

预期：FAIL，统一 Provider 不存在。

- [ ] **步骤 3：实现结构化 HTTP 适配器**

托管视觉通过中国区网关接收短期任务令牌；百度适配器仅发送指定页或至多 4 个冲突裁剪，支持 access token 缓存、PP-StructureV3 表格/版面响应和删除回执。`RateLimiter` 分 Provider 限制并发、每分钟请求和重试预算，随机抖动退避有最大等待时间。

- [ ] **步骤 4：验证网络故障矩阵**

运行：`uv run pytest tests/test_workbench/providers -q`

预期：成功、认证、429、5xx、断流、超时、Schema 错误和重复 call ID 全部通过。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/ports/models.py src/parsing_core/workbench/infrastructure/providers src/parsing_core/workbench/ocr/baidu.py tests/test_workbench/providers
git commit -m "feat: add managed and baidu ocr providers"
```

## 任务 6：实现候选比对、独立裁决和隔离

**文件：**
- 创建：`src/parsing_core/workbench/application/services/ocr_service.py`
- 创建：`tests/test_workbench/application/test_ocr_service.py`
- 创建：`tests/test_workbench/integration/test_ocr_escalation.py`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/sql/0004_ocr_quality.sql`
- 修改：`src/parsing_core/workbench/ocr/alignment.py`
- 修改：`src/parsing_core/workbench/ocr/orchestrator.py`

- [ ] **步骤 1：编写失败的升级与隔离测试**

```python
async def test_unresolved_page_is_quarantined_and_blocks_only_related_chapter(harness):
    harness.apple.returns(conflicted_candidate())
    harness.vision.returns(conflicted_candidate())
    harness.baidu.returns(conflicted_candidate())
    harness.adjudicator.returns(low_confidence_decision())
    result = await harness.service.process(page_12())
    assert result.disposition is QualityDisposition.QUARANTINED
    assert result.final_blocks == ()
    assert result.reason_codes == ("ocr.unresolved_numeric_conflict",)
```

另测一致候选不调用百度、百度后可确认则只调用一次裁决、其他页继续完成。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_ocr_service.py -q`

预期：FAIL，服务不存在。

- [ ] **步骤 3：实现可解释裁决流程**

先按行、块、表格单元格和公式 token 对齐；差异对象保存候选值、来源 Provider、bbox 和风险类型。独立裁决器不能看到某候选的商业优先级，只看到带匿名标签的候选和原图裁剪；输出最终块、逐项理由、置信度和使用证据。低于门槛不写入 Canonical 已验证层。

- [ ] **步骤 4：验证策略、持久记录和章节阻断**

运行：

```bash
uv run pytest tests/test_workbench/application/test_ocr_service.py tests/test_workbench/integration/test_ocr_escalation.py -q
```

预期：全部通过；质量发现、ProviderCall 和裁决证据均可按页查询。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application/services/ocr_service.py src/parsing_core/workbench/infrastructure/persistence/sql/0004_ocr_quality.sql src/parsing_core/workbench/ocr/alignment.py src/parsing_core/workbench/ocr/orchestrator.py tests/test_workbench/application/test_ocr_service.py tests/test_workbench/integration/test_ocr_escalation.py
git commit -m "feat: adjudicate and quarantine ocr pages"
```

## 任务 7：接入有界并发、背压、缓存和费用上限

**文件：**
- 修改：`src/parsing_core/workbench/application/pipelines/book.py`
- 修改：`src/parsing_core/workbench/ocr/page_cache.py`
- 修改：`src/parsing_core/workbench/infrastructure/jobs/worker.py`
- 创建：`tests/test_workbench/integration/test_ocr_concurrency.py`

- [ ] **步骤 1：编写失败的资源上限测试**

测试 100 个合成页，断言 PDF 渲染并发不超过 CPU 配额、Apple 队列不超过 2、每个云 Provider 不超过配置值、待处理大图队列不超过 8；取消后无遗留子进程；第二次执行至少 80% 步骤命中缓存。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/integration/test_ocr_concurrency.py -q`

预期：现有串行或无界调用不满足并发和队列断言。

- [ ] **步骤 3：实现流式生产和背压**

渲染使用受限进程池，结果进入容量 8 的异步队列；候选按 Provider semaphore 和令牌桶执行；完成结果可乱序计算但只按页号提交。缓存键包括页图哈希、Provider 身份、模型版本、提示词版本和 Schema 版本；费用上限触发 `PAUSED_BY_QUOTA`。

- [ ] **步骤 4：验证并发、取消和缓存**

运行：`uv run pytest tests/test_workbench/integration/test_ocr_concurrency.py -q --count=10`

预期：重复 10 次无竞态，缓存复用率不低于 80%，Provider 调用不超预算。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application/pipelines/book.py src/parsing_core/workbench/ocr/page_cache.py src/parsing_core/workbench/infrastructure/jobs/worker.py tests/test_workbench/integration/test_ocr_concurrency.py
git commit -m "perf: bound ocr concurrency and cost"
```

## 任务 8：建立真实教材金标准与无人值守验收

**文件：**
- 创建：`tests/benchmarks/ocr/evaluate.py`
- 创建：`tests/benchmarks/ocr/manifest.private.example.json`
- 创建：`tests/benchmarks/ocr/test_metrics.py`
- 创建：`scripts/run-private-ocr-benchmark.sh`
- 修改：`.gitignore`

- [ ] **步骤 1：编写失败的指标计算测试**

固定小型 fixture 断言 CER、页级完整率、数字准确率、表格字段准确率、公式符号准确率、自动放行误接受率和来源映射率计算结果。评估器输入只接收清单、预期 JSON 和运行 Artifact，不读取任意目录。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/benchmarks/ocr/test_metrics.py -q`

预期：FAIL，评估器不存在。

- [ ] **步骤 3：实现脱敏评估器和私有运行脚本**

脚本要求 `PDF2MD_PRIVATE_BENCHMARK_MANIFEST`，逐书运行完整 OCR，输出仅含哈希、页号和指标的 JSON。`.gitignore` 忽略 `tests/benchmarks/ocr/private/` 和结果正文；失败页图留在私有 Runner 本地。

- [ ] **步骤 4：执行 M3 验收**

运行：

```bash
uv run pytest tests/test_workbench/domain/test_quality.py tests/test_workbench/application/test_ocr_policy.py tests/test_workbench/application/test_ocr_service.py tests/test_workbench/providers tests/test_workbench/integration/test_ocr_escalation.py tests/test_workbench/integration/test_ocr_concurrency.py tests/benchmarks/ocr/test_metrics.py -q
PDF2MD_PRIVATE_BENCHMARK_MANIFEST=/secure/pdf2md/manifest.json ./scripts/run-private-ocr-benchmark.sh
```

预期：页级完整率和来源映射率 100%，正文 CER 不高于 0.2%，关键数字/表格/公式不低于 99.9%，自动放行误接受率低于 0.1%；500 页扫描教材健康网络端到端不超过 120 分钟，处理峰值不超过 2 GB；两本教材无需逐页操作。

- [ ] **步骤 5：Commit**

```bash
git add .gitignore tests/benchmarks/ocr scripts/run-private-ocr-benchmark.sh
git commit -m "test: gate ocr against private textbooks"
```

## M3 完成收据

- [ ] 本地、托管、Codex CLI 和百度 Provider 均通过相同契约与故障矩阵。
- [ ] Codex CLI 符号链接可安全配置，目标替换攻击会被拒绝。
- [ ] 不确定页面升级、裁决、隔离和章节阻断均有可追溯记录。
- [ ] 重跑复用至少 80% 已验证步骤，不重复扣费。
- [ ] 两本真实教材达到规格中的完整率、准确率和无人值守要求。
