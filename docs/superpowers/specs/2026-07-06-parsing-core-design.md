# 解析内核（#2 修订版）设计规格

**日期**: 2026-07-06
**子项目**: #2 解析内核（穿插式 AI 解读合流版）
**路径**: 地基优先（方案 A）的第一步
**状态**: 已批准，待实现计划

---

## 1. 目标与非目标

### 1.1 目标
构建一个可独立 CLI 验证的 Python 解析内核，完成下列闭环：

1. 输入：本地文件绝对路径（.xlsx / .xls / .pdf / .docx / .pptx / .md / .html / .csv / .json / .xml / .txt / .png / .jpg / .bmp / .tif 等图片）
2. 分节：按原文结构性单元切分（H2/H3、独立表格、独立大段落），超长节二次按字符窗口切分，零散短节合并至上一节
3. 节级 LLM 调用：每节独立产出「AI 解读 + Mermaid 图」穿插跟进（用本地小模型 stub 先打通流水，预留 LiteLLM 接入点）
4. 合流落盘：原文节 + AI 节穿插合并为单一 Markdown 文件，图片落盘外置不塞 Base64
5. 缓存：文件级 sha256 + 节级 sha256 双重缓存，重复节直接命中不重算
6. 崩溃可恢复：每个节是一个独立事务单元，进程中断后可按 task_id 续跑未完成节
7. 输出：返回最终产物 `.md` 文件路径 + 节级状态报告

### 1.2 非目标（本子项目不做）
- Tauri 外壳与 Sidecar 生命周期管理（#1）
- batch_id 批处理调度与并发池（#3）
- LiteLLM 三档算力路由与 Prompt 缓存（#4 仅留接入点）
- WebUI 渲染与虚拟滚动（#5）
- 多进程并行解析优化（性能优化阶段再加，先正确再快）
- WebSocket 状态推送（#3 范围）

---

## 2. 架构

### 2.1 模块结构

```
parsing_core/
├── __init__.py
├── cli.py                  # argparse 入口，调 orchestrator
├── orchestrator.py         # 编排：解析→分节→调用LLM→合流→落盘
├── parser/
│   ├── __init__.py
│   ├── base.py            # Parser 抽象基类
│   ├── markitdown_adapter.py  # MarkItDown 包装
│   ├── chunker.py         # 节切分（结构单元 + 字符窗口兜底 + 短节合并）
│   └── image_extractor.py # 图片从 MD 抽出落盘，替换为路径引用
├── llm/
│   ├── __init__.py
│   ├── base.py            # LLMClient 抽象基类
│   ├── stub_client.py     # 本地小模型 stub（确定性占位输出，用于打通流水）
│   └── prompt_templates.py # 节级 prompt 模板（强制 Mermaid + 业务指标）
├── storage/
│   ├── __init__.py
│   ├── schema.py          # SQLite DDL（WAL 模式）
│   ├── repository.py      # 任务/节/产物 CRUD
│   ├── cache.py           # 文件级 + 节级 sha256 缓存查询
│   └── fs_layout.py       # 文件落盘路径策略（appDataDir 镜像）
├── models/
│   └── dataclasses.py     # Task / Section / AIArtifact 数据类
└── utils/
    ├── hashing.py         # sha256 工具
    ├── file_lock.py       # 副本读取（不碰原文件）
    └── retry.py           # 指数退避（节级重试）
```

### 2.2 调用关系
```
cli.py
  └─ orchestrator.parse_file(path, model_tier="stub")
       ├─ utils.file_lock.snapshot(path)            # 第一步：副本
       ├─ utils.hashing.sha256(snapshot)             # 文件级缓存查询
       ├─ parser.markitdown_adapter.parse(snapshot)  # 原始 MD
       ├─ parser.image_extractor.extract(raw_md)     # 图片落盘
       ├─ parser.chunker.split(raw_md)               # 节列表
       ├─ for each section:
       │    ├─ cache.section_hit(section.sha256)?    # 节级缓存
       │    ├─ llm.stub_client.interpret(section)    # AI 解读 + Mermaid
       │    └─ storage.repository.save_ai_artifact(...)
       ├─ orchestrator.merge(sections, ai_artifacts) # 穿插合流
       └─ storage.repository.save_merged_md(...)     # 落盘
```

