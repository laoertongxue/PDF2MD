# P0-A 百度页级隔离与继续复核（设计 5.4）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 无百度 OCR Key 时不再整本阻断：冲突/复杂/抽检页隔离为 `REVIEW_PENDING`、批次以 `REVIEW_REQUIRED` 收尾并带清单发布；配置 Key 后可只重跑隔离页并升级为 `COMPLETED`。

**架构：** 在 `orchestrator.py` 增加页级终态 `REVIEW_PENDING` 与批次终态 `REVIEW_REQUIRED`（state/final schema v3，含 `review_pages` 清单与 `_review_final_is_valid` 门禁）；`workflow.py` 新增 `WorkflowStatus.REVIEW_REQUIRED`、复核 final 校验与 `start_review()`；`chapters.py`/`markdown_notes.py` 允许隔离页以空证据参与章节检测/笔记生成并写 `<!-- pdf2md: review pending page N -->` 占位与顶部 `<!-- pdf2md: review_pending=N -->`；API factory 在无 Key 时传 `baidu=None`，新增 `POST /ocr/review`；前端展示待复核清单并触发复核。

**技术栈：** Python 3.12 + FastAPI + Pydantic v2 + pytest；React 18 + TypeScript + Vitest + Testing Library。

**上游依据：** `docs/superpowers/specs/2026-09-12-commercial-p0-a-onboarding-design.md` 5.4、6、7 节；上一计划 `docs/superpowers/plans/2026-09-12-commercial-p0-a-config-and-errors.md` 任务 3 已完成 factory 的设置接线。

**勘察更正（相对任务描述）：** `needs_baidu(...)` 为真且百度引擎不可用的分支位于 `orchestrator.py` 的 `OcrOrchestrator._run_page`（第 533-548 行），不是 `_adjudicate`；本计划以真实位置为准。`_completed_ocr_final_is_valid` 实际位于 `workflow.py:473-506`；`_safe_error` 位于 `workflow.py:6227-6235`。

**取舍说明（REVIEW_PENDING  vs 复用 BAIDU_PENDING）：** `BAIDU_PENDING` 是“引擎可用、正在升级”的中间态，恢复时会被继续执行；若复用它表示“缺少引擎、等待人工复核”，现有完成校验与恢复路径会把待复核页误当作可重试的进行中页。因此保留 `BAIDU_PENDING` 不变，新增终态 `REVIEW_PENDING`，并让 `_completed_evidence_is_valid` 显式拒绝它，保证“待复核 ≠ 通过”的硬不变量。

**发布物解释：** 工作台的“合并 Markdown”是章节笔记 `intensive-reading.md`（由 `build_intensive_reading_note` / DeepSeek 生成后经 `bind_published_note` 发布）。本计划在该笔记 markdown 顶部写全局计数注释、在该章节页范围内的隔离页位置写占位注释；位于所有已确认章节范围之外的隔离页只计入顶部计数与回执，这是按章节发布的既定边界。

**约定：**
- 后端测试：`.venv/bin/python -m pytest <path> -q`
- 后端 lint/类型：`.venv/bin/ruff check <files> && .venv/bin/mypy src/parsing_core`
- 前端测试：`npm test --prefix parsing-core-app -- --run <file>`
- 前端 lint/类型：`npm --prefix parsing-core-app run lint && npm --prefix parsing-core-app run typecheck`
- 每个任务结束提交一次；提交信息使用仓库既有 Conventional Commits 英文风格。
- 所有新增代码遵循现有模式；不要重构无关代码。

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `src/parsing_core/workbench/ocr/orchestrator.py` | 修改 | 新增 `PageStatus.REVIEW_PENDING`/`BatchStatus.REVIEW_REQUIRED`；`_review_reason`；`_run_page` 无百度时隔离；批次循环跳过/重置/失败回退隔离；schema v3 + `review_pages` + `_review_final_is_valid` + `_review_final_for_request` |
| `src/parsing_core/workbench/ocr/workflow.py` | 修改 | `WorkflowStatus.REVIEW_REQUIRED`；`_read_ocr_final`/`_review_ocr_final_is_valid`；status payload 增加 `review_pages`/`review_pending`；`completed_evidence` 接受复核 final；`start_review()`；发布 metadata/markdown 校验记录 `review_pending` |
| `src/parsing_core/workbench/ocr/chapters.py` | 修改 | `_extract_page` 接受 `review_pending` 页为空证据，保持章节页码连续 |
| `src/parsing_core/workbench/ocr/markdown_notes.py` | 修改 | review 占位符、顶部计数、metadata `review_pending`/`review_pages`、markdown 校验放行注释 |
| `src/parsing_core/workbench/ocr/schemas/intensive-reading-note.json` | 修改 | metadata 增加 `review_pending`（int≥0）与 `review_pages`（唯一正整数数组） |
| `src/parsing_core/workbench/ocr/deepseek_intensive_reading.py` | 修改 | 放行并强制 review metadata 字段，保证生成阶段不丢计数 |
| `src/parsing_core/serving/api/routes_workbench.py` | 修改 | factory 无 Key 时 `baidu=None`；新增 `POST /sources/{source_id}/ocr/review`；generate 路由透传 `review_pending` |
| `tests/test_workbench/test_ocr_orchestrator.py` | 修改 | 页级隔离、恢复不重跑/不耗 attempts、有 Key 升级、失败回退隔离、final 校验、v2 兼容 |
| `tests/test_workbench/test_ocr_workflow.py` | 修改 | 工作流状态/清单/`start_review`/章节检测/发布回执；新增混合批次端到端夹具与用例 |
| `tests/test_workbench/test_ocr_markdown_notes.py` | 修改 | 占位符、顶部计数、全隔离页拒绝、markdown/占位缺失拒绝 |
| `tests/test_workbench/test_deepseek_intensive_reading.py` | 修改 | 生成阶段保留 review 占位与计数 |
| `tests/test_workbench/test_api.py` | 修改 | factory 无 Key 不再抛 `baidu_key_missing`；`/ocr/review` 前置 409 与成功路径；旧断言迁移；API 级复核升级端到端 |
| `parsing-core-app/src/api/workbenchTypes.ts` | 修改 | `OcrWorkflowStatus` 增加 `review_required`；新增 `OcrReviewPage`；`OcrStatus` 增加 `review_pages`/`review_pending` |
| `parsing-core-app/src/api/workbench.ts` | 修改 | 解析并校验新字段；新增 `reviewSourceOcr()` |
| `parsing-core-app/src/api/ocrStatus.test.ts` | 修改 | mock 载荷补新字段；新增 `review_required` 解析用例 |
| `parsing-core-app/src/components/workbench/OcrWorkflowPanel.tsx` | 修改 | 待复核清单（页码/原因）与“配置百度 Key 并继续复核”按钮；状态徽标 |
| `parsing-core-app/src/components/workbench/OcrWorkflowPanel.test.tsx` | 创建 | 清单渲染、复核调用、409 跳转设置 |

**拆分评估：** 本任务不需要新模块。改动集中在既有职责边界内（单文件新增 ≤ 约 150 行），继续复用 `orchestrator`（状态机）、`workflow`（持久化/发布校验）、`markdown_notes`（笔记契约）、`routes_workbench`（HTTP）四层结构；为 review 单独建模块会造成 private 校验函数跨文件复制，反而增加不一致风险。

---

## 任务 1：编排器页级隔离与 REVIEW_REQUIRED final

**文件：**
- 修改：`src/parsing_core/workbench/ocr/orchestrator.py`（枚举 69-87、`run_batch` 294-440、`_run_page` 533-548、`_completed_final_for_request` 714-766、`_completed_evidence_is_valid` 773-893、`_is_batch_state` 1265-1358、`_is_page_state` 1361-1374）
- 测试：`tests/test_workbench/test_ocr_orchestrator.py`（文件末尾追加；顶部补 `import copy`）

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_workbench/test_ocr_orchestrator.py` 顶部把 `import hashlib` 之前的 import 区补一行（保持字母序放在 `import json` 之后的位置不重要，只要存在）：

```python
import copy
```

在文件末尾追加以下测试：

```python
def test_conflict_page_is_isolated_when_baidu_is_unavailable(tmp_path):
    engines = FakeEngines(codex_text="不同文本")
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None

    result = _run(orchestrator, engines)

    assert result.status is BatchStatus.REVIEW_REQUIRED
    assert result.pages[1].status is PageStatus.REVIEW_PENDING
    assert engines.calls == ["vision:1", "codex:1"]
    final = json.loads((tmp_path / "ocr-state" / "batch-final.json").read_text(encoding="utf-8"))
    assert final["schema_version"] == 3
    assert final["status"] == "review_required"
    assert final["review_pages"] == [
        {"page": 1, "reason": "conflict", "alignment_status": "conflict"}
    ]
    assert final["review_pending"] == 1
    assert final["pages"]["1"]["status"] == "review_pending"
    assert final["pages"]["1"]["review_reason"] == "conflict"
    assert final["pages"]["1"]["alignment_status"] == "conflict"
    assert "decision" not in final["pages"]["1"]


def test_isolated_page_is_not_rerun_or_charged_attempts_without_baidu(tmp_path):
    engines = FakeEngines(codex_text="不同文本")
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None
    first = _run(orchestrator, engines)
    assert first.status is BatchStatus.REVIEW_REQUIRED
    state_path = tmp_path / "ocr-state" / "batch-state.json"
    attempts_before = json.loads(state_path.read_text(encoding="utf-8"))["pages"]["1"]["attempts"]
    calls_before = list(engines.calls)

    second = _run(orchestrator, engines)

    assert second.status is BatchStatus.REVIEW_REQUIRED
    assert engines.calls == calls_before
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["pages"]["1"]["status"] == "review_pending"
    assert state["pages"]["1"]["attempts"] == attempts_before


def test_isolated_page_reruns_when_baidu_is_available(tmp_path):
    engines = FakeEngines(codex_text="不同文本")
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None
    assert _run(orchestrator, engines).status is BatchStatus.REVIEW_REQUIRED
    engines.calls.clear()

    resumed = _orchestrator(tmp_path, engines)

    result = _run(resumed, engines)

    assert result.status is BatchStatus.COMPLETED
    assert "baidu:1" in engines.calls
    final = json.loads((tmp_path / "ocr-state" / "batch-final.json").read_text(encoding="utf-8"))
    assert final["status"] == "completed"
    assert "review_pages" not in final
    assert "review_pending" not in final
    assert final["pages"]["1"]["attempts"] == 1


def test_failed_review_page_returns_to_review_pending(tmp_path):
    engines = FakeEngines(codex_text="不同文本")
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None
    assert _run(orchestrator, engines).status is BatchStatus.REVIEW_REQUIRED

    def blocked_adjudication(*args, **kwargs):
        engines.calls.append("adjudicate:1")
        return SimpleNamespace(payload={"status": "accepted"}, record={})

    engines.codex.adjudicate_page = blocked_adjudication
    resumed = _orchestrator(tmp_path, engines)

    result = _run(resumed, engines)

    assert result.status is BatchStatus.REVIEW_REQUIRED
    assert result.pages[1].status is PageStatus.REVIEW_PENDING
    final = json.loads((tmp_path / "ocr-state" / "batch-final.json").read_text(encoding="utf-8"))
    assert final["review_pages"] == [
        {"page": 1, "reason": "conflict", "alignment_status": "conflict"}
    ]


def test_review_final_validation_rejects_tampered_entries(tmp_path):
    engines = FakeEngines(codex_text="不同文本")
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None
    assert _run(orchestrator, engines).status is BatchStatus.REVIEW_REQUIRED
    final = json.loads((tmp_path / "ocr-state" / "batch-final.json").read_text(encoding="utf-8"))

    for mutate in (
        lambda value: value["review_pages"][0].update({"reason": "guessed"}),
        lambda value: value["pages"]["1"].update({"status": "completed"}),
        lambda value: value.update({"review_pending": 2}),
    ):
        tampered = copy.deepcopy(final)
        mutate(tampered)
        assert orchestrator._review_final_is_valid(tampered) is False


def test_legacy_schema_two_state_without_review_fields_still_loads(tmp_path):
    engines = FakeEngines()
    result = _run(_orchestrator(tmp_path, engines), engines)
    assert result.status is BatchStatus.COMPLETED
    state_path = tmp_path / "ocr-state" / "batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["schema_version"] = 2
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o600)

    loaded = orchestrator_module._read_batch_state(state_path)

    assert loaded["schema_version"] == 2
```

同时更新既有断言：把 `test_batch_state_persists_exact_versioned_run_configuration`（约 503-523 行）中的

```python
    assert state["schema_version"] == 2
