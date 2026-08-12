# M1 持久任务与 BookPipeline 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 建立 SQLite 持久 Job 内核、领域状态机和唯一 `BookPipeline`，使导入、OCR、章节和精读任务在退出、崩溃、重复提交后仍可恢复且不会重复执行。

**架构：** 新建 `domain/application/ports/infrastructure` 边界，以版本化 Migration 和 SQLite 仓储保存 Job、Step、Event、Artifact、ProviderCall 和质量发现。旧 API 暂时保留 URL，但只调用应用服务；验证完成后删除进程内任务事实源。

**技术栈：** Python 3.12、dataclasses/StrEnum、Pydantic 2、SQLite WAL、FastAPI、pytest、pytest-repeat、Hypothesis、asyncio、文件锁。

---

## 文件清单

**创建：**

- `src/parsing_core/workbench/domain/states.py`：唯一 Job、Step、BookStage 和质量状态枚举。
- `src/parsing_core/workbench/application/__init__.py`、`commands/__init__.py`、`queries/__init__.py`、`pipelines/__init__.py`、`services/__init__.py`：应用包边界。
- `src/parsing_core/workbench/ports/__init__.py`：端口包边界。
- `src/parsing_core/workbench/infrastructure/__init__.py`、`filesystem/__init__.py`：基础设施包边界。
- `src/parsing_core/workbench/presentation/__init__.py`：展示层包边界。
- `src/parsing_core/workbench/domain/entities.py`：Job、JobStep、JobEvent、Artifact、ProviderCall 领域实体。
- `src/parsing_core/workbench/domain/value_objects.py`：ID、内容哈希、事件序号、租约值对象。
- `src/parsing_core/workbench/domain/errors.py`：稳定错误码和可重试分类。
- `src/parsing_core/workbench/ports/repositories.py`：Job、Artifact 和工作区仓储 Protocol。
- `src/parsing_core/workbench/ports/clock.py`：可测试时钟端口。
- `src/parsing_core/workbench/application/commands/jobs.py`：提交、暂停、继续、取消、重跑命令。
- `src/parsing_core/workbench/application/queries/jobs.py`：状态、事件和产物查询。
- `src/parsing_core/workbench/application/pipelines/book.py`：唯一 BookPipeline 图和阶段检查点。
- `src/parsing_core/workbench/application/services/job_service.py`：幂等提交与命令处理。
- `src/parsing_core/workbench/infrastructure/persistence/migrations.py`：事务 Migration 执行器。
- `src/parsing_core/workbench/infrastructure/persistence/sql/0001_baseline.sql`：登记现有结构版本。
- `src/parsing_core/workbench/infrastructure/persistence/sql/0002_jobs.sql`：持久任务结构。
- `src/parsing_core/workbench/infrastructure/persistence/job_repository.py`：SQLite 端口实现。
- `src/parsing_core/workbench/infrastructure/jobs/worker.py`：租约 Worker 与心跳。
- `src/parsing_core/workbench/infrastructure/jobs/runner.py`：有界 Worker 生命周期。
- `src/parsing_core/workbench/infrastructure/filesystem/workspace_lock.py`：工作区单写者锁。
- `src/parsing_core/workbench/infrastructure/filesystem/log_rotation.py`：正文脱敏、按大小/日期轮转与总额上限。
- `src/parsing_core/workbench/presentation/dto.py`：API DTO 映射。
- `src/parsing_core/serving/api/routes_jobs.py`：Job 命令和查询 API。
- `src/parsing_core/serving/api/routes_courses.py`：课程 API。
- `src/parsing_core/serving/api/routes_sources.py`：教材 API。
- `src/parsing_core/serving/api/routes_chapters.py`：章节 API。
- `tests/test_workbench/domain/test_job_state.py`：状态属性测试。
- `tests/test_workbench/infrastructure/test_migrations.py`：Migration 回滚与升级测试。
- `tests/test_workbench/infrastructure/test_job_repository.py`：租约、幂等、事件测试。
- `tests/test_workbench/application/test_job_service.py`：应用命令测试。
- `tests/test_workbench/application/test_book_pipeline.py`：阶段图测试。
- `tests/test_workbench/integration/test_job_recovery.py`：崩溃恢复集成测试。