---

## 3. 数据模型

### 3.1 SQLite Schema（WAL 模式）

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA mmap_size = 268435456;

CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,        -- UUID
  file_path     TEXT NOT NULL,            -- 用户原始路径
  snapshot_path TEXT NOT NULL,            -- 副本路径（操作对象）
  file_sha256   TEXT NOT NULL,            -- 文件级缓存键
  status        TEXT NOT NULL,            -- PENDING|PARSING|SECTIONING|LLM_RUNNING|MERGING|COMPLETED|FAILED
  model_tier    TEXT NOT NULL DEFAULT 'stub',  -- stub|local|private|public（仅接入点，本子项目只用 stub）
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  error_msg     TEXT
);

CREATE TABLE sections (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,         -- 节序号，从 0 起
  raw_md_path   TEXT NOT NULL,            -- 原文节 MD 落盘路径（外置）
  sha256        TEXT NOT NULL,            -- 节级缓存键
  char_count    INTEGER NOT NULL,
  ai_status     TEXT NOT NULL,            -- PENDING|RUNNING|COMPLETED|FAILED|PARTIAL_SUCCESS
  created_at    INTEGER NOT NULL,
  UNIQUE(task_id, seq)
);

CREATE TABLE ai_artifacts (
  id            TEXT PRIMARY KEY,
  section_id    TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
  ai_md_path    TEXT NOT NULL,            -- AI 解读 MD 落盘路径（外置）
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cost_usd      REAL,
  retry_count   INTEGER NOT NULL DEFAULT 0,
  model_name    TEXT,
  created_at    INTEGER NOT NULL,
  UNIQUE(section_id)
);

CREATE INDEX idx_task_status ON tasks(status);
CREATE INDEX idx_section_task ON sections(task_id);
CREATE INDEX idx_sha_file ON tasks(file_sha256);
CREATE INDEX idx_sha_section ON sections(sha256);
```

### 3.2 大字段落盘策略
- `raw_md_path` / `ai_md_path` 均指向磁盘文件，DB 仅存路径
- 路径规则：`{appDataDir}/parsing-core/{task_id}/{section_seq}.raw.md` 与 `{section_seq}.ai.md`
- 同节 sha256 命中缓存时直接复用现有 `.ai.md` 文件（硬链接到新 task 目录）

---

## 4. 核心算法

### 4.1 节切分（chunker.py）

**输入**：MarkItDown 输出的整篇 raw_md（图片已落盘替换为路径引用）

**步骤**：
1. **按结构单元初切**：扫描 Markdown AST，以下任一为分节点：
   - H2 / H3 标题起始
   - 独立表格（前后空行包围的 `|...|` 块）
   - 独立大段落（≥ 500 字符的非列表段落）
2. **超长节二次切分**：若单节 > 4000 字符，按段落边界二次切分，子节序号用 `seq.sub` 表示（如 `3.1`, `3.2`）—— 注：实现上简化为仍用整数 seq，但 maintain parent_seq 字段或直接平铺，**为简化本子项目平铺，不引入子节维度**
3. **零散短节合并**：若节 < 100 字符且非表格、非标题，合并至上一节
4. **输出**：`List[Section]`，每节含 `seq, raw_md, sha256`

**修正决策（已在 §4.1 步骤 2 内联）**：取消子节维度，超长节按段落边界平铺为新节，保持 seq 单一整数序列。

### 4.2 节级 LLM 调用（stub_client.py）

**stub 行为（本子项目唯一实现）**：
- 输入：节 `raw_md`
- 输出：确定性占位 MD，结构如下：

```markdown
### ▸ AI 解读

- **关键指标**: <stub 占位词>
- **风险提示**: <stub 占位词>

```mermaid
flowchart LR
  A[Stub 节 N] --> B[占位节点]
  B --> C[Mermaid 已就绪]
```
```

- 不调用任何真实模型，纯本地生成，确保 CI 可重现
- 接口 `LLMClient.interpret(section: Section) -> AIArtifact` 为 #4 留接入点

### 4.3 穿插合流（orchestrator.merge）

**合流产物结构**：

```markdown
> 任务 ID: {task_id}
> 源文件: {file_path}
> 生成时间: {ts}

