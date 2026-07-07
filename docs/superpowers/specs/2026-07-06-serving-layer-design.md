# 任务调度 + REST/WS 通信层 (#3) 设计规格

**日期**: 2026-07-06
**子项目**: #3 任务调度 + REST/WS 通信层
**路径**: 地基优先（方案 A）的第二步
**状态**: 已批准，待实现计划

---

## 1. 目标与非目标

### 1.1 目标
构建一个可独立启动的长驻 Python 服务（`python -m parsing_core.serve`），把 #2 的 CLI orchestrator 升级为支持批量提交、并发调度、长连接状态推送与断线续传的本地 HTTP/WS 服务。

1. **服务入口**：`python -m parsing_core.serve` 拉起 uvicorn 监听 127.0.0.1:8000
2. **批量提交**：`POST /api/batches` 接受 N 个本地文件绝对路径 + concurrency + priority（预留）
3. **并发池**：`asyncio.Semaphore` 双层限流（全局上限 + batch 内 concurrency），任务体内 `asyncio.to_thread(orchestrator.parse_file, ...)` 调用 #2 orchestrator
4. **WebSocket 状态机**：`ws://host/ws/batch/{batch_id}` 多路复用推该批次所有子任务事件，事件含 `seq` 单调递增、`task_id` 区分
5. **断线续传**：客户端 `?since=<last_seq>` 重连，服务端 from in-memory ring buffer 重放 `seq > since` 事件；batch 完成 30 min 后过期 410 Gone
6. **取消**：`DELETE /api/batches/{id}` 取消未启动 task；已启动 task 完成当前节后退出
7. **DB 隔离**：`serve.db` 与 CLI `core.db` 分离；FS 目录 `parsing-core-serve/` 与 CLI `parsing-core/` 分离
8. **CLI 不变**：保留前序 CLI 入口与 73 个测试全绿

### 1.2 非目标（本子项目不做）
- Tauri 外壳与 Sidecar 生命周期（#1）
- LiteLLM 三档算力路由与 Prompt 缓存（#4）—— 仍用 StubLLMClient
- WebUI 渲染与虚拟滚动（#5）—— 只产出 JSON/WS 事件
- DAG 依赖调度与优先级抢占（优先级字段先加占位，FIFO 顺序）
- 进程级 CPU 并行（asyncio.to_thread 受 GIL 限制，但本场景以 IO/LLM 为主，GIL 影响小）
- 服务重启后 in-memory 调度上下文自动恢复（客户端降级到 REST 拿最终状态）

---

## 2. 架构

### 2.1 进程拓扑

```
┌──────────────── Tauri 主进程（#1 未来）┃ Rust ────────────┐
│  PID 心跳守护、子进程拉起                            │
└────────────┬─────────────────────────────────────────────┘
             │ stdin/stdout 启停 + 监听 127.0.0.1:8000
             ▼
┌─────────────────────────────────────────────────────────┐
│  Python Sidecar（本子项目 #3）                           │
│  python -m parsing_core.serve → uvicorn                  │
│                                                          │
│  ┌────────────┐   ┌────────────┐   ┌──────────────────┐ │
│  │ REST 路由  │   │ WS 路由    │   │ 调度器 Scheduler │ │
│  │ /api/...   │   │ /ws/batch  │   │ asyncio.Semaphore│ │
│  └─────┬──────┘   └─────┬──────┘   └────────┬─────────┘ │
│        │                │                   │           │
│        ▼                ▼                   ▼           │
│  ┌──────────────────────────────────────────────────┐    │
│  │  共用：Orchestrator（来自 #2）                   │    │
│  │  + Repository / Schema / CacheService           │    │
│  │  + StubLLMClient（#4 再换）                     │    │
│  └──────────────────────────────────────────────────┘    │
│                       │                                  │
│                       ▼                                  │
│            ┌──────────────────────┐                      │
│            │ serve.db (SQLite WAL)│                      │
│            └──────────────────────┘                      │
│                       │                                  │
│                       ▼                                  │
│            ┌──────────────────────┐                      │
│            │ parsing-core-serve/  │                      │
│            │   {batch_id}/{task_id} │                    │
│            └──────────────────────┘                      │
└───────────────────────────────────────────────────────────┘
```

### 2.2 模块结构