**修改：**

- `pyproject.toml`、`uv.lock`：增加 Hypothesis 与 pytest-repeat。
- `src/parsing_core/workbench/schema.py`、`repository.py`：调用 Migration 并作为旧接口兼容门面。
- `src/parsing_core/serving/serve.py`、`api/__init__.py`：装配仓储、Worker 和新路由。
- `src/parsing_core/serving/api/routes_workbench.py`：删去直接磁盘/线程操作，转调应用服务。
- `src/parsing_core/serving/api/routes_ws.py`：从持久事件表补拉。
- `src/parsing_core/serving/scheduler.py`：只保留旧 Batch 兼容，移除 Workbench 事实状态。
- `src/parsing_core/workbench/ocr/workflow.py`：收缩为可调用步骤，不再拥有线程。
- `tests/test_workbench/test_api.py`、`tests/test_serving/test_shutdown.py`：兼容与关闭测试。

## 任务 1：定义唯一领域状态与合法转换

**文件：**
- 创建：`src/parsing_core/workbench/domain/__init__.py`
- 创建：`src/parsing_core/workbench/domain/states.py`
- 创建：`src/parsing_core/workbench/domain/errors.py`
- 创建：`tests/test_workbench/domain/test_job_state.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`

- [ ] **步骤 1：编写失败的状态机属性测试**

先将 `hypothesis>=6,<7` 与 `pytest-repeat>=0.9,<1` 加入 dev 依赖并刷新 `uv.lock`，使属性测试和并发重复测试均来自锁文件。

```python
from hypothesis import given, strategies as st
import pytest

from parsing_core.workbench.domain.errors import InvalidStateTransition
from parsing_core.workbench.domain.states import JobStatus, transition_job


@given(st.sampled_from(list(JobStatus)))
def test_terminal_job_states_never_leave_terminal(status: JobStatus) -> None:
    if status not in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
        return
    for target in JobStatus:
        if target is status:
            continue
        with pytest.raises(InvalidStateTransition):
            transition_job(status, target)


def test_quota_pause_can_only_resume_or_cancel() -> None:
    assert transition_job(JobStatus.PAUSED_BY_QUOTA, JobStatus.QUEUED) is JobStatus.QUEUED
    assert transition_job(JobStatus.PAUSED_BY_QUOTA, JobStatus.CANCELLED) is JobStatus.CANCELLED
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/domain/test_job_state.py -q`

预期：FAIL，模块尚不存在。

- [ ] **步骤 3：实现最小状态契约**

`states.py` 定义 `JobStatus`、`JobStepStatus`、`JobKind`、`BookStage`、`QualityDisposition`；所有值使用大写稳定字符串。`transition_job(current, target)` 使用显式转换集合，未知转换抛出 `InvalidStateTransition(code="job.invalid_transition")`。

核心状态：

```python
class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    PAUSED_BY_QUOTA = "PAUSED_BY_QUOTA"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
```

- [ ] **步骤 4：验证状态测试和 strict mypy**

运行：

```bash
uv run pytest tests/test_workbench/domain/test_job_state.py -q
uv run mypy --strict src/parsing_core/workbench/domain
```

预期：全部通过。

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml uv.lock src/parsing_core/workbench/domain tests/test_workbench/domain
git commit -m "feat: define durable job states"
```

## 任务 2：建立领域实体与值对象

**文件：**
- 创建：`src/parsing_core/workbench/domain/value_objects.py`
- 创建：`src/parsing_core/workbench/domain/entities.py`
- 创建：`tests/test_workbench/domain/test_entities.py`

- [ ] **步骤 1：编写失败的不可变实体测试**

```python
from dataclasses import FrozenInstanceError
import pytest