```

改为

```python
    assert state["schema_version"] == 3
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_orchestrator.py -q -k "isolated or review_final or legacy_schema_two or exact_versioned"`

预期：FAIL（`AttributeError: REVIEW_REQUIRED`、`PageStatus` 无 `REVIEW_PENDING`）。

- [ ] **步骤 3：实现状态、隔离原因与 `_run_page` 隔离**

在 `src/parsing_core/workbench/ocr/orchestrator.py` 中做以下修改。

（a）把 `.alignment` 导入补上 `AlignmentDecision`：

```python
from .alignment import (
    AlignmentDecision,
    authorize_baidu_escalation,
    classify_page,
    compare_observations,
    needs_baidu,
    primary_block_uncertainty_reason,
)
```

（b）把 `_BATCH_STATE_SCHEMA_VERSION = 2` 改为：

```python
_BATCH_STATE_SCHEMA_VERSION = 3
_LEGACY_BATCH_STATE_SCHEMA_VERSIONS = frozenset({2})
REVIEW_REASONS = frozenset({"conflict", "complex", "sampled"})
```

（c）替换两个枚举（现 69-87 行）为：

```python
class BatchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class PageStatus(StrEnum):
    PENDING = "pending"
    RENDERING = "rendering"
    PRIMARY_OCR = "primary_ocr"
    DIFFING = "diffing"
    BAIDU_PENDING = "baidu_pending"
    REVIEW_PENDING = "review_pending"
    ADJUDICATING = "adjudicating"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


def _review_reason(status: str) -> str:
    if status == AlignmentDecision.CONFLICT.value:
        return "conflict"
    if status == AlignmentDecision.COMPLEX.value:
        return "complex"
    return "sampled"
```

（d）`_PageState`（现 197-207 行）增加两个字段：

```python
class _PageState(TypedDict, total=False):
    status: str
    error: str
    attempts: int
    vision: _JsonObject
    codex: _JsonObject
    alignment: _JsonObject
    baidu: _JsonObject
    decision: _JsonObject
    page_input_fingerprint: str
    evidence_fingerprint: str
    review_reason: str
    alignment_status: str
```

（e）`_BatchState`（现 210-218 行）增加两个字段：

```python
class _BatchState(TypedDict):
    schema_version: int
    status: str
    input_fingerprint: str
    pdf_snapshot: _PdfSnapshot
    run_config: _RunConfig
    pages: dict[str, _PageState]
    updated_at: int
    error: NotRequired[str | None]
    review_pages: NotRequired[list[_JsonObject]]
    review_pending: NotRequired[int]
```

（f）把 `_run_page` 中 `needs_baidu(...)` 的分支开头（现 533-538 行）替换为：

```python
        if needs_baidu(page_hash, page, status, sample_rate=sample_rate):
            if "baidu" not in current:
                self._check_deadline(deadline)
                if self.baidu is None:
                    current["status"] = PageStatus.REVIEW_PENDING.value
                    current["review_reason"] = _review_reason(status)
                    current["alignment_status"] = status
                    current.pop("error", None)
                    self._persist(state)
                    return
                current["status"] = PageStatus.BAIDU_PENDING.value
                self._persist(state)
                authorization = authorize_baidu_escalation(
                    page_hash,
                    page,
                    status,
                    input_fingerprint=input_fingerprint,
                    sample_rate=sample_rate,
                )
```

其余 `authorization is None`、`self.baidu is None` 检查（原 545-548 行）保持原样删除 `self.baidu is None` 那个 `raise`，因为分支已在上方处理：

```python
                if authorization is None:
                    raise ValueError("Baidu escalation authorization is missing")
                image = self._call_engine(self.image_loader, image_path, deadline=deadline)