```
src/parsing_core/
  serving/
    __init__.py
    serve.py            # uvicorn 入口 + FastAPI app 工厂
    config.py           # 端口、TTL、并发默认值
    scheduler.py        # Scheduler 类 + BatchContext
    ring_buffer.py      # 事件 ring buffer (deque(maxlen=10000))
    ws_manager.py       # WebSocket 连接管理 + since replay
    models/
      __init__.py
      api.py            # Pydantic V2 schemas
    api/
      __init__.py
      routes_batches.py
      routes_tasks.py
      routes_ws.py
      deps.py           # Scheduler 依赖注入
  storage/
    schema_ext.py       # apply_serve_schema(conn)：追加 batches 表 + ALTER tasks.batch_id
tests/
  test_serving/
    test_scheduler.py
    test_ring_buffer.py
    test_ws_manager.py
    test_api_batches.py
    test_api_tasks.py
    test_api_ws.py
    test_serve_e2e.py
```

### 2.3 调用关系

```
serve.py
  └─ build_app() -> FastAPI
       ├─ lifespan: 启动 Scheduler 单例
       └─ include_router(batches, tasks, ws)
routes_batches.create_batch(files, concurrency, priority)
  └─ scheduler.submit_batch(...)  # 建 batch 行 + N task 行 + asyncio.create_task(_run_task)
       └─ _run_task(batch_id, task_id, file_path)
            ├─ await global_sem + batch_sem
            ├─ asyncio.to_thread(orchestrator.parse_file, ...)
            └─ orchestrator.on_progress = scheduler.inject_callback → emit event
routes_ws.ws_batch(batch_id, since)
  └─ ws_manager.subscribe(batch_id, ws, since)
       └─ replay seq > since from ring buffer + 加入 live subscribers
```

---

## 3. 数据模型

### 3.1 schema_ext.py

```python
SCHEMA_EXT_SQL = """
CREATE TABLE IF NOT EXISTS batches (
  id              TEXT PRIMARY KEY,
  status          TEXT NOT NULL,
  concurrency     INTEGER NOT NULL DEFAULT 4,
  policy          TEXT NOT NULL DEFAULT 'parallel',
  priority        INTEGER NOT NULL DEFAULT 0,
  total_tasks     INTEGER NOT NULL DEFAULT 0,
  completed_tasks INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,
  finished_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_batch_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_task_batch ON tasks(batch_id);
"""

# tasks 表新增 batch_id 列（IF NOT EXISTS 通过 PRAGMA table_info 探测后 ALTER）
ALTER_TASKS_SQL = "ALTER TABLE tasks ADD COLUMN batch_id TEXT REFERENCES batches(id) ON DELETE SET NULL"


def apply_serve_schema(conn):
    conn.executescript(SCHEMA_EXT_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "batch_id" not in cols:
        conn.execute(ALTER_TASKS_SQL)
    conn.commit()
```

沿用 #2 的 init_db，serve 入口先 init_db 再 apply_serve_schema。

### 3.2 Pydantic 模型（serving/models/api.py）

```python
from pydantic import BaseModel, Field

class BatchCreateRequest(BaseModel):
    files: list[str] = Field(..., min_length=1)
    concurrency: int = Field(4, ge=1, le=32)
    priority: int = 0  # 预留 FIFO

class BatchResponse(BaseModel):
    batch_id: str
    task_ids: list[str]
    accepted: int
    rejected: int

class BatchStatus(BaseModel):
    batch_id: str
    status: str
    total_tasks: int
    completed_tasks: int
    tasks: list[dict]

class TaskCreateRequest(BaseModel):
    file_path: str
    model_tier: str = "stub"  # 预留

class TaskStatus(BaseModel):
    task_id: str
    batch_id: str | None
    status: str
    sections: int
    completed: int
    error_msg: str | None

class WSEvent(BaseModel):
    seq: int
    batch_id: str
    task_id: str | None = None
    event: str  # BATCH_STATE|TASK_STATE|SECTION_STATE|LLM_TOKEN|BATCH_DONE|ERROR
    payload: dict
    ts: int
```

---

## 4. 核心算法

### 4.1 Scheduler