from parsing_core.workbench.domain.entities import Job
from parsing_core.workbench.domain.states import JobKind, JobStatus


def test_job_identity_and_payload_are_immutable() -> None:
    job = Job.new(
        kind=JobKind.BOOK_IMPORT,
        workspace_id="ws-1",
        subject_id="source-1",
        idempotency_key="sha256:abc",
        payload={"source_id": "source-1"},
        now_ms=1_000,
    )
    assert job.status is JobStatus.QUEUED
    with pytest.raises(FrozenInstanceError):
        job.subject_id = "source-2"  # type: ignore[misc]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/domain/test_entities.py -q`

预期：FAIL，实体尚不存在。

- [ ] **步骤 3：实现实体与严格构造**

使用 `@dataclass(frozen=True, slots=True)` 定义 `Job`、`JobStep`、`JobEvent`、`Artifact`、`ProviderCall`。ID 使用 `uuid.uuid4().hex`，正文载荷通过只读 JSON 值类型保存；`ContentHash.from_bytes()` 生成 `sha256:<hex>`。

- [ ] **步骤 4：验证实体测试**

运行：

```bash
uv run pytest tests/test_workbench/domain -q
uv run mypy --strict src/parsing_core/workbench/domain
```

预期：全部通过。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/domain tests/test_workbench/domain
git commit -m "feat: add immutable job entities"
```

## 任务 3：引入事务 Migration 和持久表

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/migrations.py`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/sql/0001_baseline.sql`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/sql/0002_jobs.sql`
- 创建：`tests/test_workbench/infrastructure/test_migrations.py`
- 修改：`src/parsing_core/workbench/schema.py`

- [ ] **步骤 1：编写失败的升级和回滚测试**

```python
def test_migrations_are_atomic_and_repeatable(tmp_path):
    db = tmp_path / "workspace.db"
    apply_migrations(db)
    first = schema_versions(db)
    apply_migrations(db)
    assert schema_versions(db) == first == [1, 2]
    assert table_names(db) >= {
        "wb_jobs", "wb_job_steps", "wb_job_events", "wb_artifacts",
        "wb_provider_calls", "wb_quality_findings", "wb_workspace_locks",
    }
```

另加损坏 SQL fixture，断言失败时版本号和表结构均不前进。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/infrastructure/test_migrations.py -q`

预期：FAIL，Migration 执行器不存在。

- [ ] **步骤 3：实现 Migration 与表约束**

`0002_jobs.sql` 至少包含：

```sql
CREATE TABLE wb_jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  error_code TEXT,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
CREATE TABLE wb_job_steps (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES wb_jobs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  lease_owner TEXT,
  lease_expires_at_ms INTEGER,
  next_attempt_at_ms INTEGER,
  UNIQUE(job_id, ordinal)
);
CREATE TABLE wb_job_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES wb_jobs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
```

其余表使用外键、唯一键和索引；Migration 在 `BEGIN IMMEDIATE` 中执行并记录 SHA-256，禁止修改已应用 SQL。

- [ ] **步骤 4：验证 Migration 和旧数据库升级**

运行：

```bash
uv run pytest tests/test_workbench/infrastructure/test_migrations.py tests/test_workbench/test_schema.py -q
```

预期：新数据库、现有 fixture、重复执行和失败回滚全部通过。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/persistence src/parsing_core/workbench/schema.py tests/test_workbench/infrastructure
git commit -m "feat: add transactional workspace migrations"
```

## 任务 4：实现 JobRepository 的幂等、租约和事件序号

**文件：**
- 创建：`src/parsing_core/workbench/ports/repositories.py`
- 创建：`src/parsing_core/workbench/ports/clock.py`
- 创建：`src/parsing_core/workbench/ports/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/job_repository.py`
- 创建：`tests/test_workbench/infrastructure/test_job_repository.py`

- [ ] **步骤 1：编写失败的并发领取测试**