## 第 1 节：{原文节标题或"无标题节"}

{raw_md of section 1}

### ▸ AI 解读

{ai_md of section 1}

---

## 第 2 节：...

{raw_md of section 2}

### ▸ AI 解读

{ai_md of section 2}

---
```

- 合流产物落盘到 `{appDataDir}/parsing-core/{task_id}/merged.md`
- 节间用 `---` 水平分隔线隔开
- AI 解读固定以 `### ▸ AI 解读` 三级标题开头

### 4.4 文件级 + 节级双缓存（cache.py）

```text
parse_file(path):
  sha = sha256(snapshot)
  if exists task with file_sha256=sha AND status=COMPLETED:
      return cached merged.md 硬链接  # 文件级命中，0 LLM 调用

  for each section:
    if exists ai_artifact where section.sha256 matches (across any task):
      reuse ai_md via hardlink  # 节级命中，0 LLM 调用
    else:
      ai = llm.interpret(section)
      save ai_artifact
```

---

## 5. 崩溃恢复

```
resume(task_id):
  task = repo.get_task(task_id)            # 若不存在或 COMPLETED → 无需恢复
  sections = repo.list_sections(task_id)
  pending = [s for s in sections if s.ai_status in (PENDING, RUNNING, FAILED)]
  for s in pending:
      ai = llm.interpret(s)                # 续跑
      repo.update_section_ai_status(s.id, COMPLETED)
  merge(...)
```

CLI 入口：`python -m parsing_core resume <task_id>`

---

## 6. 副本读取（防文件锁）

```python
# utils/file_lock.py
def snapshot(original_path: str) -> str:
    """返回副本路径，绝不触碰原文件"""
    snap = tempfile.NamedTemporaryFile(delete=False, suffix=Path(original_path).suffix)
    shutil.copy2(original_path, snap.name)
    return snap.name
```

任务完成后清理副本（`os.unlink(snapshot_path)`），清理时机：DB 持久化 snapshot_path 便于异常时清理。

---

## 7. CLI 接口

```bash
# 解析新文件
python -m parsing_core parse <file_path> [--model stub] [--force]

# 恢复中断任务
python -m parsing_core resume <task_id>

# 查询任务状态
python -m parsing_core status <task_id>

# 列出所有任务
python -m parsing_core list [--status COMPLETED]

# 清理指定任务的所有资产
python -m parsing_core purge <task_id>
```

**输出约定**：
- 所有命令输出 JSON 到 stdout（便于 #3 调度）
- 日志走 stderr
- `parse` 成功输出：`{"task_id":"...","merged_md_path":"...","sections":N,"cached":bool}`

---

## 8. 错误处理

| 场景 | 处理 |
|---|---|
| 原文件不存在 | 立即返回错误，不创建 task |
| 不支持的文件类型 | 返回 `UNSUPPORTED_TYPE`，不创建 task |
| MarkItDown 解析失败 | task.status=FAILED，error_msg 记录，snapshot 保留供 #3 决策 |
| 单节 LLM 失败 | section.ai_status=FAILED，retry_count++，整任务继续其他节 |
| 三次重试后仍失败 | section.ai_status=PARTIAL_SUCCESS，merged.md 仍生成，对应节 AI 段写"⚠ 此节解读失败，可重试" |
| 落盘写失败 | 抛 IO 异常，task.status=FAILED |

---

## 9. 测试策略

### 9.1 单元测试
- `chunker.split`: 给定样例 MD，断言节切分结果（含超长切分、短节合并、表格独立成节）
- `image_extractor.extract`: 给定含 Base64 图的 MD，断言图片落盘 + 路径替换正确
- `cache.section_hit`: sha256 命中应复用硬链接，未命中应新生成
- `repository`: tasks/sections/ai_artifacts CRUD 全覆盖
- `stub_client.interpret`: 输出格式契约校验（必须含 `### ▸ AI 解读` + `mermaid` 代码块）