```python
class Scheduler:
    def __init__(self, orch_factory, max_global_concurrency=8):
        self._orch_factory = orch_factory
        self._global_sem = asyncio.Semaphore(max_global_concurrency)
        self._batches = {}    # batch_id -> BatchContext
        self._buffers = {}     # batch_id -> deque(maxlen=10000)
        self._subscribers = {} # batch_id -> set[WebSocket]
        self._seq_counters = {} # batch_id -> next seq
        self._cancelled = set() # batch_ids flagged for cancel

    async def submit_batch(self, files, concurrency, priority) -> BatchResponse:
        batch_id = str(uuid.uuid4())
        task_ids = []
        # 写 batches 行 + N 个 tasks 行（同步 to_thread）
        for path in files:
            task_id = str(uuid.uuid4())
            task_ids.append(task_id)
            # DB 写入由 orchestrator 在 _run_task 内 create_task 完成
            # 此处仅创建 batch 行 + 草拟 task 清单（用临时表或仅内存清单）
        # Batch 上下文
        batch_sem = asyncio.Semaphore(concurrency)
        self._batches[batch_id] = BatchContext(sem=batch_sem, total=len(files))
        self._buffers[batch_id] = collections.deque(maxlen=10000)
        self._subscribers[batch_id] = set()
        self._seq_counters[batch_id] = 0
        # 排任务
        for path, task_id in zip(files, task_ids):
            asyncio.create_task(self._run_task(batch_id, task_id, path))
        return BatchResponse(batch_id=batch_id, task_ids=task_ids, accepted=len(task_ids), rejected=0)

    async def _run_task(self, batch_id, task_id, file_path):
        if batch_id in self._cancelled:
            self._emit(batch_id, WSEvent(..., event="TASK_STATE", payload={"status": "CANCELLED"}))
            return
        async with self._global_sem:
            ctx = self._batches[batch_id]
            async with ctx.sem:
                orch = self._orch_factory()
                # 注入进度回调
                async def on_progress(task_id_, kind, payload):
                    await self._emit(batch_id, WSEvent(..., event=kind, payload=payload, task_id=task_id_))
                orch.on_progress = on_progress
                try:
                    await asyncio.to_thread(orch.parse_file, file_path)
                except Exception as e:
                    await self._emit(batch_id, WSEvent(..., event="ERROR", payload={"error": str(e)}, task_id=task_id))
                finally:
                    ctx.completed += 1
                    if ctx.completed >= ctx.total:
                        await self._finalize_batch(batch_id)

    async def _emit(self, batch_id, event):
        event.seq = self._seq_counters[batch_id]
        self._seq_counters[batch_id] += 1
        self._buffers[batch_id].append(event)
        subs = list(self._subscribers.get(batch_id, ()))
        for ws in subs:
            try:
                await ws.send_text(event.model_dump_json())
            except Exception:
                self._subscribers[batch_id].discard(ws)
```

### 4.2 on_progress 回调注入（orchestrator 修订）

`Orchestrator.__init__` 新增 `on_progress: Callable[[str, str, dict], Awaitable[None]] | None = None`。每次 `repo.update_task_status` 前后调一次。本子项目只 emit 四种事件：
- TASK_STATE（状态变化）
- SECTION_STATE（节 AI 完成）
- BATCH_DONE（batch 终止）
- ERROR

### 4.3 ws_manager 与 ring buffer replay

```python
class WsManager:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    async def subscribe(self, batch_id, ws, since=0):
        # 注册订阅
        self.scheduler._subscribers.setdefault(batch_id, set()).add(ws)
        # 重放 seq > since
        buffer = self.scheduler._buffers.get(batch_id, ())
        for event in list(buffer):
            if event.seq > since:
                await ws.send_text(event.model_dump_json())
        # 若 batch 已完成 + ring buffer 过期，返回 410
        if batch_id not in self.scheduler._buffers and batch_id not in self.scheduler._batches:
            await ws.close(code=410, reason="Batch gone")
            return
        # 否则保持连接等新事件（scheduler._emit 自动 broadcast）

    def unsubscribe(self, batch_id, ws):
        self.scheduler._subscribers.get(batch_id, set()).discard(ws)
```

### 4.4 取消

`DELETE /api/batches/{id}` → scheduler._cancelled.add(batch_id) → batch 内未启动 _run_task 检测 cancelled 直接 emit CANCELLED 退出；已启动 task 自然完成（asyncio 任务被 cancel 时，to_thread 内同步线程无法立即停，等当前节结束 WebSocket 自动关闭后下次 await 检测 cancelled 退出）。

### 4.5 错误与崩溃