```python
def test_only_one_worker_can_lease_a_ready_step(repository, queued_job, clock):
    first = repository.claim_next_step("worker-a", clock.now_ms(), lease_ms=30_000)
    second = repository.claim_next_step("worker-b", clock.now_ms(), lease_ms=30_000)
    assert first is not None
    assert second is None


def test_expired_lease_is_reclaimed_without_new_provider_call(repository, running_step, clock):
    clock.advance_ms(30_001)
    reclaimed = repository.claim_next_step("worker-b", clock.now_ms(), lease_ms=30_000)
    assert reclaimed.id == running_step.id
    assert reclaimed.attempt == running_step.attempt + 1
```

另测相同 `idempotency_key` 返回同一 Job，事件 `seq` 严格递增。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/infrastructure/test_job_repository.py -q`

预期：FAIL，仓储尚不存在。

- [ ] **步骤 3：实现短事务 SQLite 仓储**

端口必须提供 `submit_job`、`get_job`、`claim_next_step`、`heartbeat`、`complete_step`、`fail_step`、`append_event`、`events_after`、`request_cancel`。连接启用：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

领取使用单个 `BEGIN IMMEDIATE` 事务和条件更新，任何 Provider 网络调用都不得位于数据库事务内。

事件按 Job 保留完整终态摘要和最近 10,000 条明细；超过上限时只压缩已完成 Job 的进度事件，错误、计费、质量、发布和用户操作事件永久保留。仓储测试必须断言清理后 `seq` 仍单调且补拉返回 `events.compacted` 边界事件。

- [ ] **步骤 4：验证仓储与并发压力**

运行：

```bash
uv run pytest tests/test_workbench/infrastructure/test_job_repository.py -q
uv run pytest tests/test_workbench/infrastructure/test_job_repository.py -q --count=20
```

预期：重复运行无间歇失败，同一步不会被两个 Worker 同时领取。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/ports src/parsing_core/workbench/infrastructure/persistence/job_repository.py tests/test_workbench/infrastructure/test_job_repository.py
git commit -m "feat: persist idempotent job leases"
```

## 任务 5：实现 Worker、心跳、取消和崩溃恢复

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/jobs/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/filesystem/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/jobs/worker.py`
- 创建：`src/parsing_core/workbench/infrastructure/jobs/runner.py`
- 创建：`src/parsing_core/workbench/infrastructure/filesystem/workspace_lock.py`
- 创建：`src/parsing_core/workbench/infrastructure/filesystem/log_rotation.py`
- 创建：`tests/test_workbench/infrastructure/test_log_rotation.py`
- 创建：`tests/test_workbench/integration/test_job_recovery.py`
- 修改：`src/parsing_core/serving/serve.py`

- [ ] **步骤 1：编写失败的崩溃恢复测试**

```python
async def test_restart_reclaims_expired_step_and_reuses_checkpoint(harness):
    job_id = harness.submit_book_job()
    await harness.worker.run_one(crash_after_checkpoint=True)
    harness.clock.advance_ms(31_000)
    restarted = harness.restart()
    await restarted.run_until_idle()
    job = restarted.repository.get_job(job_id)
    assert job.status is JobStatus.COMPLETED
    assert restarted.provider.calls == 1
```

另测暂停、继续、取消和两个 App 争夺同一工作区锁。

日志测试写入 100 MiB 合成消息，断言单文件不超过 10 MiB、总额不超过 100 MiB、最多保留 14 天，Secret、正文和绝对课程路径均被替换为错误码或哈希。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/integration/test_job_recovery.py -q`

预期：FAIL，Worker 和锁尚不存在。

- [ ] **步骤 3：实现受限 Worker 生命周期**

Worker 每 10 秒续租 30 秒租约；每次开始副作用前写入输入哈希，完成后原子保存产物和检查点。取消仅在安全检查点生效；进程启动时把过期 `RUNNING` 步骤转回 `READY`。`WorkspaceLock` 使用锁文件 PID、进程启动时间和随机实例 ID 防止 PID 复用误判。