### 9.2 集成测试
- 端到端：`parse <fixtures/sample.xlsx>` → 校验 merged.md 存在且结构正确
- 崩溃恢复：人为中断 LLM 调用 → `resume` → 校验所有节完成
- 缓存命中：同一文件二次 parse → `cached=true`，0 次 LLM 调用（用计数器 stub 校验）

### 9.3 性能基线测试（标注非门禁，仅记录）
- 100 页 PDF ≤ 7s（注：本子项目串行实现，性能优化阶段才达标；当前仅记录基线）
- 50MB Excel 安全通过不 OOM

---

## 10. 依赖清单

```toml
# pyproject.toml
[project]
dependencies = [
  "markitdown>=0.0.1",          # 微软 MarkItDown
  "sqlite-utils",               # SQLite 便利封装（可选，也可纯 stdlib）
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "pytest-asyncio",
  "pytest-cov",
  "ruff",
]
```

- 仅 Python 3.11+ stdlib + markitdown，刻意保持依赖最小
- **不**引入 LiteLLM（#4 范围）
- **不**引入 FastAPI（#3 范围）

---

## 11. 验收标准

本子项目完成的充要条件：

1. ✅ 可用 `python -m parsing_core parse <file>` 跑通最小文件（如 sample.md）
2. ✅ 产物 `merged.md` 含原文节 + AI 解读节穿插结构
3. ✅ AI 段含 `### ▸ AI 解读` 标题 + 至少一个 ```mermaid 代码块
4. ✅ 图片从 Base64 替换为本地路径，路径文件实际存在
5. ✅ 同文件二次 parse 命中文件级缓存（cached=true）
6. ✅ 改动一节内容后再 parse，未变动节命中节级缓存（节级 LLM 调用数为 0）
7. ✅ 人为中断后 `resume <task_id>` 能完成剩余节
8. ✅ `pytest` 全绿，覆盖率 ≥ 80%
9. ✅ `ruff check` 无 warning
10. ✅ 崩溃后不留未清理 snapshot（purge 可清）

---

## 12. 与下游子系统的接入点（契约）

为 #3 / #4 / #1 / #5 预留的稳定接口：

| 下游 | 接入点 | 形态 |
|---|---|---|
| #3 调度 | `orchestrator.parse_file(path, model_tier) -> Task` | 同步函数，可被 asyncio 包装为 run_in_executor |
| #3 调度 | `repository.list_tasks_by_status(status)` | 批量任务查询入口 |
| #4 算力路由 | `llm.base.LLMClient` 抽象基类 | 实现该接口即可替换 stub |
| #4 算力路由 | `models.Section` 数据类 | LLM 客户端只依赖此结构 |
| #1 Tauri | `cli.parse / resume / status / list / purge` | 子进程调用 |
| #5 WebUI | `merged.md` 文件路径 + 节级状态 JSON | 直接读取文件 |

---

## 13. 风险与未决项

| 风险 | 应对 |
|---|---|
| MarkItDown 版本迭代 API 不稳 | adapter 包一层，pin 版本 |
| 不同文件类型 AST 难统一 | chunker 退化按段落/表格字符正则识别，不依赖严格 AST |
| 节级 prompt 在真实 LLM 上成本高 | #4 阶段加 prompt caching 与节级批处理 |
| Mermaid 在某些客户端预览失败 | #5 阶段加 mermaid.js 兜底渲染 + 图片导出 |
| SQLite 在高并发批量写入时锁竞争 | 本子项目串行，#3 阶段开 WAL + 队列化写入已规划 |

---

## 14. 时间估算（粗）

| 阶段 | 工作量 |
|---|---|
| 工程骨架 + schema + repository | 0.5 天 |
| MarkItDown adapter + image_extractor | 0.5 天 |
| chunker + 单测 | 1 天 |
| stub_client + prompt_templates | 0.5 天 |
| orchestrator + merge + 落盘 | 1 天 |
| 缓存 + 崩溃恢复 | 1 天 |
| CLI + 集成测试 + 文档 | 1 天 |
| **合计** | **5.5 天** |

---

**下一步**：调用 `writing-plans` 技能基于本规格产出实现计划。