| 场景 | 处理 |
|---|---|
| 单任务异常 | task.status=FAILED；emit ERROR；batch 继续；终态若部分失败 batch=PARTIAL |
| WS 断线 | 自动 unsubscribe；ring buffer 继续累积；客户端 ?since 重连 |
| 服务重启 | in-memory 上下文丢失；batch 进行中 task 由用户手动 resume 推进；客户端重连接 410 后降级 GET /api/batches/{id} |
| DB BUSY | WAL 模式 mitigate；若真发生抛 OperationalError，task=FAILED |
| cancel 传播 | asyncio 任务取消语义；接受"当前节完成后退出"的延迟 |

---

## 5. REST/WS API 契约

### 5.1 REST

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/batches` | POST | 创建批次。body `{files, concurrency?, priority?}` → `BatchResponse` |
| `/api/tasks` | POST | 单文件入口。body `{file_path, model_tier?}` → `BatchResponse` (size=1) |
| `/api/batches` | GET | 列出所有批次。`?status=RUNNING` 可过滤 → `[BatchStatus]` |
| `/api/batches/{id}` | GET | 查批次详情 → `BatchStatus` |
| `/api/batches/{id}` | DELETE | 取消批次。未启动 task 取消、已启动等其完成 → `{cancelled: N}` |
| `/api/tasks/{id}` | GET | 查任务详情 → `TaskStatus` |
| `/api/tasks/{id}` | DELETE | 清理任务资产（调 orch.purge） → `{purged: true}` |
| `/api/tasks/{id}/merged` | GET | 下载 merged.md。`text/markdown` |
| `/health` | GET | `{status: "ok"}` |

### 5.2 WebSocket

```
ws://127.0.0.1:8000/ws/batch/{batch_id}?since={last_seq}
```

事件 JSON 一行一条：
```json
{"seq": 42, "batch_id": "...", "task_id": "...", "event": "TASK_STATE", "payload": {"status": "LLM_RUNNING"}, "ts": 1783331520}
```

事件枚举：
- `BATCH_STATE` — batch 状态变化
- `TASK_STATE` — task 状态变化（PARSING/SECTIONING/LLM_RUNNING/MERGING/COMPLETED/FAILED）
- `SECTION_STATE` — section AI 完成 + section_seq
- `LLM_TOKEN` — 流式 token（预留给 #4）
- `BATCH_DONE` — 终止信号
- `ERROR` — 任务错误

### 5.3 断线续传

- 服务端 ring buffer: `collections.deque(maxlen=10000)`
- 客户端传 `?since=N`
- 服务端 replay `seq > N`
- batch 完成后 ring buffer 保留 30 min（`SERVE_BUFFER_TTL_SEC = 1800`）
- 过期后重连 → close 410 + reason "batch gone"
- 重启后 buffer 丢失，重连走降级 → REST GET 拿最终状态

---

## 6. 服务配置

```python
# serving/config.py
HOST = "127.0.0.1"
PORT = 8000
MAX_GLOBAL_CONCURRENCY = 8
DEFAULT_BATCH_CONCURRENCY = 4
RING_BUFFER_MAX = 10000
SERVE_BUFFER_TTL_SEC = 1800  # 30 min
SERVE_DB_NAME = "serve.db"
SERVE_FS_DIRNAME = "parsing-core-serve"
```

`serve.py` 用 argparse 暴露 `--port`、`--host`、`--global-concurrency`，默认值取 config.py。

---

## 7. CLI 与服务的关系

- CLI 不修改：`parsing-core parse <file>` 仍直接 `Orchestrator(...).parse_file(...)`
- 服务：`parsing-core serve`（新子命令）启动 uvicorn。或独立模块 `python -m parsing_core.serve`
- 两入口 DB/FS 隔离：CLI 用 `core.db` + `parsing-core/` 目录；serve 用 `serve.db` + `parsing-core-serve/` 目录
- orchestrator.on_progress 字段是 Optional，CLI 调用时为 None（前 73 测试不变）

---

## 8. 测试策略

### 8.1 测试分层

| 层 | 工具 | 目标 |
|---|---|---|
| 单元 | pytest | Scheduler 内部、ring_buffer、ws_manager |
| 集成 | httpx.AsyncClient + pytest-asyncio | REST CRUD |
| 集成 | starlette TestWebSocketSession | WS 事件流、subscribe/unsubscribe |
| E2E | 真起 uvicorn + httpx | 完整 batch → WS 接收 → REST 查状态 |

### 8.2 关键测试（≥ 30 新增，全量 ≥ 100）

1. POST /api/batches：单文件、5 文件、10 文件并发、不存在的文件路径（rejected）、concurrency 限流
2. POST /api/tasks 单文件→自动 batch size=1
3. GET /api/batches/{id} 详情
4. GET /api/batches?status=RUNNING 过滤
5. DELETE /api/batches/{id} 取消
6. GET /api/tasks/{id}
7. DELETE /api/tasks/{id} purge
8. GET /api/tasks/{id}/merged 下载
9. GET /health
10. WS 握手 → BATCH_STATE RUNNING → N 个 TASK_STATE → BATCH_DONE
11. WS since=N 重放
12. WS 410 Gone（monkeypatch 改短 TTL）
13. WS 取消 → CANCELLED 事件
14. WS 解析失败 → ERROR + PARTIAL
15. ring_buffer 单元测试（maxlen、replay、过期清理）
16. Scheduler 单元：emit seq 单调、broadcast 多订阅者、unsubscribe 死连接清理
17. DB 隔离：CLI 与服务同时跑不冲突
18. orchestrator.on_progress 字段为 None 时不报错（CLI 兼容）

### 8.3 性能基线（非门禁仅记录）

- 100 个 sample.md 并发 submitting 在 concurrency=4 下 < 30s
- 1000 个 WS 事件 ring buffer 内存占用 < 10MB

---

## 9. 依赖与文件清单

### 9.1 依赖增量

```toml
[project.optional-dependencies]
serve = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "websockets>=12.0",
]
dev = [
  # 原有 ...
  "httpx>=0.27",
  "pytest-asyncio>=0.23",
]
```

### 9.2 新增文件（16 个源文件 + 8 个测试文件）

详见 §2.2 模块结构。

---

## 10. 验收标准

本子项目完成的充要条件：

1. ✅ `python -m parsing_core.serve` 能启动并监听 127.0.0.1:8000
2. ✅ `curl -X POST /api/batches -d '{"files":["<abs_path>"]}'` 返回 batch_id 与 task_ids
3. ✅ `wscat -c ws://127.0.0.1:8000/ws/batch/{batch_id}` 能收到事件流含 BATCH_STATE、TASK_STATE、BATCH_DONE
4. ✅ `wscat -c "...?since=N"` 能重放 seq > N 的事件
5. ✅ 5 文件批次能并发跑（concurrency=4），全部 COMPLETED
6. ✅ 不存在的文件路径在 batches 响应中 rejected > 0
7. ✅ DELETE 进行中 batch 能取消未启动 task
8. ✅ 解析失败的文件 → ERROR 事件 + batch status=PARTIAL
9. ✅ CLI 前 73 个测试仍全绿
10. ✅ 服务端新增 ≥ 30 个测试，全量 ≥ 100，覆盖率 ≥ 80%
11. ✅ ruff check src tests 无 warning
12. ✅ /health 返回 {"status":"ok"}