- [ ] **步骤 4：验证崩溃、取消和双实例**

运行：

```bash
uv run pytest tests/test_workbench/integration/test_job_recovery.py tests/test_workbench/infrastructure/test_log_rotation.py tests/test_serving/test_shutdown.py -q
```

预期：全部通过，恢复时间测试小于 10 秒，重复 Provider 调用数保持 1。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/jobs src/parsing_core/workbench/infrastructure/filesystem src/parsing_core/serving/serve.py tests/test_workbench/integration tests/test_workbench/infrastructure/test_log_rotation.py
git commit -m "feat: recover durable jobs after restart"
```

## 任务 6：建立唯一 BookPipeline 和应用命令

**文件：**
- 创建：`src/parsing_core/workbench/application/commands/jobs.py`
- 创建：`src/parsing_core/workbench/application/__init__.py`
- 创建：`src/parsing_core/workbench/application/commands/__init__.py`
- 创建：`src/parsing_core/workbench/application/queries/__init__.py`
- 创建：`src/parsing_core/workbench/application/pipelines/__init__.py`
- 创建：`src/parsing_core/workbench/application/services/__init__.py`
- 创建：`src/parsing_core/workbench/application/queries/jobs.py`
- 创建：`src/parsing_core/workbench/application/services/job_service.py`
- 创建：`src/parsing_core/workbench/application/pipelines/book.py`
- 创建：`tests/test_workbench/application/test_job_service.py`
- 创建：`tests/test_workbench/application/test_book_pipeline.py`

- [ ] **步骤 1：编写失败的流水线图测试**

```python
def test_book_pipeline_has_one_ordered_stage_graph() -> None:
    pipeline = BookPipeline.default()
    assert [step.stage for step in pipeline.steps] == [
        BookStage.INGEST,
        BookStage.NORMALIZE,
        BookStage.OCR,
        BookStage.CHAPTERS,
        BookStage.READING,
        BookStage.TOPICS,
        BookStage.WRITING,
        BookStage.EXPORT,
    ]
    assert len({step.ordinal for step in pipeline.steps}) == len(pipeline.steps)
```

应用服务测试断言相同课程、来源哈希和配置版本只创建一个 Job。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application -q`

预期：FAIL，应用模块不存在。

- [ ] **步骤 3：实现流水线定义和命令处理**

`BookPipeline.default()` 返回不可变步骤规范；每一步声明输入 Artifact kind、输出 Artifact kind、可重试错误码和最大尝试数。`JobService.submit_book()` 使用 `sha256(source_hash + config_hash + pipeline_version)` 作为幂等键，并返回现有或新 Job。

- [ ] **步骤 4：验证应用层和依赖边界**

运行：

```bash
uv run pytest tests/test_workbench/application -q
uv run mypy --strict src/parsing_core/workbench/domain src/parsing_core/workbench/application src/parsing_core/workbench/ports
./scripts/verify-architecture.sh
```

预期：全部通过，应用层没有 FastAPI、SQLite 或 Provider SDK 导入。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application tests/test_workbench/application
git commit -m "feat: add canonical book pipeline"
```

## 任务 7：拆分路由并让旧 URL 进入新内核

**文件：**
- 创建：`src/parsing_core/workbench/presentation/dto.py`
- 创建：`src/parsing_core/workbench/presentation/__init__.py`
- 创建：`src/parsing_core/serving/api/routes_jobs.py`
- 创建：`src/parsing_core/serving/api/routes_courses.py`
- 创建：`src/parsing_core/serving/api/routes_sources.py`
- 创建：`src/parsing_core/serving/api/routes_chapters.py`
- 修改：`src/parsing_core/serving/api/__init__.py`
- 修改：`src/parsing_core/serving/api/routes_workbench.py`
- 修改：`src/parsing_core/serving/api/routes_ws.py`
- 修改：`src/parsing_core/serving/serve.py`
- 修改：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写失败的 202 契约测试**

```python
def test_source_ocr_endpoint_submits_durable_job(auth_client, source_id):
    response = auth_client.post(f"/api/workbench/sources/{source_id}/ocr")
    assert response.status_code == 202
    payload = response.json()
    assert payload["job_id"]
    assert payload["status"] == "QUEUED"