```

- [ ] **步骤 4：实现批次循环、final 发布与校验**

（a）`run_batch` 开头（现 294-322 行）替换为：

```python
        try:
            completed = self._completed_final_for_request(
                pdf_path,
                page_numbers,
                dpi,
                languages,
                sample_rate,
                deadline=deadline,
            )
            if completed is not None:
                return BatchRun(
                    BatchStatus.COMPLETED,
                    self._page_runs(completed["pages"]),
                )
            if self.baidu is None:
                review = self._review_final_for_request(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
                if review is not None:
                    return BatchRun(
                        BatchStatus.REVIEW_REQUIRED,
                        self._page_runs(review["pages"]),
                    )
            with self._publication_transaction(deadline=deadline):
                completed = self._completed_final_for_request(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
                if completed is not None:
                    return BatchRun(
                        BatchStatus.COMPLETED,
                        self._page_runs(completed["pages"]),
                    )
                if self.baidu is None:
                    review = self._review_final_for_request(
                        pdf_path,
                        page_numbers,
                        dpi,
                        languages,
                        sample_rate,
                        deadline=deadline,
                    )
                    if review is not None:
                        return BatchRun(
                            BatchStatus.REVIEW_REQUIRED,
                            self._page_runs(review["pages"]),
                        )
                self._discard_final_artifact()
                self._check_control(deadline)
                state = self._load_or_create_state(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
```

（b）把页面循环（现 350-368 行）替换为：

```python
            for page in page_numbers:
                current = page_state[str(page)]
                self._check_control(deadline)
                resumed_review = current.get("status") == PageStatus.REVIEW_PENDING.value
                prior_reason: object = None
                prior_alignment: object = None
                if resumed_review:
                    if self.baidu is None:
                        continue
                    prior_reason = current.get("review_reason")
                    prior_alignment = current.get("alignment_status")
                    self._reset_page(current)
                elif current.get("status") == PageStatus.COMPLETED.value:
                    valid = self._completed_evidence_is_valid(state, current, page, sample_rate)
                    self._check_control(deadline)
                    if valid:
                        continue
                    self._reset_page(current)
                if int(current.get("attempts", 0)) >= self.max_page_attempts:
                    return self._finish(
                        state, BatchStatus.FAILED, "ocr_retry_limit_reached", page_runs
                    )
                current["attempts"] = int(current.get("attempts", 0)) + 1
                self._check_control(deadline)
                try:
                    self._run_page(
                        state, current, pdf_path, page, dpi, languages, sample_rate, deadline
                    )
                except (_BatchCancelled, AtomicCommitError, _StateInvalid, TimeoutError):
                    raise
                except Exception:
                    if not resumed_review:
                        raise
                    if isinstance(prior_reason, str):
                        current["review_reason"] = prior_reason
                    if isinstance(prior_alignment, str):
                        current["alignment_status"] = prior_alignment
                    current["status"] = PageStatus.REVIEW_PENDING.value
                    current.pop("error", None)
                    self._persist(state)
                self._check_control(deadline)
```

（c）把循环结束后的完成判定与发布块（现 370-398 行）替换为：

```python
            self._check_control(deadline)
            if any(
                page_state[str(page)].get("status")
                not in {PageStatus.COMPLETED.value, PageStatus.REVIEW_PENDING.value}
                for page in page_numbers
            ):
                return self._finish(state, BatchStatus.BLOCKED, "ocr_batch_incomplete", page_runs)
            review_pages: list[_JsonObject] = []
            for page in page_numbers:
                record = page_state[str(page)]
                if record.get("status") != PageStatus.REVIEW_PENDING.value:
                    continue
                reason = record.get("review_reason")
                alignment_status = record.get("alignment_status")
                if not isinstance(reason, str) or reason not in REVIEW_REASONS:
                    raise _StateInvalid("review reason is invalid")
                if not isinstance(alignment_status, str) or not alignment_status:
                    raise _StateInvalid("review alignment status is invalid")
                review_pages.append(
                    {
                        "page": page,
                        "reason": reason,
                        "alignment_status": alignment_status,
                    }
                )
            self._check_control(deadline)
            with self._publication_transaction(deadline=deadline):
                completed = self._completed_final_for_request(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
                if completed is not None:
                    return BatchRun(
                        BatchStatus.COMPLETED,
                        self._page_runs(completed["pages"]),
                    )
                if self._completed_transaction_exists_unlocked():
                    raise _StateInvalid("completed OCR publication changed")
                self._check_control(deadline)
                state["error"] = None
                if review_pages:
                    state["review_pages"] = review_pages
                    state["review_pending"] = len(review_pages)
                    self._set_status(state, BatchStatus.REVIEW_REQUIRED)
                    self._check_control(deadline)
                    if not self._review_final_is_valid(state):
                        raise _StateInvalid("review OCR publication is invalid")
                    self._publish_atomically_unlocked(state, deadline=deadline)
                    return BatchRun(BatchStatus.REVIEW_REQUIRED, self._page_runs(page_state))
                state.pop("review_pages", None)
                state.pop("review_pending", None)
                self._set_status(state, BatchStatus.COMPLETED)
                self._check_control(deadline)
                self._publish_atomically_unlocked(state, deadline=deadline)
            return BatchRun(BatchStatus.COMPLETED, self._page_runs(page_state))
```

（d）在 `_completed_final_for_request`（现 714-766 行）之后、`_reset_page` 之前插入两个方法：

```python
    def _review_final_for_request(
        self,
        pdf_path: str | Path,
        pages: Sequence[int],
        dpi: int,
        languages: Sequence[str],
        sample_rate: float,
        *,
        deadline: float | None,
    ) -> _BatchState | None:
        try:
            state = _read_batch_state(
                self.state_root / "batch-state.json",
                deadline=deadline,
            )
            final = _read_batch_state(
                self.state_root / "batch-final.json",
                deadline=deadline,
            )
        except (FileNotFoundError, _StateInvalid):
            return None

        snapshot = _snapshot_pdf(pdf_path, deadline=deadline)
        run_config: _RunConfig = {
            "pages": list(pages),
            "dpi": dpi,
            "languages": list(languages),
            "sample_rate": sample_rate,
        }
        fingerprint = _fingerprint(
            {
                "pdf_snapshot": snapshot,
                "pages": list(pages),
                "dpi": dpi,
                "languages": list(languages),
                "sample_rate": sample_rate,
            }
        )
        if (
            state != final
            or final["status"] != BatchStatus.REVIEW_REQUIRED.value
            or final["pdf_snapshot"] != snapshot
            or final["run_config"] != run_config
            or final["input_fingerprint"] != fingerprint
        ):
            return None
        if not self._review_final_is_valid(final):
            return None
        return final

    def _review_final_is_valid(self, state: object) -> bool:
        if not _is_batch_state(state):
            return False
        if state.get("status") != BatchStatus.REVIEW_REQUIRED.value:
            return False
        review_pages = state.get("review_pages")
        review_pending = state.get("review_pending")
        if not isinstance(review_pages, list) or not review_pages:
            return False
        if (
            not isinstance(review_pending, int)
            or isinstance(review_pending, bool)
            or review_pending != len(review_pages)
        ):
            return False
        pages = state["run_config"]["pages"]
        sample_rate = state["run_config"]["sample_rate"]
        expected: dict[int, _JsonObject] = {}
        for entry in review_pages:
            if not isinstance(entry, dict) or set(entry) != {
                "page",
                "reason",
                "alignment_status",
            }:
                return False
            page = entry.get("page")
            reason = entry.get("reason")
            if (
                not isinstance(page, int)
                or isinstance(page, bool)
                or page not in pages
                or page in expected
            ):
                return False
            if not isinstance(reason, str) or reason not in REVIEW_REASONS:
                return False
            expected[page] = entry
        try:
            for page in pages:
                record = state["pages"][str(page)]
                if page in expected:
                    entry = expected[page]
                    if record.get("status") != PageStatus.REVIEW_PENDING.value:
                        return False
                    if record.get("review_reason") != entry["reason"]:
                        return False
                    if record.get("alignment_status") != entry["alignment_status"]:
                        return False
                    continue
                if record.get("status") != PageStatus.COMPLETED.value:
                    return False
                if not self._completed_evidence_is_valid(state, record, page, sample_rate):
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        return True
```

（e）`_completed_evidence_is_valid` 开头（现 773-781 行）在 `_is_batch_state` 判断后加一行：

```python
        if not _is_batch_state(state) or not _is_page_state(current):
            return False
        if current.get("status") == PageStatus.REVIEW_PENDING.value:
            return False
```

（f）`_is_batch_state`（现 1265-1358 行）整体替换为：

```python
def _is_batch_state(value: object) -> TypeGuard[_BatchState]:
    if not isinstance(value, dict):
        return False
    allowed_fields = {
        "schema_version",
        "status",
        "input_fingerprint",
        "pdf_snapshot",
        "run_config",
        "pages",
        "updated_at",
        "error",
        "review_pages",
        "review_pending",
    }
    schema_version = value.get("schema_version")
    if (
        set(value) - allowed_fields
        or schema_version
        not in ({_BATCH_STATE_SCHEMA_VERSION} | _LEGACY_BATCH_STATE_SCHEMA_VERSIONS)
    ):
        return False
    if value.get("status") not in {status.value for status in BatchStatus}:
        return False
    input_fingerprint = value.get("input_fingerprint")
    if (
        not isinstance(input_fingerprint, str)
        or len(input_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in input_fingerprint)
    ):
        return False
    if not isinstance(value.get("updated_at"), int):
        return False
    snapshot = value.get("pdf_snapshot")
    if not isinstance(snapshot, dict):
        return False
    if set(snapshot) != {"sha256", "size"}:
        return False
    if (
        not isinstance(snapshot.get("sha256"), str)
        or len(snapshot["sha256"]) != 64
        or not isinstance(snapshot.get("size"), int)
        or isinstance(snapshot["size"], bool)
        or snapshot["size"] <= 0
    ):
        return False
    run_config = value.get("run_config")
    if not isinstance(run_config, dict) or set(run_config) != {
        "pages",
        "dpi",
        "languages",
        "sample_rate",
    }:
        return False
    configured_pages = run_config.get("pages")
    dpi = run_config.get("dpi")
    languages = run_config.get("languages")
    sample_rate = run_config.get("sample_rate")
    if (
        not isinstance(configured_pages, list)
        or not configured_pages
        or any(
            not isinstance(page, int) or isinstance(page, bool) or page <= 0
            for page in configured_pages
        )
        or len(set(configured_pages)) != len(configured_pages)
        or not isinstance(dpi, int)
        or isinstance(dpi, bool)
        or not 72 <= dpi <= 1200
        or not isinstance(languages, list)
        or not languages
        or any(not isinstance(language, str) or not language for language in languages)
        or not isinstance(sample_rate, int | float)
        or isinstance(sample_rate, bool)
        or not math.isfinite(float(sample_rate))
        or not 0 <= sample_rate <= 1
    ):
        return False
    pages = value.get("pages")
    if not isinstance(pages, dict):
        return False
    if set(pages) != {str(page) for page in configured_pages}:
        return False
    for page_number, page_state in pages.items():
        if not isinstance(page_number, str) or not _is_page_state(page_state):
            return False
    error = value.get("error")
    if error is not None and (
        not isinstance(error, str) or re.fullmatch(r"ocr_[a-z0-9_]+", error) is None
    ):
        return False
    review_pages_value = value.get("review_pages")
    review_pending_value = value.get("review_pending")
    if review_pages_value is None:
        if review_pending_value is not None:
            return False
    else:
        if not isinstance(review_pages_value, list) or not review_pages_value:
            return False
        seen_review_pages: set[int] = set()
        for entry in review_pages_value:
            if not isinstance(entry, dict) or set(entry) != {
                "page",
                "reason",
                "alignment_status",
            }:
                return False
            review_page = entry.get("page")
            reason = entry.get("reason")
            if (
                not isinstance(review_page, int)
                or isinstance(review_page, bool)
                or review_page not in configured_pages
                or review_page in seen_review_pages
            ):
                return False
            if not isinstance(reason, str) or reason not in REVIEW_REASONS:
                return False
            alignment_status = entry.get("alignment_status")
            if not isinstance(alignment_status, str) or not alignment_status:
                return False
            seen_review_pages.add(review_page)
        if (
            not isinstance(review_pending_value, int)
            or isinstance(review_pending_value, bool)
            or review_pending_value != len(review_pages_value)
        ):
            return False
    expected_fingerprint = _fingerprint(
        {
            "pdf_snapshot": snapshot,
            "pages": configured_pages,
            "dpi": dpi,
            "languages": languages,
            "sample_rate": sample_rate,
        }
    )
    return input_fingerprint == expected_fingerprint
```

（g）`_is_page_state`（现 1361-1374 行）替换为：

```python
def _is_page_state(value: object) -> TypeGuard[_PageState]:
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return False
    if value.get("status") not in {status.value for status in PageStatus}:
        return False
    attempts = value.get("attempts")
    if attempts is not None and not isinstance(attempts, int):
        return False
    for field in (
        "error",
        "page_input_fingerprint",
        "evidence_fingerprint",
        "review_reason",
        "alignment_status",
    ):
        field_value = value.get(field)
        if field_value is not None and not isinstance(field_value, str):
            return False
    for field in ("vision", "codex", "alignment", "baidu", "decision"):
        if field in value and not _is_json_object(value[field]):
            return False
    return True
```

（h）`_load_or_create_state`（现 694-712 行）的读取命中分支替换为：

```python
        else:
            if value["input_fingerprint"] == fingerprint and value["run_config"] == run_config:
                value["schema_version"] = _BATCH_STATE_SCHEMA_VERSION
                return value
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_orchestrator.py -q`

预期：PASS（原有用例 + 6 个新用例；`exact_versioned` 断言已改为 3）。

- [ ] **步骤 6：Lint 与类型检查**

运行：

```bash
.venv/bin/ruff check src/parsing_core/workbench/ocr/orchestrator.py tests/test_workbench/test_ocr_orchestrator.py
.venv/bin/mypy src/parsing_core
```

预期：无错误。

- [ ] **步骤 7：Commit**

```bash
git add src/parsing_core/workbench/ocr/orchestrator.py tests/test_workbench/test_ocr_orchestrator.py
git commit -m "feat(ocr): isolate baidu-required pages as review pending"
```

---

## 任务 2：工作流 REVIEW_REQUIRED 状态与继续复核

**文件：**
- 修改：`src/parsing_core/workbench/ocr/workflow.py`（`WorkflowStatus` 231-238、`status_payload` 411-463、final 校验 466-506、`OcrWorkflow.start` 5875-5891、`status/_effective_status` 5960-6001、`completed_evidence` 6049-6054、`_persisted_status` 6126-6151）
- 修改：`src/parsing_core/workbench/ocr/chapters.py`（`_extract_page` 322-351）
- 测试：`tests/test_workbench/test_ocr_workflow.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_workbench/test_ocr_workflow.py` 顶部把 orchestrator 的 import 改为：

```python
from parsing_core.workbench.ocr.orchestrator import BatchRun, BatchStatus
```

（若现有为 `from parsing_core.workbench.ocr.orchestrator import BatchStatus`，只加 `BatchRun`。）

在 `_complete_workflow_fixture` 附近新增夹具：

```python
def _review_workflow_fixture(tmp_path: Path):
    engines = FakeEngines(codex_text="不同文本")
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None
    result = _run(orchestrator, engines)
    assert result.status is BatchStatus.REVIEW_REQUIRED
    return engines, tmp_path / "ocr-state", result
```

在文件末尾追加：

```python
def test_review_required_status_payload_lists_review_pages(tmp_path: Path):
    _engines, state_root, _result = _review_workflow_fixture(tmp_path)

    payload = status_payload(
        status=WorkflowStatus.REVIEW_REQUIRED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "review_required"
    assert payload["publishable"] is True
    assert payload["error"] is None
    assert payload["review_pending"] == 1
    assert payload["review_pages"] == [
        {"page": 1, "reason": "conflict", "alignment_status": "conflict"}
    ]


def test_review_workflow_restart_reports_review_required(tmp_path: Path):
    _engines, state_root, _result = _review_workflow_fixture(tmp_path)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("review work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "review_required"
    assert payload["publishable"] is True
    assert payload["review_pending"] == 1
    final, pages = workflow.completed_evidence()
    assert final["status"] == "review_required"
    assert pages[0]["status"] == "review_pending"


def test_review_required_final_validation_rejects_tampering(tmp_path: Path):
    _engines, state_root, _result = _review_workflow_fixture(tmp_path)
    final_path = state_root / "batch-final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final["review_pending"] = 2
    workflow_module._atomic_json(final_path, final)

    payload = status_payload(
        status=WorkflowStatus.REVIEW_REQUIRED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"


def test_start_review_uses_persisted_run_config(tmp_path: Path):
    _engines, state_root, _result = _review_workflow_fixture(tmp_path)
    observed: list[dict[str, object]] = []

    class StubOrchestrator:
        def run_batch(self, source_path, *, pages, dpi, languages, sample_rate, **kwargs):
            observed.append(
                {
                    "pages": tuple(pages),
                    "dpi": dpi,
                    "languages": tuple(languages),
                    "sample_rate": sample_rate,
                }
            )
            return BatchRun(BatchStatus.COMPLETED, {})

    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: StubOrchestrator(),
    )

    workflow.start_review()
    assert workflow._thread is not None
    workflow._thread.join(timeout=5)

    assert observed == [
        {"pages": (1,), "dpi": 300, "languages": ("zh-Hans",), "sample_rate": 0}
    ]


def test_start_review_rejects_completed_final(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path, publish_note=False)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    with pytest.raises(ValueError, match="ocr_review_not_ready"):
        workflow.start_review()


def test_detect_chapters_accepts_review_final(tmp_path: Path):
    _engines, state_root, _result = _review_workflow_fixture(tmp_path)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("review work must not rerun"),
    )

    tree = workflow.detect_chapters()

    assert tree["input_fingerprint"]
    assert isinstance(tree["chapters"], list)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_workflow.py -q -k "review_required_status or review_workflow_restart or start_review or detect_chapters_accepts_review"`

预期：FAIL（`WorkflowStatus` 无 `REVIEW_REQUIRED`；`start_review` 不存在）。

- [ ] **步骤 3：实现工作流状态、读取器、payload 与 `start_review`**

（a）`WorkflowStatus`（现 231-238 行）替换为：

```python
class WorkflowStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

（b）在 `_completed_ocr_final_is_valid`（现 473-506 行）之后新增：

```python
def _review_ocr_final_is_valid(final: dict[str, Any], source_path: str | Path) -> bool:
    try:
        if final.get("status") != BatchStatus.REVIEW_REQUIRED.value or not _is_batch_state(final):
            return False
        snapshot = final.get("pdf_snapshot")
        if not isinstance(snapshot, dict) or snapshot != _snapshot_pdf(source_path):
            return False
        input_fingerprint = final.get("input_fingerprint")
        pages = final.get("pages")
        if not isinstance(input_fingerprint, str) or not input_fingerprint:
            return False
        if not isinstance(pages, dict) or not pages:
            return False
        page_numbers = sorted(int(key) for key in pages)
        if page_numbers != list(range(1, len(page_numbers) + 1)):
            return False
        validator = OcrOrchestrator(
            vision=None, codex=None, baidu=None, state_root=Path(source_path).parent
        )
        return validator._review_final_is_valid(final)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _read_ocr_final(
    final_path: Path, source_path: str | Path
) -> tuple[WorkflowStatus, dict[str, Any]]:
    final = _read_regular_json(final_path)
    if _completed_ocr_final_is_valid(final, source_path):
        return WorkflowStatus.COMPLETED, final
    if _review_ocr_final_is_valid(final, source_path):
        return WorkflowStatus.REVIEW_REQUIRED, final
    raise ValueError("OCR final evidence is invalid")
```

（c）`status_payload`（现 411-436 行）替换为：

```python
def status_payload(
    *,
    status: WorkflowStatus,
    source_path: str | Path,
    state_root: str | Path,
    error: str | None = None,
) -> dict[str, Any]:
    paths = workflow_paths(state_root)
    final: dict[str, Any] | None = None
    if status in {WorkflowStatus.COMPLETED, WorkflowStatus.REVIEW_REQUIRED}:
        try:
            _validate_finalized_migration_receipts(paths)
            status, final = _read_ocr_final(paths.final, source_path)
        except _LegacyMigrationError as exc:
            status = WorkflowStatus.BLOCKED
            error = exc.code
        except (OSError, ValueError):
            status = WorkflowStatus.BLOCKED
            error = "ocr_evidence_invalid"
    return _status_payload_from_snapshot(
        status=status,
        source_path=source_path,
        paths=paths,
        error=error,
        completed_final=final,
    )
```

（d）`_status_payload_from_snapshot`（现 439-463 行）替换为：

```python
def _status_payload_from_snapshot(
    *,
    status: WorkflowStatus,
    source_path: str | Path,
    paths: WorkflowPaths,
    error: str | None,
    completed_final: dict[str, Any] | None,
) -> dict[str, Any]:
    published = False
    published_path: Path | None = None
    review_pages: list[dict[str, Any]] | None = None
    review_pending = 0
    if status is WorkflowStatus.COMPLETED:
        if completed_final is None:
            status = WorkflowStatus.BLOCKED
            error = "ocr_evidence_invalid"
        else:
            published, error, published_path = _publication_status(completed_final, paths)
    elif status is WorkflowStatus.REVIEW_REQUIRED:
        if completed_final is None:
            status = WorkflowStatus.BLOCKED
            error = "ocr_evidence_invalid"
        else:
            published = True
            review_pages = list(completed_final.get("review_pages") or [])
            review_pending = int(completed_final.get("review_pending") or 0)
    return {
        "status": status.value,
        "source_path": str(Path(source_path).expanduser()),
        "state_path": str(paths.state),
        "error": error,
        "publishable": published,
        "markdown_path": str(published_path) if published_path is not None else None,
        "chapter_tree_path": str(paths.chapter_tree) if paths.chapter_tree.is_file() else None,
        "review_pages": review_pages,
        "review_pending": review_pending,
    }
```

（e）`OcrWorkflow.start`（现 5875-5891 行）替换为：

```python
    def start(
        self,
        *,
        dpi: int = 300,
        languages: tuple[str, ...] = ("zh-Hans", "en-US"),
        pages: tuple[int, ...] | None = None,
        sample_rate: float = 0.05,
    ) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("OCR 任务正在运行")
            claim = _try_claim_ocr_worker(self.paths)
            if claim is None:
                raise ValueError("OCR 任务正在运行")
            self._worker_claim = claim
            try:
                self._cancel.clear()
                self._error = None
                self._status = WorkflowStatus.RUNNING
                self._launch_thread(
                    dpi=dpi,
                    languages=languages,
                    pages=pages,
                    sample_rate=sample_rate,
                )
            except Exception:
                self._status = WorkflowStatus.IDLE
                self._release_worker_claim()
                raise

    def start_review(self) -> None:
        try:
            status, final = _read_ocr_final(self.paths.final, self.source_path)
        except (OSError, ValueError) as exc:
            raise ValueError("ocr_review_not_ready") from exc
        if status is not WorkflowStatus.REVIEW_REQUIRED or final is None:
            raise ValueError("ocr_review_not_ready")
        run_config = final["run_config"]
        self.start(
            dpi=int(run_config["dpi"]),
            languages=tuple(str(language) for language in run_config["languages"]),
            pages=tuple(int(page) for page in run_config["pages"]),
            sample_rate=float(run_config["sample_rate"]),
        )
```

（f）`_effective_status`（现 5971-6001 行）替换为：

```python
    def _effective_status(
        self,
    ) -> tuple[WorkflowStatus, str | None, dict[str, Any] | None]:
        with self._lock:
            try:
                _validate_finalized_migration_receipts(self.paths)
            except _LegacyMigrationError as exc:
                return WorkflowStatus.BLOCKED, exc.code, None
            status = self._status
            error = self._error
            if (
                status
                in {
                    WorkflowStatus.COMPLETED,
                    WorkflowStatus.REVIEW_REQUIRED,
                    WorkflowStatus.BLOCKED,
                    WorkflowStatus.FAILED,
                    WorkflowStatus.CANCELLED,
                }
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return WorkflowStatus.RUNNING, None, None
            if status is WorkflowStatus.IDLE:
                return self._persisted_status()
            if status in {WorkflowStatus.COMPLETED, WorkflowStatus.REVIEW_REQUIRED}:
                try:
                    final_status, final = _read_ocr_final(self.paths.final, self.source_path)
                except (OSError, ValueError):
                    return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
                return final_status, None, final
            return status, error, None
```

（g）`completed_evidence`（现 6049-6054 行）替换为：

```python
    def completed_evidence(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._lock:
            status, _error, final = self._effective_status()
            if (
                status not in {WorkflowStatus.COMPLETED, WorkflowStatus.REVIEW_REQUIRED}
                or final is None
            ):
                raise ValueError("OCR 尚未完成，不能读取证据")
            return final, _normalized_completed_pages(final)
```

（h）`_persisted_status`（现 6126-6151 行）替换为：

```python
    def _persisted_status(
        self,
    ) -> tuple[WorkflowStatus, str | None, dict[str, Any] | None]:
        if self._migration_error is not None:
            return WorkflowStatus.BLOCKED, self._migration_error, None
        try:
            final_status, final = _read_ocr_final(self.paths.final, self.source_path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
        else:
            return final_status, None, final

        try:
            value = _read_regular_json(self.paths.state)
        except FileNotFoundError:
            return WorkflowStatus.IDLE, None, None
        except (OSError, ValueError):
            return WorkflowStatus.BLOCKED, "ocr_state_invalid", None
        status, error = restored_workflow_status(value)
        if status is WorkflowStatus.RUNNING:
            return WorkflowStatus.BLOCKED, "ocr_state_interrupted", None
        if status in {WorkflowStatus.COMPLETED, WorkflowStatus.REVIEW_REQUIRED}:
            return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
        return status, error, None
```

（i）`chapters.py` 顶部 import 区加入（放在 `from .atomic_io import atomic_replace_bytes` 之后）：

```python
from .orchestrator import PageStatus
```

（j）`chapters.py` 的 `_extract_page`（现 322 行）开头替换为：

```python
def _extract_page(record: object) -> _PageEvidence:
    page = _page_number(record)
    if not isinstance(record, dict):
        raise ChapterConfirmationError("OCR page number is invalid")
    if record.get("status") == PageStatus.REVIEW_PENDING.value:
        return _PageEvidence(page, (), "review_pending", "review_pending")
    decision = record.get("decision") if isinstance(record, dict) else None
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_workflow.py tests/test_workbench/test_ocr_orchestrator.py -q`

预期：PASS。既有 `test_restored_workflow_status_preserves_known_enum_values` 会自动覆盖新增枚举值。

- [ ] **步骤 5：运行章节/笔记相关既有用例确认无回归**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_markdown_notes.py tests/test_workbench/test_ocr_orchestrator.py -q`

预期：PASS（此时 `chapters.py` 只多接受一种页状态，不影响旧路径）。

- [ ] **步骤 6：Lint 与类型检查**

运行：

```bash
.venv/bin/ruff check src/parsing_core/workbench/ocr/workflow.py src/parsing_core/workbench/ocr/chapters.py tests/test_workbench/test_ocr_workflow.py
.venv/bin/mypy src/parsing_core
```

预期：无错误。

- [ ] **步骤 7：Commit**

```bash
git add src/parsing_core/workbench/ocr/workflow.py src/parsing_core/workbench/ocr/chapters.py tests/test_workbench/test_ocr_workflow.py
git commit -m "feat(ocr): expose review-required workflow state and review resume"
```

---

## 任务 3：笔记占位符与 review 元数据

**文件：**
- 修改：`src/parsing_core/workbench/ocr/markdown_notes.py`（builder 135-238、`validate_intensive_reading_note` 241-270、`_accepted_pages` 369-440、`_render_markdown` 443-478）
- 修改：`src/parsing_core/workbench/ocr/schemas/intensive-reading-note.json`（metadata properties）
- 修改：`src/parsing_core/workbench/ocr/deepseek_intensive_reading.py`（`_finalize_generated_note` 139-276）
- 测试：`tests/test_workbench/test_ocr_markdown_notes.py`、`tests/test_workbench/test_deepseek_intensive_reading.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_workbench/test_ocr_markdown_notes.py` 的 `_inputs()` 之后新增 helper，并在文件末尾追加用例：

```python
def _review_page(number: int) -> dict:
    return {
        "page": number,
        "status": "review_pending",
        "page_input_fingerprint": "book-input",
        "evidence_fingerprint": "",
    }


def test_review_pending_page_becomes_placeholder_with_top_counter():
    tree, confirmation, _pages = _inputs()
    pages = [_page(2, "第一章 战略管理", "战略是组织的长期方向。"), _review_page(3)]

    note = build_intensive_reading_note(
        tree,
        confirmation,
        pages,
        source_id="source-1",
        review_pending=1,
    )

    assert note["markdown"].startswith("<!-- pdf2md: review_pending=1 -->\n")
    assert "<!-- pdf2md: review pending page 3 -->" in note["markdown"]
    assert "[src:source-1:p2" in note["markdown"]
    assert note["metadata"]["review_pending"] == 1
    assert note["metadata"]["review_pages"] == [3]
    validate_intensive_reading_note(note)


def test_review_only_chapter_is_rejected():
    tree, confirmation, _pages = _inputs()
    pages = [_review_page(2), _review_page(3)]

    with pytest.raises(MarkdownNoteError, match="accepted OCR pages"):
        build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")


def test_validate_rejects_missing_review_placeholder():
    tree, confirmation, _pages = _inputs()
    pages = [_page(2, "第一章 战略管理", "战略是组织的长期方向。"), _review_page(3)]
    note = build_intensive_reading_note(
        tree,
        confirmation,
        pages,
        source_id="source-1",
        review_pending=1,
    )
    broken = dict(note)
    broken["markdown"] = note["markdown"].replace(
        "<!-- pdf2md: review pending page 3 -->", ""
    )

    with pytest.raises(MarkdownNoteError, match="review placeholder"):
        validate_intensive_reading_note(broken)
```

在 `tests/test_workbench/test_deepseek_intensive_reading.py` 的 `_accepted_base_note` 之后新增 helper，并在文件末尾追加：

```python
def _accepted_review_base_note():
    tree, confirmation, pages = _inputs()
    pages = [
        pages[0],
        {
            "page": 3,
            "status": "review_pending",
            "page_input_fingerprint": "book-input",
            "evidence_fingerprint": "",
        },
    ]
    return build_intensive_reading_note(
        tree, confirmation, pages, source_id="book-1", review_pending=1
    )


def test_generator_preserves_review_pending_markup():
    base = _accepted_review_base_note()
    output = _generated(base)
    prompt = build_generation_prompt(base)
    output["metadata"]["prompt_fingerprint"] = prompt_fingerprint(prompt)
    client = FakeClient(json.dumps(output, ensure_ascii=False))

    result = DeepSeekIntensiveReadingGenerator(client).generate(base)

    assert result["metadata"]["review_pending"] == 1
    assert result["metadata"]["review_pages"] == [3]
    assert "<!-- pdf2md: review_pending=1 -->" in result["markdown"]
    assert "<!-- pdf2md: review pending page 3 -->" in result["markdown"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_markdown_notes.py tests/test_workbench/test_deepseek_intensive_reading.py -q -k "review"`

预期：FAIL（`build_intensive_reading_note() got an unexpected keyword argument 'review_pending'`）。

- [ ] **步骤 3：实现笔记占位符、顶部计数与元数据**

（a）`markdown_notes.py` 的 import 区加入：

```python
from .orchestrator import PageStatus
```

（b）在 `_MINDMAP_LINE_RE` 之后新增正则：

```python
_REVIEW_COMMENT_RE = re.compile(
    r"<!-- pdf2md: (?:review_pending=\d+|review pending page \d+) -->"
)
```

（c）`build_intensive_reading_note` 签名（现 135-142 行）替换为：

```python
def build_intensive_reading_note(
    chapter_tree: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    pages: Iterable[Mapping[str, Any]],
    *,
    source_id: str,
    prompt_rules_version: str = DEFAULT_PROMPT_RULES_VERSION,
    review_pending: int | None = None,
) -> dict[str, Any]:
```

（d）把 builder 正文中 `chapter = confirmation["chapter"]` 到 `if not evidence_lines:` 的整段（现 158-178 行）替换为：

```python
    chapter = confirmation["chapter"]
    page_records = _accepted_pages(
        pages, chapter, expected_input_fingerprint=chapter_tree["input_fingerprint"]
    )
    if not page_records:
        raise MarkdownNoteError("accepted OCR pages are required")
    chapter_review_pages = tuple(
        page for page, _evidence, _input, blocks in page_records if not blocks
    )
    if review_pending is None:
        review_pending = len(chapter_review_pages)
    if (
        not isinstance(review_pending, int)
        or isinstance(review_pending, bool)
        or review_pending < len(chapter_review_pages)
    ):
        raise MarkdownNoteError("review pending count is invalid")
    source_refs: list[str] = []
    evidence_lines: list[str] = []
    accepted_pages = 0
    for page, evidence, page_input, blocks in page_records:
        if not blocks:
            evidence_lines.append(f"- <!-- pdf2md: review pending page {page} -->")
            continue
        accepted_pages += 1
        for block in blocks:
            block_id = block.id
            citation = f"[src:{source_id}:p{page}:{block_id}]"
            text = _safe_markdown_text(block.text)
            if text:
                source_refs.append(citation)
                evidence_lines.append(
                    f"- {citation}（PDF 第 {page} 页；OCR 输入指纹 `{page_input}`；"
                    f"证据指纹 `{evidence}`）：{text}"
                )
    if not accepted_pages:
        raise MarkdownNoteError("chapter requires accepted OCR pages")
    if not evidence_lines:
        raise MarkdownNoteError("accepted OCR contains no text evidence")
```

（e）metadata 字典（现 180-192 行）的结尾 `"citation_ids": source_refs,` 替换为：

```python
        "citation_ids": source_refs,
        "review_pending": review_pending,
        "review_pages": list(chapter_review_pages),
    }
```

（f）`_accepted_pages` 的 `for record in selected:` 循环开头（现 383-385 行）替换为：

```python
    for record in selected:
        if record.get("status") == PageStatus.REVIEW_PENDING.value:
            result.append(
                _AcceptedPage(
                    page=_page_number(record),
                    evidence="",
                    input_fingerprint="",
                    blocks=(),
                )
            )
            continue
        decision = record.get("decision")
        payload = decision.get("payload") if isinstance(decision, dict) else None
```

（g）`_render_markdown`（现 443-462 行）的开头替换为：

```python
def _render_markdown(
    chapter: Mapping[str, object],
    metadata: Mapping[str, object],
    sections: Iterable[Mapping[str, object]],
    mermaid: Sequence[Mapping[str, object]],
) -> str:
    lines: list[str] = []
    review_pending = metadata.get("review_pending", 0)
    if (
        isinstance(review_pending, int)
        and not isinstance(review_pending, bool)
        and review_pending > 0
    ):
        lines.extend([f"<!-- pdf2md: review_pending={review_pending} -->", ""])
    lines.extend(
        [
            f"# {_safe_markdown_text(chapter['number'])} {_safe_markdown_text(chapter['title'])}",
            "",
            f"> 来源：PDF 第 {metadata['page_start']}–{metadata['page_end']} 页",
            f"> 输入指纹：`{metadata['input_fingerprint']}`",
            f"> 章节指纹：`{metadata['chapter_fingerprint']}`",
            f"> OCR 证据指纹：`{metadata['evidence_fingerprint']}`",
            f"> 精读规则版本：`{metadata['prompt_rules_version']}`",
        ]
    )
    if metadata.get("model"):
        lines.append(f"> 模型：`{metadata['model']}`")
    if metadata.get("prompt_fingerprint"):
        lines.append(f"> Prompt 指纹：`{metadata['prompt_fingerprint']}`")
    lines.append("")
```

（h）`validate_intensive_reading_note` 中（现 258-262 行）：

```python
    markdown = value["markdown"]
    if _DANGEROUS_RE.search(markdown) or "<" in markdown:
        raise MarkdownNoteError("markdown contains unsafe markup")
```

替换为：

```python
    markdown = value["markdown"]
    sanitized = _REVIEW_COMMENT_RE.sub("", markdown)
    if _DANGEROUS_RE.search(sanitized) or "<" in sanitized:
        raise MarkdownNoteError("markdown contains unsafe markup")
```

（i）在同函数结尾（现 269-270 行 `if set(item["key"] ...` 之后、函数结束前）追加：

```python
    review_pending = metadata.get("review_pending", 0)
    review_pages = metadata.get("review_pages", [])
    if (
        not isinstance(review_pending, int)
        or isinstance(review_pending, bool)
        or review_pending < 0
    ):
        raise MarkdownNoteError("review pending count is invalid")
    if (
        not isinstance(review_pages, list)
        or review_pages != sorted(review_pages)
        or len(review_pages) != len(set(review_pages))
        or any(
            not isinstance(page, int) or isinstance(page, bool) or page < 1
            for page in review_pages
        )
        or len(review_pages) > review_pending
    ):
        raise MarkdownNoteError("review pages are invalid")
    if review_pending:
        if (
            markdown.splitlines()[0].strip()
            != f"<!-- pdf2md: review_pending={review_pending} -->"
        ):
            raise MarkdownNoteError("review pending header is missing")
        for page in review_pages:
            if f"<!-- pdf2md: review pending page {page} -->" not in markdown:
                raise MarkdownNoteError("review placeholder is missing")
    elif review_pages or "<!-- pdf2md:" in markdown:
        raise MarkdownNoteError("unexpected review markup")
```

（j）`schemas/intensive-reading-note.json` 的 metadata properties，把 `"note_fingerprint"` 行改为：

```json
        "note_fingerprint": {"type": "string", "minLength": 1, "maxLength": 128},
        "review_pending": {"type": "integer", "minimum": 0, "maximum": 10000},
        "review_pages": {
          "type": "array", "maxItems": 10000, "uniqueItems": true,
          "items": {"type": "integer", "minimum": 1}
        }
```

- [ ] **步骤 4：实现 DeepSeek 生成阶段的 review 绑定**

（a）`deepseek_intensive_reading.py` 的 `allowed_metadata`（现 152-166 行）在 `"prompt_fingerprint"` 之后加两项：

```python
        "prompt_fingerprint",
        "review_pending",
        "review_pages",
    }
```

（b）metadata 绑定循环（现 169-171 行）替换为：

```python
    for key, value in base_metadata.items():
        if key in {"note_fingerprint", "review_pending", "review_pages"}:
            continue
        if metadata.get(key) != value:
            raise DeepSeekGenerationError("generated note metadata is not bound to input")
```

（c）最终 metadata 组装（现 253-260 行）在 `metadata["citation_ids"] = list(citation_ids)` 之后加：

```python
    metadata["review_pending"] = base_metadata.get("review_pending", 0)
    metadata["review_pages"] = list(base_metadata.get("review_pages", []))
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_markdown_notes.py tests/test_workbench/test_deepseek_intensive_reading.py -q`

预期：PASS（既有 6 段笔记结构、Mermaid 校验与生成绑定用例全部保持）。

- [ ] **步骤 6：Lint 与类型检查**

运行：

```bash
.venv/bin/ruff check src/parsing_core/workbench/ocr/markdown_notes.py src/parsing_core/workbench/ocr/deepseek_intensive_reading.py tests/test_workbench/test_ocr_markdown_notes.py tests/test_workbench/test_deepseek_intensive_reading.py
.venv/bin/mypy src/parsing_core
```

预期：无错误。

- [ ] **步骤 7：Commit**

```bash
git add src/parsing_core/workbench/ocr/markdown_notes.py src/parsing_core/workbench/ocr/schemas/intensive-reading-note.json src/parsing_core/workbench/ocr/deepseek_intensive_reading.py tests/test_workbench/test_ocr_markdown_notes.py tests/test_workbench/test_deepseek_intensive_reading.py
git commit -m "feat(ocr): preserve review placeholders in intensive-reading notes"
```

---

## 任务 4：发布契约记录 review_pending

**文件：**
- 修改：`src/parsing_core/workbench/ocr/workflow.py`（`_PUBLICATION_METADATA_FIELDS` 55-69、`_validate_publication_metadata` 2686-2738、`_markdown_publication_is_valid` 2741-2795）
- 测试：`tests/test_workbench/test_ocr_workflow.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_workbench/test_ocr_workflow.py` 末尾追加：

```python
def test_publication_metadata_accepts_review_pending_fields(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    metadata["review_pending"] = 2
    metadata["review_pages"] = [2]

    workflow_module._validate_publication_metadata(
        metadata,
        input_fingerprint=final["input_fingerprint"],
        evidence_fingerprint=tree["evidence_fingerprint"],
        chapter=confirmation["chapter"],
    )

    metadata["review_pending"] = 0
    with pytest.raises(ValueError, match="review metadata"):
        workflow_module._validate_publication_metadata(
            metadata,
            input_fingerprint=final["input_fingerprint"],
            evidence_fingerprint=tree["evidence_fingerprint"],
            chapter=confirmation["chapter"],
        )


def test_markdown_publication_requires_review_markup(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    metadata["review_pending"] = 1
    metadata["review_pages"] = [2]
    markdown = _valid_markdown(final, tree, confirmation)
    digest = hashlib.sha256(markdown.encode()).hexdigest()

    assert (
        workflow_module._markdown_publication_is_valid(
            metadata, markdown, final["input_fingerprint"], expected_sha256=digest
        )
        is False
    )

    reviewed = _valid_markdown(final, tree, confirmation).replace(
        "## 原文证据\n",
        "## 原文证据\n<!-- pdf2md: review pending page 2 -->\n",
    )
    reviewed = "<!-- pdf2md: review_pending=1 -->\n" + reviewed
    reviewed_digest = hashlib.sha256(reviewed.encode()).hexdigest()
    assert (
        workflow_module._markdown_publication_is_valid(
            metadata, reviewed, final["input_fingerprint"], expected_sha256=reviewed_digest
        )
        is True
    )
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_workflow.py -q -k "publication_metadata_accepts_review or markdown_publication_requires_review"`

预期：FAIL（`_validate_publication_metadata` 对 `review_pending` 报 unexpected fields；markdown 校验忽略注释要求）。

- [ ] **步骤 3：实现发布 metadata 与 markdown 校验**

（a）`_PUBLICATION_METADATA_FIELDS`（现 55-69 行）替换为：

```python
_PUBLICATION_METADATA_FIELDS = {
    "input_fingerprint",
    "chapter_fingerprint",
    "evidence_fingerprint",
    "prompt_rules_version",
    "source_id",
    "chapter_id",
    "chapter_number",
    "chapter_title",
    "page_start",
    "page_end",
    "citation_ids",
    "model",
    "prompt_fingerprint",
    "review_pending",
    "review_pages",
}
_REQUIRED_PUBLICATION_METADATA_FIELDS = _PUBLICATION_METADATA_FIELDS - {
    "review_pending",
    "review_pages",
}
```

（b）`_validate_publication_metadata` 开头（现 2693-2699 行）替换为：

```python
    fields = set(metadata)
    unexpected = fields - _PUBLICATION_METADATA_FIELDS
    if unexpected:
        raise ValueError("published metadata has unexpected fields")
    if not _REQUIRED_PUBLICATION_METADATA_FIELDS <= fields:
        raise ValueError("published metadata is incomplete")
    review_pending = metadata.get("review_pending", 0)
    review_pages = metadata.get("review_pages", [])
    if (
        not isinstance(review_pending, int)
        or isinstance(review_pending, bool)
        or review_pending < 0
        or not isinstance(review_pages, list)
        or any(
            not isinstance(page, int) or isinstance(page, bool) or page < 1
            for page in review_pages
        )
        or len(review_pages) != len(set(review_pages))
        or review_pages != sorted(review_pages)
        or len(review_pages) > review_pending
    ):
        raise ValueError("published review metadata is invalid")
    if metadata.get("model") != "deepseek-v4-pro":
```

（c）`_markdown_publication_is_valid` 结尾替换为：

```python
    diagrams = _MARKDOWN_FENCE_RE.findall(markdown)
    if len(diagrams) != 2:
        return False
    try:
        validate_mermaid_block(diagrams[0], expected_type="flowchart")
        validate_mermaid_block(diagrams[1], expected_type="flowchart")
    except Exception:
        return False
    review_pending = metadata.get("review_pending", 0)
    review_pages = metadata.get("review_pages", [])
    if review_pending:
        if f"<!-- pdf2md: review_pending={review_pending} -->" not in markdown:
            return False
        if any(
            f"<!-- pdf2md: review pending page {page} -->" not in markdown
            for page in review_pages
        ):
            return False
    elif "<!-- pdf2md:" in markdown:
        return False
    return "[src:" in markdown
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_workflow.py -q`

再运行：`.venv/bin/python -m pytest tests/test_workbench/test_deepseek_intensive_reading.py tests/test_workbench/test_ocr_markdown_notes.py -q`

预期：全部 PASS（旧笔记不含 review 字段时按 0 处理；旧 legacy 迁移发布路径的 metadata 不含 review 字段，仍满足 required 子集校验）。

- [ ] **步骤 5：Lint 与类型检查**

运行：

```bash
.venv/bin/ruff check src/parsing_core/workbench/ocr/workflow.py tests/test_workbench/test_ocr_workflow.py
.venv/bin/mypy src/parsing_core
```

预期：无错误。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/ocr/workflow.py tests/test_workbench/test_ocr_workflow.py
git commit -m "feat(ocr): record review_pending count in publication contract"
```

---

## 任务 5：API 接线（可选百度引擎 + 继续复核端点）

**文件：**
- 修改：`src/parsing_core/serving/api/routes_workbench.py`（factory 337-398、OCR 路由 620-760）
- 测试：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写失败的测试**

（a）把 `tests/test_workbench/test_api.py` 顶部从 `test_ocr_workflow` 的 import 改为：

```python
from test_ocr_workflow import (
    _complete_workflow_fixture,
    _note_metadata,
    _prepare_chapter_context,
    _review_workflow_fixture,
    _valid_markdown,
)
```

（b）在文件末尾追加：

```python
def test_ocr_factory_tolerates_missing_baidu_key(tmp_path, monkeypatch):
    routes_workbench._OCR_WORKFLOWS.clear()
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    source = SimpleNamespace(id="source-baidu-optional", file_path=str(pdf))
    course = SimpleNamespace(root_dir=str(tmp_path))
    captured: dict[str, object] = {}

    class StubWorkflow:
        def __init__(self, *, source_path, state_root, orchestrator_factory):
            captured["factory"] = orchestrator_factory

    monkeypatch.setattr(routes_workbench, "OcrWorkflow", StubWorkflow)
    monkeypatch.setattr(routes_workbench, "_find_vision_helper", lambda: tmp_path / "vision")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda path=None: "codex")
    monkeypatch.setattr(routes_workbench, "resolve_baidu_api_key", lambda: None)
    monkeypatch.setattr(
        routes_workbench,
        "VisionClient",
        lambda **kwargs: SimpleNamespace(cache=SimpleNamespace(pages_dir=tmp_path / "pages")),
    )
    monkeypatch.setattr(routes_workbench, "CodexVisionExecutor", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        routes_workbench,
        "BaiduOcrClient",
        lambda **kwargs: pytest.fail("baidu client must not be constructed without a key"),
    )
    monkeypatch.setattr(routes_workbench, "RegisteredPdfSources", lambda paths: object())

    workflow = routes_workbench._ocr_workflow(source, course)

    assert workflow.__class__ is StubWorkflow
    orchestrator = captured["factory"](lambda: False)
    assert orchestrator.baidu is None


def test_ocr_factory_uses_baidu_when_key_present(tmp_path, monkeypatch):
    routes_workbench._OCR_WORKFLOWS.clear()
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    source = SimpleNamespace(id="source-baidu-present", file_path=str(pdf))
    course = SimpleNamespace(root_dir=str(tmp_path))
    captured: dict[str, object] = {}
    sentinel = object()

    class StubWorkflow:
        def __init__(self, *, source_path, state_root, orchestrator_factory):
            captured["factory"] = orchestrator_factory

    monkeypatch.setattr(routes_workbench, "OcrWorkflow", StubWorkflow)
    monkeypatch.setattr(routes_workbench, "_find_vision_helper", lambda: tmp_path / "vision")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda path=None: "codex")
    monkeypatch.setattr(routes_workbench, "resolve_baidu_api_key", lambda: "baidu-key")
    monkeypatch.setattr(
        routes_workbench,
        "VisionClient",
        lambda **kwargs: SimpleNamespace(cache=SimpleNamespace(pages_dir=tmp_path / "pages")),
    )
    monkeypatch.setattr(routes_workbench, "CodexVisionExecutor", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(routes_workbench, "BaiduOcrClient", lambda **kwargs: sentinel)
    monkeypatch.setattr(routes_workbench, "RegisteredPdfSources", lambda paths: object())

    routes_workbench._ocr_workflow(source, course)

    orchestrator = captured["factory"](lambda: False)
    assert orchestrator.baidu is not None
    assert orchestrator.baidu.client is sentinel


def test_ocr_review_route_requires_baidu_key(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    fixture_root = root / "ocr-fixture"
    fixture_root.mkdir()
    _engines, state_root, _result = _review_workflow_fixture(fixture_root)
    _course, source = _registered_pdf_source(c, root, fixture_root / "book.pdf")
    workflow = OcrWorkflow(
        source_path=fixture_root / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("review work must not rerun"),
    )
    monkeypatch.setattr(routes_workbench, "_ocr_workflow", lambda *args, **kwargs: workflow)
    monkeypatch.setattr(routes_workbench, "resolve_baidu_api_key", lambda: None)

    response = c.post(f"/api/workbench/sources/{source['id']}/ocr/review")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "ocr_review_not_ready",
        "params": {"reason": "baidu_key_missing"},
    }


def test_ocr_review_route_rejects_without_review_final(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    _course, source = _registered_pdf_source(c, root, pdf)
    workflow = OcrWorkflow(
        source_path=pdf,
        state_root=root / ".pdf2md" / "empty-review",
        orchestrator_factory=lambda _cancel: pytest.fail("review work must not run"),
    )
    monkeypatch.setattr(routes_workbench, "_ocr_workflow", lambda *args, **kwargs: workflow)
    monkeypatch.setattr(routes_workbench, "resolve_baidu_api_key", lambda: "baidu-key")

    response = c.post(f"/api/workbench/sources/{source['id']}/ocr/review")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ocr_review_not_ready"


def test_ocr_review_route_starts_review_with_persisted_config(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    fixture_root = root / "ocr-fixture"
    fixture_root.mkdir()
    _engines, state_root, _result = _review_workflow_fixture(fixture_root)
    _course, source = _registered_pdf_source(c, root, fixture_root / "book.pdf")
    workflow = OcrWorkflow(
        source_path=fixture_root / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("stub start must intercept"),
    )
    recorded: dict[str, object] = {}
    monkeypatch.setattr(workflow, "start", lambda **kwargs: recorded.update(kwargs))
    monkeypatch.setattr(routes_workbench, "_ocr_workflow", lambda *args, **kwargs: workflow)
    monkeypatch.setattr(routes_workbench, "resolve_baidu_api_key", lambda: "baidu-key")

    response = c.post(f"/api/workbench/sources/{source['id']}/ocr/review")

    assert response.status_code == 200
    assert response.json()["status"] == "review_required"
    assert recorded == {
        "dpi": 300,
        "languages": ("zh-Hans",),
        "pages": (1,),
        "sample_rate": 0.0,
    }
```

（c）迁移旧断言：删除 `test_ocr_without_provider_reports_structured_error`（现 3792-3814 行）的整个函数体，替换为：

```python
def test_ocr_without_baidu_no_longer_blocks_start(tmp_path, monkeypatch):
    test_client = client(tmp_path)
    root = course_root(tmp_path)
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    _course, source = _registered_pdf_source(test_client, root, pdf)
    monkeypatch.setattr(routes_workbench.environment_module, "read_secret", lambda *_args: "")
    monkeypatch.delenv("PDF2MD_BAIDU_API_KEY", raising=False)
    observed: dict[str, object] = {}

    class StubWorkflow:
        def start(self):
            observed["started"] = True

        def status(self):
            return {"status": "running"}

    monkeypatch.setattr(routes_workbench, "OcrWorkflow", lambda **kwargs: StubWorkflow())
    response = test_client.post(
        f"/api/workbench/sources/{source['id']}/ocr",
        json={},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert observed == {"started": True}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py -q -k "factory_tolerates or factory_uses_baidu or review_route or no_longer_blocks"`

预期：FAIL（factory 仍抛 `WorkflowBlockedError("baidu_key_missing")`；`/ocr/review` 404）。

- [ ] **步骤 3：实现 factory 可选百度引擎**

把 `_ocr_workflow` 中（现 351-383 行）的 `try` 块替换为：

```python
            try:
                helper = _find_vision_helper()
                if settings is None:
                    codex_path = resolve_codex_path()
                else:
                    codex_path = resolve_codex_path(settings.codex_cli_path)
                baidu_key = resolve_baidu_api_key()
                validator = RegisteredPdfSources([pdf_path])
                vision = VisionClient(
                    helper_path=helper,
                    cache_root=state_root / "cache",
                    source_validator=validator,
                    helper_version="bundled-vision",
                    timeout=90,
                )
                codex = CodexVisionExecutor(
                    codex_path=codex_path,
                    temp_root=state_root / "codex-tmp",
                    trusted_image_root=vision.cache.pages_dir,
                    timeout=180,
                    cancel_event=cancel_signal,
                )
                baidu = (
                    _DeadlineAdapter(BaiduOcrClient(api_key=baidu_key))
                    if baidu_key
                    else None
                )
            except WorkflowBlockedError:
                raise
```

并把返回的 `OcrOrchestrator(...)` 中 `baidu=_DeadlineAdapter(baidu),` 改为：

```python
                baidu=baidu,
```

（`_find_vision_helper` 与 Codex 错误映射逻辑保持不变；`baidu_key` 必须在 factory 闭包内解析，保证缓存过 workflow 后新配置的 Key 在下次运行时生效。）

- [ ] **步骤 4：实现 `/ocr/review` 路由与 generate 透传**

（a）在 `cancel_source_ocr` 路由（现 657-672 行）之后新增：

```python
@router.post("/sources/{source_id}/ocr/review")
async def review_source_ocr(source_id: str, sch: SchedulerDep) -> dict[str, object]:
    def review_ocr_transaction() -> dict[str, object]:
        repo = _repo(sch)
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        course = repo.get_course(source.course_id)
        if course is None:
            raise HTTPException(404, "course not found")
        settings = load_settings(_settings_root(sch))
        if resolve_baidu_api_key() is None:
            raise api_error(409, "ocr_review_not_ready", reason="baidu_key_missing")
        workflow = _ocr_workflow(source, course, settings)
        try:
            workflow.start_review()
        except ValueError as exc:
            raise api_error(409, "ocr_review_not_ready", reason=str(exc)) from exc
        return workflow.status()

    return await run_in_threadpool(review_ocr_transaction)
```

（b）在 `generate_source_note` 路由中，把 `base = build_intensive_reading_note(tree, confirmation, pages, source_id=source.id)`（现 735-740 行）替换为：

```python
        review_pages = final.get("review_pages")
        review_pending = len(review_pages) if isinstance(review_pages, list) else 0
        base = build_intensive_reading_note(
            tree,
            confirmation,
            pages,
            source_id=source.id,
            review_pending=review_pending,
        )
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_api.py tests/test_workbench/test_ocr_workflow.py -q`

预期：PASS。注意 `test_missing_codex_maps_to_stable_error_code` 仍应返回 `codex_unavailable`（factory 在百度解析前先解析 Codex）。

- [ ] **步骤 6：Lint 与类型检查**

运行：

```bash
.venv/bin/ruff check src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_api.py
.venv/bin/mypy src/parsing_core
```

预期：无错误。

- [ ] **步骤 7：Commit**

```bash
git add src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_api.py
git commit -m "feat(api): start ocr without baidu and add review resume endpoint"
```

---

## 任务 6：前端待复核清单与继续复核

**文件：**
- 修改：`parsing-core-app/src/api/workbenchTypes.ts`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 修改：`parsing-core-app/src/api/ocrStatus.test.ts`
- 修改：`parsing-core-app/src/components/workbench/OcrWorkflowPanel.tsx`（整文件替换）
- 创建：`parsing-core-app/src/components/workbench/OcrWorkflowPanel.test.tsx`

- [ ] **步骤 1：编写失败的测试**

（a）更新 `parsing-core-app/src/api/ocrStatus.test.ts`：把 `it.each` 列表改为

```typescript
  it.each(["idle", "running", "completed", "review_required", "blocked", "failed", "cancelled"])(
```

并把两个 mock 返回的 json 对象都补上字段

```typescript
            review_pages: null,
            review_pending: 0,
```

再在文件末尾（describe 内）追加：

```typescript
  it("accepts review_required with a review page list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          status: "review_required",
          source_path: "/tmp/book.pdf",
          state_path: "/tmp/state/batch-state.json",
          error: null,
          publishable: true,
          markdown_path: null,
          chapter_tree_path: null,
          review_pages: [
            { page: 3, reason: "conflict", alignment_status: "conflict" },
            { page: 7, reason: "sampled", alignment_status: "consistent" },
          ],
          review_pending: 2,
        }),
      }),
    );

    const { getSourceOcrStatus } = await import("./workbench");
    await expect(getSourceOcrStatus("source-1")).resolves.toMatchObject({
      status: "review_required",
      review_pending: 2,
    });
  });
```

（b）创建 `parsing-core-app/src/components/workbench/OcrWorkflowPanel.test.tsx`：

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { SafeApiError } from "../../api/workbench";
import type { OcrStatus, Source } from "../../api/workbenchTypes";
import OcrWorkflowPanel from "./OcrWorkflowPanel";

const mocks = vi.hoisted(() => ({
  getSourceOcrStatus: vi.fn(),
  reviewSourceOcr: vi.fn(),
  startSourceOcr: vi.fn(),
  cancelSourceOcr: vi.fn(),
  recognizeSourceChapters: vi.fn(),
  confirmSourceChapter: vi.fn(),
  generateSourceNote: vi.fn(),
}));

vi.mock("../../api/workbench", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/workbench")>();
  return {
    SafeApiError: actual.SafeApiError,
    getSourceOcrStatus: mocks.getSourceOcrStatus,
    reviewSourceOcr: mocks.reviewSourceOcr,
    startSourceOcr: mocks.startSourceOcr,
    cancelSourceOcr: mocks.cancelSourceOcr,
    recognizeSourceChapters: mocks.recognizeSourceChapters,
    confirmSourceChapter: mocks.confirmSourceChapter,
    generateSourceNote: mocks.generateSourceNote,
  };
});

const source: Source = {
  id: "source-1",
  course_id: "course-1",
  kind: "main",
  file_path: "/tmp/book.pdf",
  title: "战略教材",
  status: "ready",
};

const reviewStatus: OcrStatus = {
  status: "review_required",
  source_path: "/tmp/book.pdf",
  state_path: "/tmp/state/batch-state.json",
  error: null,
  publishable: true,
  markdown_path: null,
  chapter_tree_path: null,
  review_pages: [
    { page: 3, reason: "conflict", alignment_status: "conflict" },
    { page: 7, reason: "sampled", alignment_status: "consistent" },
  ],
  review_pending: 2,
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSourceOcrStatus.mockResolvedValue(reviewStatus);
  mocks.reviewSourceOcr.mockResolvedValue(reviewStatus);
});

it("lists review pages and requests a review rerun", async () => {
  render(
    <MemoryRouter>
      <OcrWorkflowPanel source={source} />
    </MemoryRouter>,
  );

  expect(await screen.findByText("待复核 2 页")).toBeInTheDocument();
  expect(screen.getByText("第 3 页 · 冲突")).toBeInTheDocument();
  expect(screen.getByText("第 7 页 · 抽样")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "配置百度 Key 并继续复核" }));

  expect(mocks.reviewSourceOcr).toHaveBeenCalledWith("source-1");
});

it("links to settings when a review rerun is not ready", async () => {
  mocks.reviewSourceOcr.mockRejectedValueOnce(
    new SafeApiError("conflict", "ocr_review_not_ready", {}, 409),
  );
  render(
    <MemoryRouter>
      <OcrWorkflowPanel source={source} />
    </MemoryRouter>,
  );

  await screen.findByText("待复核 2 页");
  await userEvent.click(screen.getByRole("button", { name: "配置百度 Key 并继续复核" }));

  expect(await screen.findByRole("button", { name: "去配置百度 Key" })).toBeInTheDocument();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm test --prefix parsing-core-app -- --run src/api/ocrStatus.test.ts src/components/workbench/OcrWorkflowPanel.test.tsx`

预期：FAIL（`review_required` 未在 `OCR_STATUSES`；`reviewSourceOcr` 未导出；清单文案不存在）。

- [ ] **步骤 3：实现类型与 API 解析**

（a）`workbenchTypes.ts` 中把 `OcrWorkflowStatus` 与 `OcrStatus` 替换为：

```typescript
export type OcrWorkflowStatus =
  | "idle"
  | "running"
  | "completed"
  | "review_required"
  | "blocked"
  | "failed"
  | "cancelled";

export type OcrReviewReason = "conflict" | "complex" | "sampled";

export interface OcrReviewPage {
  page: number;
  reason: OcrReviewReason;
  alignment_status: string;
}

export interface OcrStatus {
  status: OcrWorkflowStatus;
  source_path: string;
  state_path: string;
  error: string | null;
  publishable: boolean;
  markdown_path: string | null;
  chapter_tree_path: string | null;
  review_pages: OcrReviewPage[] | null;
  review_pending: number;
}
```

（b）`workbench.ts` 的 workbenchTypes import 列表加入 `OcrReviewPage`；把 `OCR_STATUSES` 改为：

```typescript
const OCR_STATUSES = new Set([
  "idle",
  "running",
  "completed",
  "review_required",
  "blocked",
  "failed",
  "cancelled",
]);
```

（c）在 `parseOcrStatus` 之前新增 helper：

```typescript
function isReviewPages(value: unknown): value is OcrReviewPage[] {
  return (
    Array.isArray(value) &&
    value.every(
      (item) =>
        isRecord(item) &&
        typeof item.page === "number" &&
        Number.isInteger(item.page) &&
        item.page >= 1 &&
        (item.reason === "conflict" || item.reason === "complex" || item.reason === "sampled") &&
        typeof item.alignment_status === "string",
    )
  );
}
```

（d）`parseOcrStatus` 的条件里，在 `(value.chapter_tree_path !== null && ...)` 之后加：

```typescript
    (value.review_pages !== null && !isReviewPages(value.review_pages)) ||
    typeof value.review_pending !== "number" ||
    !Number.isInteger(value.review_pending) ||
    value.review_pending < 0
```

（e）在 `cancelSourceOcr` 之后新增：

```typescript
export function reviewSourceOcr(sourceId: string): Promise<OcrStatus> {
  return post<OcrStatus>(
    `/api/workbench/sources/${sourceId}/ocr/review`,
    undefined,
    parseOcrStatus,
  );
}
```

- [ ] **步骤 4：实现面板（整文件替换）**

用以下内容覆盖 `parsing-core-app/src/components/workbench/OcrWorkflowPanel.tsx`：

```tsx
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Loader2,
  Play,
  RefreshCw,
  Sparkles,
  XCircle,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import MermaidBlock from "../MermaidBlock";
import { ocrErrorInfo } from "../../api/errorMessages";
import {
  SafeApiError,
  cancelSourceOcr,
  confirmSourceChapter,
  generateSourceNote,
  getSourceOcrStatus,
  recognizeSourceChapters,
  reviewSourceOcr,
  startSourceOcr,
} from "../../api/workbench";
import type { OcrChapter, OcrChapterTree, OcrNoteResult, OcrStatus, Source } from "../../api/workbenchTypes";

const REVIEW_REASON_LABELS: Record<string, string> = {
  conflict: "冲突",
  complex: "复杂版式",
  sampled: "抽样",
};

export default function OcrWorkflowPanel({ source }: { source: Source }) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<OcrStatus | null>(null);
  const [tree, setTree] = useState<OcrChapterTree | null>(null);
  const [note, setNote] = useState<OcrNoteResult | null>(null);
  const [selectedChapter, setSelectedChapter] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needsBaiduKey, setNeedsBaiduKey] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await getSourceOcrStatus(source.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取 OCR 状态");
    }
  }, [source.id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    if (status?.status !== "running") return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [refresh, status?.status]);

  const run = async (operation: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    setNeedsBaiduKey(false);
    try {
      await operation();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const chapters = tree?.chapters ?? [];
  const reviewPages = status?.review_pages ?? [];
  const start = () =>
    void run(async () => {
      setTree(null);
      setNote(null);
      await startSourceOcr(source.id);
    });
  const cancel = () => void run(() => cancelSourceOcr(source.id));
  const review = () =>
    void run(async () => {
      try {
        await reviewSourceOcr(source.id);
      } catch (reason) {
        if (reason instanceof SafeApiError && reason.code === "ocr_review_not_ready") {
          setNeedsBaiduKey(true);
        }
        throw reason;
      }
    });
  const detect = () =>
    void run(async () => {
      const next = await recognizeSourceChapters(source.id);
      setTree(next);
      setSelectedChapter(next.chapters[0]?.id ?? null);
    });
  const confirm = () =>
    selectedChapter ? void run(() => confirmSourceChapter(source.id, selectedChapter)) : undefined;
  const generate = () =>
    selectedChapter ? void run(async () => setNote(await generateSourceNote(source.id, selectedChapter))) : undefined;

  return (
    <section aria-label={`无人值守 OCR：${source.title}`} className="border-t border-zinc-200 pt-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">无人值守精读链路</h2>
          <p className="mt-1 text-xs text-zinc-500">Apple Vision → Codex 视觉复核 → 百度冲突升级 → Codex 终审</p>
        </div>
        <StatusBadge status={status} />
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {status?.status !== "running" && status?.status !== "review_required" && (
          <button
            type="button"
            onClick={start}
            disabled={busy}
            className="inline-flex items-center gap-1.5 bg-zinc-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
          >
            <Play size={14} />
            启动 OCR
          </button>
        )}
        {status?.status === "running" && (
          <button
            type="button"
            onClick={cancel}
            disabled={busy}
            className="inline-flex items-center gap-1.5 border border-red-200 px-3 py-2 text-xs text-red-700 disabled:opacity-40"
          >
            <Ban size={14} />
            取消任务
          </button>
        )}
        {status?.status === "review_required" && (
          <button
            type="button"
            onClick={review}
            disabled={busy}
            className="inline-flex items-center gap-1.5 bg-amber-600 px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
          >
            <AlertTriangle size={14} />
            配置百度 Key 并继续复核
          </button>
        )}
        {(status?.status === "completed" || status?.status === "review_required") && (
          <button
            type="button"
            onClick={detect}
            disabled={busy}
            className="inline-flex items-center gap-1.5 border border-zinc-200 px-3 py-2 text-xs text-zinc-700 disabled:opacity-40"
          >
            <RefreshCw size={14} />
            识别章节
          </button>
        )}
        {status && ["failed", "blocked", "cancelled"].includes(status.status) && (
          <button
            type="button"
            onClick={start}
            disabled={busy}
            className="inline-flex items-center gap-1.5 border border-zinc-200 px-3 py-2 text-xs text-zinc-700 disabled:opacity-40"
          >
            <RefreshCw size={14} />
            重试
          </button>
        )}
      </div>
      {status?.status === "review_required" && (
        <div className="mt-3 border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <p className="font-medium">待复核 {status.review_pending} 页</p>
          <p className="mt-1">
            隔离页面不会进入正文，也不计入完成；配置百度 OCR Key 后只会重跑这些页面。
          </p>
          <ul className="mt-2 space-y-0.5">
            {reviewPages.map((item) => (
              <li key={item.page}>
                第 {item.page} 页 · {REVIEW_REASON_LABELS[item.reason] ?? item.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {status?.error && (
        <div role="alert" className="mt-3 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-xs text-red-700">
          <p className="font-medium">{ocrErrorInfo(status.error)?.title ?? status.error}</p>
          {ocrErrorInfo(status.error) && <p className="mt-1">{ocrErrorInfo(status.error)?.description}</p>}
          {ocrErrorInfo(status.error)?.action !== undefined && ocrErrorInfo(status.error)?.action !== "none" && (
            <button type="button" className="mt-2 underline" onClick={() => navigate("/workbench/settings")}>
              去精读设置
            </button>
          )}
        </div>
      )}
      {error && (
        <div role="alert" className="mt-3 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-xs text-red-700">
          <p>{error}</p>
          {needsBaiduKey && (
            <button
              type="button"
              className="mt-2 underline"
              onClick={() => navigate("/workbench/settings")}
            >
              去配置百度 Key
            </button>
          )}
        </div>
      )}
      {status?.status === "completed" && !status.publishable && (
        <p className="mt-3 text-xs text-amber-700">OCR 已结束，但尚未发布完整精读结果，不能标记为完成。</p>
      )}
      {chapters.length > 0 && (
        <div className="mt-4 border border-zinc-200 bg-white p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-xs font-semibold">章节候选 · {chapters.length}</h3>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={confirm}
                disabled={!selectedChapter || busy}
                className="border border-zinc-200 px-2.5 py-1.5 text-xs disabled:opacity-40"
              >
                确认章节
              </button>
              <button
                type="button"
                onClick={generate}
                disabled={!selectedChapter || busy}
                className="inline-flex items-center gap-1.5 bg-emerald-600 px-2.5 py-1.5 text-xs text-white disabled:opacity-40"
              >
                <Sparkles size={13} />
                运行精读
              </button>
            </div>
          </div>
          <div className="mt-3 space-y-1">
            {chapters.map((chapter) => (
              <ChapterOption
                key={chapter.id}
                chapter={chapter}
                selected={selectedChapter === chapter.id}
                onSelect={setSelectedChapter}
              />
            ))}
          </div>
        </div>
      )}
      {note?.publishable && <NotePreview markdown={note.markdown} />}
    </section>
  );
}

function StatusBadge({ status }: { status: OcrStatus | null }) {
  const value = status?.status ?? "idle";
  const label = {
    idle: "未启动",
    running: "处理中",
    completed: "OCR 已完成",
    review_required: "待复核",
    blocked: "已阻断",
    failed: "失败",
    cancelled: "已取消",
  }[value];
  const Icon =
    value === "running"
      ? Loader2
      : value === "completed"
        ? CheckCircle2
        : value === "review_required"
          ? AlertTriangle
          : ["failed", "blocked", "cancelled"].includes(value)
            ? XCircle
            : RefreshCw;
  return (
    <span
      className={`inline-flex items-center gap-1.5 text-xs ${
        value === "completed"
          ? "text-emerald-700"
          : value === "review_required"
            ? "text-amber-700"
            : ["failed", "blocked"].includes(value)
              ? "text-red-700"
              : "text-zinc-500"
      }`}
    >
      <Icon size={14} className={value === "running" ? "animate-spin" : ""} />
      {label}
    </span>
  );
}

function ChapterOption({
  chapter,
  selected,
  onSelect,
}: {
  chapter: OcrChapter;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <label
      className={`block cursor-pointer border px-3 py-2 text-xs ${selected ? "border-emerald-500 bg-emerald-50" : "border-zinc-100 hover:bg-zinc-50"}`}
    >
      <input
        type="radio"
        name="ocr-chapter"
        checked={selected}
        onChange={() => onSelect(chapter.id)}
        className="mr-2"
      />
      {chapter.number} {chapter.title}
      <span className="ml-2 text-zinc-400">
        PDF 第 {chapter.page_start ?? "?"}-{chapter.page_end ?? "?"} 页
      </span>
      {chapter.needs_confirmation && <span className="ml-2 text-amber-700">需要确认</span>}
    </label>
  );
}

function NotePreview({ markdown }: { markdown: string }) {
  const parts = markdown.split(/```mermaid\n([\s\S]*?)```/g);
  return (
    <article className="mt-5 border-t border-zinc-200 pt-5">
      <h3 className="text-sm font-semibold">精读 Markdown 预览</h3>
      <div className="prose prose-zinc mt-3 max-w-none text-sm leading-7">
        {parts.map((part, index) =>
          index % 2 === 1 ? (
            <MermaidBlock key={index} code={part.trim()} />
          ) : (
            <ReactMarkdown key={index}>{part}</ReactMarkdown>
          ),
        )}
      </div>
    </article>
  );
}
```

- [ ] **步骤 5：运行测试验证通过**

运行：`npm test --prefix parsing-core-app -- --run src/api/ocrStatus.test.ts src/components/workbench/OcrWorkflowPanel.test.tsx src/api/workbench.test.ts`

再运行：`npm test --prefix parsing-core-app -- --run src/components/workbench/EnvironmentCard.test.tsx src/components/workbench/Settings.test.tsx`

预期：PASS。

- [ ] **步骤 6：Lint 与类型检查**

运行：

```bash
npm --prefix parsing-core-app run lint
npm --prefix parsing-core-app run typecheck
```

预期：无错误。

- [ ] **步骤 7：Commit**

```bash
git add parsing-core-app/src/api/workbenchTypes.ts parsing-core-app/src/api/workbench.ts parsing-core-app/src/api/ocrStatus.test.ts parsing-core-app/src/components/workbench/OcrWorkflowPanel.tsx parsing-core-app/src/components/workbench/OcrWorkflowPanel.test.tsx
git commit -m "feat(web): show review pending pages and resume review"
```

---

## 任务 7：跨层端到端验收（混合批次 → 复核升级 → 占位发布）

**文件：**
- 测试：`tests/test_workbench/test_ocr_workflow.py`、`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写混合批次夹具与工作流级用例**

（a）`tests/test_workbench/test_ocr_workflow.py` 顶部 import 调整为：

```python
from parsing_core.workbench.ocr.markdown_notes import (
    build_intensive_reading_note,
    validate_intensive_reading_note,
)
from parsing_core.workbench.ocr.orchestrator import BatchRun, BatchStatus, PageStatus
```

（b）在 `_review_workflow_fixture` 之后新增：

```python
class MixedReviewEngines(FakeEngines):
    def _vision(self, pdf_path, *, page, dpi, languages, **_control):
        result = super()._vision(pdf_path, page=page, dpi=dpi, languages=languages, **_control)
        if page == 2:
            block = result.observation["blocks"][0]
            block["text"] = "冲突文本"
            block["candidates"] = [{"text": "冲突文本", "confidence": 0.99}]
        return result

    def _transcribe(
        self, image_path, *, page_number, width, height, expected_image_sha256, **_control
    ):
        result = super()._transcribe(
            image_path,
            page_number=page_number,
            width=width,
            height=height,
            expected_image_sha256=expected_image_sha256,
            **_control,
        )
        if page_number == 2:
            result.payload["blocks"][0]["text"] = "一致文本"
        return result

    def _adjudicate(self, *args, page_number, **kwargs):
        result = super()._adjudicate(*args, page_number=page_number, **kwargs)
        if page_number == 1:
            result.payload["final_blocks"][0]["text"] = "1 战略管理"
        return result


def _mixed_review_fixture(tmp_path: Path):
    engines = MixedReviewEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    orchestrator.baidu = None
    result = orchestrator.run_batch(
        engines.pdf_path,
        pages=[1, 2],
        dpi=300,
        languages=["zh-Hans"],
        sample_rate=0,
    )
    assert result.status is BatchStatus.REVIEW_REQUIRED
    assert result.pages[1].status is PageStatus.COMPLETED
    assert result.pages[2].status is PageStatus.REVIEW_PENDING
    return engines, tmp_path / "ocr-state", result
```

（c）在文件末尾追加：

```python
def test_mixed_batch_isolates_only_conflict_pages(tmp_path: Path):
    _engines, state_root, _result = _mixed_review_fixture(tmp_path)

    final = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))

    assert final["status"] == "review_required"
    assert final["pages"]["1"]["status"] == "completed"
    assert final["pages"]["2"]["status"] == "review_pending"
    assert final["review_pages"] == [
        {"page": 2, "reason": "conflict", "alignment_status": "conflict"}
    ]


def test_review_note_marks_pending_pages_and_keeps_comment_contract(tmp_path: Path):
    _engines, state_root, _result = _mixed_review_fixture(tmp_path)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("review work must not rerun"),
    )
    final, pages = workflow.completed_evidence()
    tree = workflow.detect_chapters()
    confirmation = build_confirmation(tree, tree["chapters"][0]["id"])

    note = build_intensive_reading_note(
        tree,
        confirmation,
        pages,
        source_id="source-1",
        review_pending=len(final["review_pages"]),
    )

    assert "<!-- pdf2md: review_pending=1 -->" in note["markdown"]
    assert "<!-- pdf2md: review pending page 2 -->" in note["markdown"]
    assert "1 战略管理" in note["markdown"]
    assert note["metadata"]["review_pending"] == 1
    assert note["metadata"]["review_pages"] == [2]
    validate_intensive_reading_note(note)


def test_publish_review_note_records_pending_pages(tmp_path: Path):
    _engines, state_root, _result = _mixed_review_fixture(tmp_path)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("review work must not rerun"),
    )
    final, _pages = workflow.completed_evidence()
    tree = workflow.detect_chapters()
    confirmation = build_confirmation(tree, tree["chapters"][0]["id"])
    metadata = _note_metadata(final, tree, confirmation)
    metadata["review_pending"] = 1
    metadata["review_pages"] = [2]
    markdown = _valid_markdown(final, tree, confirmation).replace(
        "## 原文证据\n",
        "## 原文证据\n<!-- pdf2md: review pending page 2 -->\n",
    )
    markdown = "<!-- pdf2md: review_pending=1 -->\n" + markdown

    def generate(output_path: Path):
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    _note, artifact = workflow.generate_and_publish(
        generate,
        expected_final=final,
        expected_tree=tree,
        confirmation=confirmation,
    )

    assert "<!-- pdf2md: review pending page 2 -->" in artifact.read_text(encoding="utf-8")
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["review_pending"] == 1
    payload = workflow.status()
    assert payload["status"] == "review_required"
    assert payload["publishable"] is True
    assert payload["review_pages"] == [
        {"page": 2, "reason": "conflict", "alignment_status": "conflict"}
    ]
```

- [ ] **步骤 2：编写 API 级复核升级用例**

在 `tests/test_workbench/test_api.py` 顶部 import 区补充：

```python
from test_ocr_orchestrator import _orchestrator
from test_ocr_workflow import (
    _complete_workflow_fixture,
    _mixed_review_fixture,
    _note_metadata,
    _prepare_chapter_context,
    _review_workflow_fixture,
    _valid_markdown,
)
```

在文件末尾追加：

```python
def test_ocr_review_route_upgrades_mixed_review_final(tmp_path, monkeypatch):
    c = client(tmp_path)
    root = course_root(tmp_path)
    fixture_root = root / "ocr-fixture"
    fixture_root.mkdir()
    engines, state_root, _result = _mixed_review_fixture(fixture_root)
    _course, source = _registered_pdf_source(c, root, fixture_root / "book.pdf")
    workflow = OcrWorkflow(
        source_path=fixture_root / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: _orchestrator(fixture_root, engines),
    )
    monkeypatch.setattr(routes_workbench, "_ocr_workflow", lambda *args, **kwargs: workflow)
    monkeypatch.setattr(routes_workbench, "resolve_baidu_api_key", lambda: "baidu-key")

    response = c.post(f"/api/workbench/sources/{source['id']}/ocr/review")

    assert response.status_code == 200
    payload = _poll_ocr_status(c, source["id"], lambda value: value["status"] == "completed")
    assert payload["status"] == "completed"
    assert payload["review_pending"] == 0
    assert payload["review_pages"] is None
    final = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    assert final["status"] == "completed"
    assert final["pages"]["1"]["status"] == "completed"
    assert final["pages"]["2"]["status"] == "completed"
```

- [ ] **步骤 3：运行端到端用例**

运行：`.venv/bin/python -m pytest tests/test_workbench/test_ocr_workflow.py tests/test_workbench/test_api.py -q -k "mixed or review_note or publish_review_note or upgrades_mixed"`

预期：PASS。若失败，优先核对 `MixedReviewEngines` 的页 1/页 2 语义（页 1 为已终审章节页，页 2 为冲突隔离页）。

- [ ] **步骤 4：全量回归**

运行：

```bash
.venv/bin/python -m pytest tests/test_workbench/test_ocr_orchestrator.py tests/test_workbench/test_ocr_workflow.py tests/test_workbench/test_ocr_markdown_notes.py tests/test_workbench/test_deepseek_intensive_reading.py tests/test_workbench/test_api.py tests/test_workbench/test_environment.py tests/test_serving/test_scheduler.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：Lint、类型与前端门禁**

运行：

```bash
.venv/bin/ruff check src/parsing_core tests/test_workbench
.venv/bin/mypy src/parsing_core
npm --prefix parsing-core-app run lint
npm --prefix parsing-core-app run typecheck
npm test --prefix parsing-core-app -- --run src/api/ocrStatus.test.ts src/components/workbench/OcrWorkflowPanel.test.tsx
```

预期：无错误、PASS。

- [ ] **步骤 6：手工验收（本机，可选但建议）**

1. 不配置百度 Key 启动 OCR：完成后面板显示“待复核 N 页”与页码/原因列表，章节笔记 markdown 顶部含 `<!-- pdf2md: review_pending=N -->` 且隔离页位置含占位注释。
2. 在设置页保存百度 Key 后点击“配置百度 Key 并继续复核”：仅隔离页重跑（非隔离页不重新识别），成功后状态变为“OCR 已完成”。
3. 复核过程中取消：状态为已取消，隔离清单保留在 `batch-state.json`，再次点击复核会从头重跑隔离页（不消耗失败页 attempts 上限之外的次数）。

- [ ] **步骤 7：Commit**

```bash
git add tests/test_workbench/test_ocr_workflow.py tests/test_workbench/test_api.py
git commit -m "test(ocr): cover mixed review batch, publication and review resume"
```

---

## 规格自检

### 1. 规格覆盖度（对照 spec 5.4 / 6 / 7）

| 规格要求 | 对应任务 |
|---|---|
| 新增页级终态 `REVIEW_PENDING`（保留 `BAIDU_PENDING`） | 任务 1（枚举、`_run_page`） |
| 无百度时不猜测、不放行，记录 `review_reason ∈ {conflict, complex, sampled}` 与 `alignment_status` | 任务 1（`_mark_review_pending` 等价逻辑、`_review_reason`、隔离测试） |
| 新增批次终态 `REVIEW_REQUIRED`，非隔离页完成且闸门通过时写 final | 任务 1（完成块、`_review_final_is_valid`） |
| final schema 升级 + `review_pages` 清单 + `_review_final_is_valid` | 任务 1（schema v3、字段校验）；任务 2（`_review_ocr_final_is_valid`） |
| `status_payload` 对 `REVIEW_REQUIRED` 返回 `publishable: true` 与 `review_pages`；`WorkflowStatus` 新增值 | 任务 2 |
| 非隔离页正常发布、隔离页占位注释、合并 Markdown 顶部计数、final 与回执记录计数 | 任务 3（占位/顶部/元数据）、任务 4（回执校验）、任务 7（端到端） |
| `POST /api/workbench/sources/{source_id}/ocr/review`，前置 409 `ocr_review_not_ready`，只重跑隔离页、失败保持 REVIEW_REQUIRED | 任务 2（`start_review`）、任务 5（路由与错误码）；任务 1（复核失败回退隔离） |
| 工厂无 Key 时 `baidu=None`（替换 `WorkflowBlockedError`） | 任务 5 |
| 前端待复核清单 + “配置百度 Key 并继续复核” + `OcrStatus` 新字段 | 任务 6 |
| 恢复/中断：无 Key 续跑不重跑、不耗 attempts；有 Key 时重置执行 | 任务 1（循环分支与测试） |
| 硬不变量：隔离页不计入 COMPLETED、不进入正文、不满足完成校验 | 任务 1（`_completed_evidence_is_valid` 拒绝 REVIEW_PENDING、`_completed_final_for_request` 全页 COMPLETED 检查） |
| 旧 final 兼容（无 review 字段视为无隔离页）、旧断言迁移 | 任务 1（schema 2 读取兼容）、任务 2（旧 completed 用例保持）、任务 5（迁移 `baidu_key_missing` 断言） |

### 2. 占位符扫描

- 全文无 TODO / 待定 / “类似任务 N” / “补充错误处理”等空泛步骤。
- 每个实现步骤都带完整可粘贴代码或可定位的整函数替换；测试步骤都带完整用例与运行命令。
- 所有新类型、函数、常量第一次使用前都已在同一计划中定义：`REVIEW_REASONS`、`_review_reason`、`_review_final_is_valid`、`_review_final_for_request`、`_read_ocr_final`、`_review_ocr_final_is_valid`、`start_review`、`review_pending`/`review_pages`、`reviewSourceOcr`、`OcrReviewPage`。

### 3. 类型一致性

- 后端统一名称：`PageStatus.REVIEW_PENDING`（值 `"review_pending"`）、`BatchStatus.REVIEW_REQUIRED`（值 `"review_required"`）、`WorkflowStatus.REVIEW_REQUIRED`（值 `"review_required"`）。
- 状态载荷字段统一为 `review_pending: int` 与 `review_pages: [{"page": int, "reason": str, "alignment_status": str}]`；前端 `OcrReviewPage` 与之逐字段对应。
- 笔记 metadata 字段统一为 `review_pending`（全局计数）与 `review_pages`（章节内页码升序数组），并在 `markdown_notes.py`、`deepseek_intensive_reading.py`、`workflow.py`（发布校验）、`intensive-reading-note.json` 四处同名同序。
- 错误码统一为 `ocr_review_not_ready`（409，`detail = {"code", "params"}`），前端 `SafeApiError.code` 与 `errorMessages.ts` 既有映射一致。

### 4. 已知边界（计划内明确记录）

- `REVIEW_REQUIRED` 发布物是章节笔记：位于所有已确认章节页范围之外的隔离页只计入顶部计数与回执，不产生章节正文占位；这是现有“按章节发布”架构的边界。
- 复核期间取消/超时保留既有语义（`_finish` 丢弃 final、状态为 cancelled/failed）；页级证据失败（`_EvidenceBlocked`/引擎异常）才回退为 `REVIEW_PENDING` 并更新清单。这是为了维持“失败页不发布”的硬约束。
- 旧 v2 state/final 继续可读（`_is_batch_state` 接受 2 与 3）；legacy v1→v2 迁移产物仍写 `schema_version: 2`，不破坏既有迁移测试。

---

## 执行交接

计划保存于 `docs/superpowers/plans/2026-09-12-commercial-p0-a-ocr-review.md`。两种执行方式：

1. **子代理驱动（推荐）**：每个任务调度一个新子代理，任务间审查，使用 superpowers:subagent-driven-development。
2. **内联执行**：在当前会话使用 superpowers:executing-plans，按批执行并在任务间设审查检查点。