---

## 11. 风险与未决项

| 风险 | 应对 |
|---|---|
| `Orchestrator` 加 on_progress 字段可能破坏前序测试 | 默认 None；CLI 调用不注入；测试验证 None 兼容 |
| asyncio.to_thread 在 GIL 下高并发 CPU 争用 | 接受已知局限；CPU 密集场景由 #4 算力路由阶段引入进程池解决 |
| WS 服务重启丢 in-memory 状态 | 客户端降级到 REST 拿最终状态；进行中 task 由用户手动 resume |
| SQLite 跨连接锁竞争 | WAL 模式 mitigate；双层 Semaphore 限流；若真发生 task=FAILED |
| 30 min TTL 短导致客户端慢重连丢事件 | 可调 config；客户端应优先降级 REST |
| Pydantic V2 与 websockets 版本兼容 | 锁版本下限；CI 验证 |

---

## 12. 时间估算

| 阶段 | 工作量 |
|---|---|
| schema_ext + Pydantic models + config | 0.5 天 |
| Scheduler + ring_buffer + ws_manager | 1.5 天 |
| REST routes + WS routes + deps | 1 天 |
| orchestrator on_progress 修订 + 回归测试 | 0.5 天 |
| serve.py uvicorn 入口 + lifespan | 0.5 天 |
| 单元 + 集成 + E2E 测试 | 2 天 |
| ruff/lint/文档 | 0.5 天 |
| **合计** | **6.5 天** |

---

**下一步**：调用 `writing-plans` 技能基于本规格产出实现计划。