```

另测 `GET /api/jobs/{id}`、`POST /pause`、`/resume`、`/cancel`、`GET /events?after_seq=`，以及旧 URL 不再创建线程。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/test_api.py -q`

预期：FAIL，当前 OCR 路由同步返回 workflow 状态。

- [ ] **步骤 3：实施 DTO、路由和兼容门面**

所有命令端点返回 `202 Accepted` 和 `JobResponse`；查询只调用 QueryService。`routes_workbench.py` 保留旧 URL 导入和转发，不再调用 `_import_sources_sync`、`threading.Thread` 或磁盘函数。WebSocket 只推送 `seq`，断线客户端从事件 API 补拉。

- [ ] **步骤 4：验证 API 契约和无直接副作用**

运行：

```bash
uv run pytest tests/test_workbench/test_api.py tests/test_serving/test_api_ws.py -q
uv run pytest tests/test_architecture.py -q
```

预期：全部通过；架构测试确认 API 不导入 OCR Provider 或直接写文件模块。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/presentation src/parsing_core/serving tests/test_workbench/test_api.py tests/test_serving/test_api_ws.py
git commit -m "refactor: route workbench through job services"
```

## 任务 8：移除 Workbench 进程内事实源并完成耐久性验收

**文件：**
- 修改：`src/parsing_core/serving/scheduler.py`
- 修改：`src/parsing_core/workbench/ocr/workflow.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 修改：`src/parsing_core/workbench/schema.py`
- 创建：`tests/test_workbench/integration/test_book_job_e2e.py`

- [ ] **步骤 1：编写失败的进程边界 E2E**

测试使用两个独立 Python 进程：第一个提交任务并在步骤检查点后强制退出；第二个打开同一工作区并运行至完成。断言 Job ID、事件序号、Artifact 哈希稳定，Provider 副作用只发生一次。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/integration/test_book_job_e2e.py -q`

预期：旧 `OcrWorkflow` 线程或 Scheduler 内存状态导致恢复断言失败。

- [ ] **步骤 3：删除重复状态所有权**

从 `OcrWorkflow` 删除 `_thread`、`_status` 和 `start()`；保留纯步骤函数供 Worker 调用。从 Scheduler 删除 Workbench `_buffers`、`_cancelled` 和订阅状态；旧 Batch API 仍保留自己的兼容数据。`Repository` 旧方法转调新仓储并标记内部弃用，不新增第二套表。

- [ ] **步骤 4：执行 M1 全量验收**

运行：

```bash
uv run pytest tests/test_workbench/domain tests/test_workbench/application tests/test_workbench/infrastructure tests/test_workbench/integration -q
./scripts/verify-fast.sh
./scripts/verify-architecture.sh
```

预期：全部通过；进程重启恢复不超过 10 秒；100 次重复提交只产生一个有效 Job。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/serving/scheduler.py src/parsing_core/workbench/ocr/workflow.py src/parsing_core/workbench/repository.py src/parsing_core/workbench/schema.py tests/test_workbench/integration/test_book_job_e2e.py
git commit -m "refactor: remove in-memory workbench state"
```

## M1 完成收据

- [ ] Job、Step、Event、Artifact、ProviderCall 和质量发现均由 SQLite 持久化。
- [ ] 暂停、继续、取消、失败重试、崩溃恢复和事件补拉测试全绿。
- [ ] 同一幂等键不会重复执行 Provider 或重复计费。
- [ ] API 不直接访问磁盘、SQLite 或 Provider。
- [ ] 旧 URL 行为兼容，但所有 Workbench 任务只进入一个 `BookPipeline`。
