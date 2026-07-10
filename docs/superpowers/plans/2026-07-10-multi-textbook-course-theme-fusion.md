# 多教材课程主题融合精读实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 PDF2MD 支持一门课程导入两本或更多教材，保留每本书的原始章节和章节精读结果，在此基础上生成可人工调整的课程主题目录与多对多章节映射，并产出带来源标注、两张 Mermaid 图和 8 至 12 张写作卡片的课程主题融合精读笔记。

**架构：** 延续现有 SQLite + Markdown 双存储、FastAPI 后端、React/Zustand 前端和 Tauri 桌面壳。章节精读仍是第一层可靠输入；新增课程主题、主题章节映射、主题运行记录、主题笔记块和主题卡片。主题流水线只读取已完成自检的章节精读结果，所有轮次成功后才原子发布新版本，失败时保留上一版成功结果。桌面端采用语雀式三栏工作台，教材章节始终按教材分组，主题映射由 AI 起草、用户确认。

**技术栈：** Python 3.11、FastAPI、Pydantic 2、SQLite、pytest、React 19、TypeScript、Zustand、Vite、Vitest、Testing Library、Tauri 2、Mermaid 11、DeepSeek API、Codex CLI。

---

## 实施边界

### 必须实现

- 同一课程导入两本及以上 PDF、Word 教材，并可继续追加。
- 使用系统文件选择器和拖放导入，不要求粘贴路径。
- 将课程外部文件复制到课程目录的 `教材原文件/`，避免后端长期依赖任意外部路径。
- 教材章节、确认页和精读页始终按教材分组。
- 每本教材保留独立章节树和章节精读结果。
- AI 基于已完成章节精读结果生成课程主题及多对多映射。
- 用户可以增删、改名、排序、合并、拆分主题并调整章节映射。
- 映射确认且全部关联章节完成后，主题才可生成。
- 主题融合包含固定 15 节、来源标注、两张 Mermaid 图和 8 至 12 张卡片。
- 上游章节或映射变化后标记 `需要更新`，保留上一版成功结果。
- SQLite 状态与 Markdown 文件重启后保持一致。

### 本轮不实现

- 云端同步、多人协作和账号系统。
- 自动发布公众号或社交媒体。
- 自动重新生成过期主题。
- 将多本教材全文直接拼接为一次超长模型请求。
- Excel、PPT、图片的完整章节语义识别。导入能力继续保留扩展口，首轮教材闭环以 PDF、DOC、DOCX 为验收格式。

## 文件结构

### 新增后端文件

- `src/parsing_core/workbench/topic_state.py`：主题状态计算和失效传播。
- `src/parsing_core/workbench/topic_outline.py`：课程主题目录与映射生成、验证、落库。
- `src/parsing_core/workbench/topic_task_package.py`：融合精读任务包。
- `src/parsing_core/workbench/topic_pipeline.py`：主题多轮融合、校验和原子发布。
- `src/parsing_core/workbench/topic_markdown_sync.py`：课程主题 Markdown、运行记录和卡片同步。
- `src/parsing_core/workbench/source_import.py`：外部教材文件复制、重名处理和格式校验。
- `src/parsing_core/serving/api/routes_topics.py`：主题目录、映射、融合运行接口。
- `src/parsing_core/serving/models/topics.py`：主题相关 API 模型。

### 新增前端文件

- `parsing-core-app/src/components/workbench/TopicMap.tsx`：主题目录和章节映射编辑器。
- `parsing-core-app/src/components/workbench/TopicFusion.tsx`：主题融合精读工作台。
- `parsing-core-app/src/components/workbench/SourceChapterTree.tsx`：按教材分组的章节树。
- `parsing-core-app/src/components/workbench/ImportTextbooks.tsx`：多教材选择、拖放和导入队列。
- `parsing-core-app/src/test/setup.ts`：Vitest DOM 测试环境。
- `parsing-core-app/vitest.config.ts`：前端测试配置。

### 新增测试文件

- `tests/test_workbench/test_source_import.py`
- `tests/test_workbench/test_topic_state.py`
- `tests/test_workbench/test_topic_outline.py`
- `tests/test_workbench/test_topic_task_package.py`
- `tests/test_workbench/test_topic_pipeline.py`
- `tests/test_workbench/test_topic_markdown_sync.py`
- `tests/test_workbench/test_topic_api.py`
- `parsing-core-app/src/components/workbench/ImportTextbooks.test.tsx`
- `parsing-core-app/src/components/workbench/SourceChapterTree.test.tsx`
- `parsing-core-app/src/components/workbench/TopicMap.test.tsx`
- `parsing-core-app/src/components/workbench/TopicFusion.test.tsx`
- `parsing-core-app/src/store/useWorkbenchStore.test.ts`

### 修改文件

- `src/parsing_core/workbench/models.py`
- `src/parsing_core/workbench/schema.py`
- `src/parsing_core/workbench/repository.py`
- `src/parsing_core/workbench/pipeline.py`
- `src/parsing_core/workbench/hybrid.py`
- `src/parsing_core/workbench/markdown_sync.py`
- `src/parsing_core/serving/api/routes_workbench.py`
- `src/parsing_core/serving/serve.py`
- `parsing-core-app/package.json`
- `parsing-core-app/src-tauri/src/main.rs`
- `parsing-core-app/src/api/workbench.ts`
- `parsing-core-app/src/api/workbenchTypes.ts`
- `parsing-core-app/src/store/useWorkbenchStore.ts`
- `parsing-core-app/src/components/Layout.tsx`
- `parsing-core-app/src/components/workbench/SourceDetail.tsx`
- `parsing-core-app/src/components/workbench/ChapterConfirm.tsx`
- `parsing-core-app/src/components/workbench/ChapterWorkbench.tsx`
- `parsing-core-app/src/components/workbench/CardPool.tsx`
- `parsing-core-app/src/App.tsx`

## 状态与数据契约

后端持久化英文状态，前端统一映射中文：

```python
TOPIC_DRAFT = "DRAFT"
TOPIC_NOT_READY = "NOT_READY"
TOPIC_READY = "READY"
TOPIC_RUNNING = "RUNNING"
TOPIC_COMPLETED = "COMPLETED"
TOPIC_STALE = "STALE"
TOPIC_FAILED = "FAILED"
```

状态转换只允许：

```text
DRAFT -> NOT_READY | READY
NOT_READY -> READY
READY -> RUNNING
RUNNING -> COMPLETED | FAILED
COMPLETED -> STALE
FAILED -> RUNNING | STALE
STALE -> RUNNING
```

主题融合轮次固定为：

```python
TOPIC_ROUNDS = [
    "alignment",
    "comparison",
    "plain_cases",
    "framework_application",
    "mermaid",
    "cards",
    "review",
]
```

## 任务 0：收束当前本地课程目录修复

**文件：**
- 修改：`src/parsing_core/serving/api/routes_workbench.py`
- 修改：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：检查工作区只包含已知修改**

运行：

```bash
git status --short
git diff -- src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_api.py
```

预期：只看到允许绝对本地课程目录、拒绝相对目录和文件系统异常转换的修改，不包含主题功能代码。

- [ ] **步骤 2：运行目录边界测试**

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_api.py -q
```

预期：`test_workbench_create_course_accepts_absolute_local_directory`、相对路径拒绝和文件系统错误用例全部通过。

- [ ] **步骤 3：提交当前修复**

```bash
git add src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_api.py
git commit -m "fix(workbench): allow local course directories"
```

## 任务 1：建立课程主题持久化模型

**文件：**
- 修改：`src/parsing_core/workbench/models.py`
- 修改：`src/parsing_core/workbench/schema.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 修改：`tests/test_workbench/test_schema.py`
- 修改：`tests/test_workbench/test_repository.py`

- [ ] **步骤 1：先写失败的 schema 测试**

在 `tests/test_workbench/test_schema.py` 增加断言，要求以下表存在：

```python
expected_topic_tables = {
    "wb_topics",
    "wb_topic_chapters",
    "wb_topic_note_blocks",
    "wb_topic_cards",
    "wb_topic_runs",
}
assert expected_topic_tables.issubset(table_names)
```

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_schema.py -q
```

预期：失败并指出 `wb_topics` 等表不存在。

- [ ] **步骤 2：增加数据类和表结构**

在 `models.py` 增加：

```python
@dataclass(frozen=True)
class CourseTopic:
    id: str
    course_id: str
    seq: int
    title: str
    description: str
    status: str
    confirmed: bool
    stale_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TopicChapterLink:
    topic_id: str
    chapter_id: str
    created_at: str


@dataclass(frozen=True)
class TopicNoteBlock:
    id: str
    topic_id: str
    kind: str
    content: str
    updated_at: str


@dataclass(frozen=True)
class TopicCard:
    id: str
    topic_id: str
    card_type: str
    title: str
    content: str
    source_refs_json: str
    created_at: str


@dataclass(frozen=True)
class TopicRunRecord:
    id: str
    topic_id: str
    round_key: str
    status: str
    input_fingerprint: str
    output: str
    error: str | None
    started_at: str
    finished_at: str | None
```

在 `schema.py` 增加表，关键约束如下：

```sql
CREATE TABLE IF NOT EXISTS wb_topics (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    confirmed INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(course_id, seq)
);

CREATE TABLE IF NOT EXISTS wb_topic_chapters (
    topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(topic_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS wb_topic_note_blocks (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(topic_id, kind)
);

CREATE TABLE IF NOT EXISTS wb_topic_cards (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
    card_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_topic_runs (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES wb_topics(id) ON DELETE CASCADE,
    round_key TEXT NOT NULL,
    status TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
```

- [ ] **步骤 3：先写失败的 repository 测试**

覆盖：创建主题、更新标题、重新排序、替换多对多映射、删除主题级联、替换笔记块和卡片、追加运行记录。要求 `replace_topic_chapters` 拒绝其他课程的章节。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_repository.py -q
```

预期：因 repository 方法不存在而失败。

- [ ] **步骤 4：实现 repository API**

实现以下稳定签名：

```python
def create_topic(self, course_id: str, seq: int, title: str, description: str = "") -> CourseTopic:
    raise NotImplementedError

def get_topic(self, topic_id: str) -> CourseTopic | None:
    raise NotImplementedError

def list_topics(self, course_id: str) -> list[CourseTopic]:
    raise NotImplementedError

def update_topic(self, topic_id: str, *, title: str, description: str) -> CourseTopic:
    raise NotImplementedError

def reorder_topics(self, course_id: str, topic_ids: list[str]) -> list[CourseTopic]:
    raise NotImplementedError

def delete_topic(self, topic_id: str) -> None:
    raise NotImplementedError

def replace_topic_chapters(self, topic_id: str, chapter_ids: list[str]) -> list[TopicChapterLink]:
    raise NotImplementedError

def list_topic_chapters(self, topic_id: str) -> list[Chapter]:
    raise NotImplementedError

def replace_topic_note_blocks(self, topic_id: str, blocks: dict[str, str]) -> list[TopicNoteBlock]:
    raise NotImplementedError

def replace_topic_cards(self, topic_id: str, cards: list[dict[str, object]]) -> list[TopicCard]:
    raise NotImplementedError

def create_topic_run(self, topic_id: str, round_key: str, input_fingerprint: str) -> TopicRunRecord:
    raise NotImplementedError

def finish_topic_run(self, run_id: str, *, output: str, error: str | None) -> TopicRunRecord:
    raise NotImplementedError
```

实际代码必须完整实现上述签名；所有批量替换和排序必须在 SQLite 事务内完成。

- [ ] **步骤 5：验证并提交**

```bash
.venv/bin/pytest tests/test_workbench/test_schema.py tests/test_workbench/test_repository.py -q
git add src/parsing_core/workbench/models.py src/parsing_core/workbench/schema.py src/parsing_core/workbench/repository.py tests/test_workbench/test_schema.py tests/test_workbench/test_repository.py
git commit -m "feat(workbench): persist course topics and mappings"
```

预期：目标测试全部通过。

## 任务 2：实现主题就绪判断和失效传播

**文件：**
- 新增：`src/parsing_core/workbench/topic_state.py`
- 新增：`tests/test_workbench/test_topic_state.py`
- 修改：`src/parsing_core/workbench/pipeline.py`
- 修改：`src/parsing_core/workbench/repository.py`

- [ ] **步骤 1：写状态机失败测试**

测试以下场景：

1. 无映射或映射未确认时为 `DRAFT`。
2. 任一关联章节未完成自检时为 `NOT_READY`。
3. 映射确认且全部章节完成时为 `READY`。
4. 已完成主题的关联章节重新运行后变为 `STALE`。
5. 仅标记状态和原因，不删除主题笔记块、卡片及历史运行。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_topic_state.py -q
```

预期：模块不存在而失败。

- [ ] **步骤 2：实现状态服务**

```python
@dataclass(frozen=True)
class TopicReadiness:
    status: str
    blocking_chapter_ids: tuple[str, ...]


def evaluate_topic_readiness(repo: WorkbenchRepository, topic_id: str) -> TopicReadiness:
    """根据映射确认状态和章节 review 轮次状态计算主题状态。"""


def refresh_topic_status(repo: WorkbenchRepository, topic_id: str) -> CourseTopic:
    """更新 DRAFT、NOT_READY 或 READY，不覆盖 RUNNING、COMPLETED、STALE。"""


def mark_topics_stale_for_chapter(
    repo: WorkbenchRepository,
    chapter_id: str,
    reason: str,
) -> list[CourseTopic]:
    """标记受章节变化影响且已有发布结果的主题。"""
```

章节完成的唯一判定为 `review` 轮次状态 `COMPLETED`，不得只看章节是否有 Markdown 文件。

- [ ] **步骤 3：接入章节流水线**

在章节单轮重跑或整章重跑开始前调用：

```python
mark_topics_stale_for_chapter(
    self.repo,
    chapter_id,
    reason=f"章节 {chapter_id} 已重新运行",
)
```

若主题从未成功发布，只重新计算 `NOT_READY` 或 `READY`，不使用 `STALE`。

- [ ] **步骤 4：验证并提交**

```bash
.venv/bin/pytest tests/test_workbench/test_topic_state.py tests/test_workbench/test_pipeline.py -q
git add src/parsing_core/workbench/topic_state.py src/parsing_core/workbench/pipeline.py src/parsing_core/workbench/repository.py tests/test_workbench/test_topic_state.py
git commit -m "feat(workbench): track topic readiness and staleness"
```

## 任务 3：实现安全的多教材文件导入

**文件：**
- 新增：`src/parsing_core/workbench/source_import.py`
- 新增：`tests/test_workbench/test_source_import.py`
- 修改：`src/parsing_core/serving/api/routes_workbench.py`
- 修改：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：写文件导入失败测试**

覆盖：

- 从课程目录外选择 PDF，并复制到 `<课程>/教材原文件/教材.pdf`。
- 同名不同内容文件依次生成 `教材.pdf`、`教材-2.pdf`。
- 拒绝不存在的路径、目录、相对路径和不支持的扩展名。
- 文件复制失败时 API 返回 400，不留下 Source 记录。
- 一次导入两本书后，课程有两个独立 Source。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_source_import.py tests/test_workbench/test_api.py -q
```

预期：导入服务和批量接口不存在而失败。

- [ ] **步骤 2：实现导入服务**

```python
SUPPORTED_TEXTBOOK_EXTENSIONS = {".pdf", ".doc", ".docx"}


@dataclass(frozen=True)
class ImportedTextbook:
    title: str
    source_path: Path
    stored_path: Path


def import_textbook_file(course_root: Path, source_path: Path) -> ImportedTextbook:
    """校验绝对本地文件，复制到教材原文件目录并处理重名。"""
```

复制流程使用临时文件加 `Path.replace()`；数据库 Source 只保存复制成功后的 `stored_path`。

- [ ] **步骤 3：增加批量导入 API**

请求与响应：

```python
class TextbookImportRequest(BaseModel):
    paths: list[str] = Field(min_length=1)


class TextbookImportItem(BaseModel):
    source_id: str
    title: str
    stored_path: str


class TextbookImportResponse(BaseModel):
    items: list[TextbookImportItem]
```

路由：

```text
POST /api/workbench/courses/{course_id}/sources/import
```

同一批次按文件逐个返回结果；任一文件失败时整个批次回滚数据库记录并删除本批次已复制文件。

- [ ] **步骤 4：验证并提交**

```bash
.venv/bin/pytest tests/test_workbench/test_source_import.py tests/test_workbench/test_api.py -q
git add src/parsing_core/workbench/source_import.py src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_source_import.py tests/test_workbench/test_api.py
git commit -m "feat(workbench): import multiple textbook files"
```

## 任务 4：生成可验证的课程主题目录和映射

**文件：**
- 新增：`src/parsing_core/workbench/topic_outline.py`
- 新增：`tests/test_workbench/test_topic_outline.py`
- 修改：`src/parsing_core/workbench/executors.py`
- 修改：`src/parsing_core/workbench/hybrid.py`

- [ ] **步骤 1：写主题生成失败测试**

测试两本教材、四个已完成章节，要求：

- 输入包含课程名、教材名、章节名和章节精读结果，不包含整本原文。
- 输出允许一个章节映射多个主题，一个主题映射多本教材章节。
- 输出包含建议理由和未覆盖章节。
- 未知章节 ID、重复主题顺序、空主题和跨课程章节使整次生成失败。
- 校验失败时数据库中不留下半成品主题。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_topic_outline.py -q
```

预期：模块不存在而失败。

- [ ] **步骤 2：定义严格 JSON 契约**

```python
class TopicOutlineItem(BaseModel):
    title: str = Field(min_length=1)
    description: str
    chapter_ids: list[str] = Field(min_length=1)
    reason: str


class TopicOutlineResult(BaseModel):
    topics: list[TopicOutlineItem] = Field(min_length=1)
    unmapped_chapter_ids: list[str]
```

提供稳定入口：

```python
def build_topic_outline_prompt(repo: WorkbenchRepository, course_id: str) -> str:
    """只读取 review 已完成章节的精读笔记块。"""


def generate_topic_outline(
    repo: WorkbenchRepository,
    course_id: str,
    executor: TextExecutor,
) -> TopicOutlineResult:
    """先完整验证模型 JSON，再用一个事务替换草稿主题和映射。"""
```

- [ ] **步骤 3：补充 stub 和混合执行器支持**

`TextExecutor` 使用单一接口：

```python
class TextExecutor(Protocol):
    def run(self, task_key: str, prompt: str) -> str:
        raise NotImplementedError
```

保留现有章节执行器适配层，新增 `topic_outline` 路由到 DeepSeek；测试中使用固定 JSON stub，不能调用真实网络。

- [ ] **步骤 4：验证并提交**

```bash
.venv/bin/pytest tests/test_workbench/test_topic_outline.py tests/test_workbench/test_hybrid.py -q
git add src/parsing_core/workbench/topic_outline.py src/parsing_core/workbench/executors.py src/parsing_core/workbench/hybrid.py tests/test_workbench/test_topic_outline.py tests/test_workbench/test_hybrid.py
git commit -m "feat(workbench): generate course topic outlines"
```

## 任务 5：实现主题融合任务包和原子发布流水线

**文件：**
- 新增：`src/parsing_core/workbench/topic_task_package.py`
- 新增：`src/parsing_core/workbench/topic_pipeline.py`
- 新增：`tests/test_workbench/test_topic_task_package.py`
- 新增：`tests/test_workbench/test_topic_pipeline.py`
- 修改：`src/parsing_core/workbench/hybrid.py`

- [ ] **步骤 1：写任务包失败测试**

任务包必须包含：课程、主题、全部关联教材和章节、每章精读笔记、固定来源标签、前序轮次输出。不得包含未映射章节或教材全文。

```python
@dataclass(frozen=True)
class TopicTaskPackage:
    course_id: str
    topic_id: str
    topic_title: str
    source_chapters: tuple[TopicSourceChapter, ...]
    previous_outputs: dict[str, str]
```

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_topic_task_package.py -q
```

预期：模块不存在而失败。

- [ ] **步骤 2：实现任务包和来源标签**

每个章节生成唯一标签：

```python
def source_label(source_title: str, chapter_seq: int) -> str:
    return f"[《{source_title}》·第 {chapter_seq} 章]"
```

提示词明确要求核心概念、关键观点、教材案例和分歧使用这些精确标签。

- [ ] **步骤 3：写流水线失败测试**

覆盖：

1. `NOT_READY` 和未确认映射拒绝运行。
2. 七轮严格按 `TOPIC_ROUNDS` 执行。
3. DeepSeek 执行文字轮次，Codex CLI 执行 Mermaid 和 review。
4. 输出必须有 15 个固定栏目、恰好两段 Mermaid 和 8 至 12 张卡片。
5. 缺少来源标签、Mermaid 语法围栏或卡片数量越界时 review 失败。
6. 任一轮失败时旧笔记和旧卡片保持不变，状态为 `FAILED`。
7. 全部轮次成功后一次事务替换笔记块和卡片，状态为 `COMPLETED`。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_topic_pipeline.py -q
```

预期：流水线不存在而失败。

- [ ] **步骤 4：实现多轮流水线**

稳定入口：

```python
class TopicFusionPipeline:
    def __init__(
        self,
        repo: WorkbenchRepository,
        executor: IntensiveReadingExecutor,
    ) -> None:
        self.repo = repo
        self.executor = executor

    def run_all(self, topic_id: str) -> CourseTopic:
        """在内存中收集全部轮次，通过校验后原子发布。"""
```

轮次职责：

| 轮次 | 产出栏目 |
|---|---|
| `alignment` | 主题概要、关联教材与章节、核心概念 |
| `comparison` | 教材观点对照、共识与分歧、互补视角 |
| `plain_cases` | 通俗解释、教材案例、现实案例与问题解决 |
| `framework_application` | 综合分析框架、实际应用方法、延伸思考 |
| `mermaid` | 知识结构图、应用流程图 |
| `cards` | 8 至 12 张结构化写作卡片 |
| `review` | 来源、结构、Mermaid、卡片数量和一致性报告 |

`cards` 轮输出严格 JSON：

```json
{
  "cards": [
    {
      "card_type": "concept",
      "title": "卡片标题",
      "content": "卡片正文",
      "source_refs": ["[《教材 A》·第 1 章]"]
    }
  ]
}
```

发布前校验 8 至 12 张卡片，`source_refs` 必须属于任务包允许标签集合。

- [ ] **步骤 5：验证并提交**

```bash
.venv/bin/pytest tests/test_workbench/test_topic_task_package.py tests/test_workbench/test_topic_pipeline.py tests/test_workbench/test_hybrid.py -q
git add src/parsing_core/workbench/topic_task_package.py src/parsing_core/workbench/topic_pipeline.py src/parsing_core/workbench/hybrid.py tests/test_workbench/test_topic_task_package.py tests/test_workbench/test_topic_pipeline.py tests/test_workbench/test_hybrid.py
git commit -m "feat(workbench): run atomic topic fusion pipeline"
```

## 任务 6：同步教材与课程主题 Markdown

**文件：**
- 新增：`src/parsing_core/workbench/topic_markdown_sync.py`
- 新增：`tests/test_workbench/test_topic_markdown_sync.py`
- 修改：`src/parsing_core/workbench/markdown_sync.py`
- 修改：`tests/test_workbench/test_markdown_sync.py`

- [ ] **步骤 1：写目录结构失败测试**

要求课程根目录生成：

```text
教材/
  教材 A/
    01-第一章/
      intensive-note.md
      cards.md
      runs/
  教材 B/
    01-第一章/
      intensive-note.md
      cards.md
      runs/
课程主题/
  01-招聘与甄选/
    topic-map.md
    intensive-note.md
    cards.md
    runs/
```

测试同名章节在不同教材下不会互相覆盖，重命名主题后旧目录被安全迁移而不是复制出两份有效结果。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_markdown_sync.py tests/test_workbench/test_topic_markdown_sync.py -q
```

预期：目录断言失败。

- [ ] **步骤 2：调整章节同步路径**

章节路径由课程根目录下的 `NN-章节` 调整为：

```python
chapter_dir = course_root / "教材" / safe_name(source.title) / chapter_folder_name(chapter)
```

对已有旧目录提供一次迁移：若新目录不存在且旧目录存在，则原子移动；不得删除无法识别的用户文件。

- [ ] **步骤 3：实现主题同步**

```python
def sync_topic_markdown(repo: WorkbenchRepository, topic_id: str) -> Path:
    """写入映射、已发布融合笔记、卡片和运行记录。"""
```

`topic-map.md` 必须列出主题说明、确认状态、每个关联教材章节和来源标签。`intensive-note.md` 必须按固定 15 节输出，并保留 Mermaid 围栏以供 App 直接预览。

- [ ] **步骤 4：验证并提交**

```bash
.venv/bin/pytest tests/test_workbench/test_markdown_sync.py tests/test_workbench/test_topic_markdown_sync.py -q
git add src/parsing_core/workbench/markdown_sync.py src/parsing_core/workbench/topic_markdown_sync.py tests/test_workbench/test_markdown_sync.py tests/test_workbench/test_topic_markdown_sync.py
git commit -m "feat(workbench): sync textbook and topic markdown"
```

## 任务 7：提供完整的主题 API

**文件：**
- 新增：`src/parsing_core/serving/models/topics.py`
- 新增：`src/parsing_core/serving/api/routes_topics.py`
- 新增：`tests/test_workbench/test_topic_api.py`
- 修改：`src/parsing_core/serving/serve.py`
- 修改：`src/parsing_core/serving/api/routes_workbench.py`

- [ ] **步骤 1：写 API 失败测试**

覆盖以下接口：

```text
GET    /api/workbench/courses/{course_id}/topics
POST   /api/workbench/courses/{course_id}/topics
POST   /api/workbench/courses/{course_id}/topics/generate
PUT    /api/workbench/courses/{course_id}/topics/reorder
POST   /api/workbench/courses/{course_id}/topics/confirm
GET    /api/workbench/topics/{topic_id}
PATCH  /api/workbench/topics/{topic_id}
DELETE /api/workbench/topics/{topic_id}
PUT    /api/workbench/topics/{topic_id}/chapters
POST   /api/workbench/topics/{topic_id}/run
POST   /api/workbench/topics/{topic_id}/run-hybrid
GET    /api/workbench/topics/{topic_id}/note-blocks
GET    /api/workbench/topics/{topic_id}/cards
GET    /api/workbench/topics/{topic_id}/runs
```

测试错误语义：404 表示对象不存在；409 表示主题未就绪、映射未确认或已有运行；422 表示请求结构错误；400 表示非法跨课程映射或模型输出无法验证。

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_topic_api.py -q
```

预期：路由不存在，返回 404。

- [ ] **步骤 2：实现 API 模型和路由**

主题响应必须一次返回映射和阻塞信息：

```python
class TopicResponse(BaseModel):
    id: str
    course_id: str
    seq: int
    title: str
    description: str
    status: str
    confirmed: bool
    stale_reason: str | None
    chapter_ids: list[str]
    blocking_chapter_ids: list[str]
```

修改映射时先校验所有章节属于同一课程，再事务替换映射；若已有成功结果，设置 `STALE` 并保留旧结果。确认课程主题目录时要求每个主题至少映射一个章节。

- [ ] **步骤 3：注册路由并做回归**

在 `serve.py` 中注册 `routes_topics.router`。运行：

```bash
.venv/bin/pytest tests/test_workbench/test_topic_api.py tests/test_workbench/test_api.py -q
```

预期：主题和既有工作台 API 全部通过。

- [ ] **步骤 4：提交**

```bash
git add src/parsing_core/serving/models/topics.py src/parsing_core/serving/api/routes_topics.py src/parsing_core/serving/serve.py src/parsing_core/serving/api/routes_workbench.py tests/test_workbench/test_topic_api.py
git commit -m "feat(api): expose course topic workflows"
```

## 任务 8：建立前端测试基线、类型和 Store

**文件：**
- 修改：`parsing-core-app/package.json`
- 新增：`parsing-core-app/vitest.config.ts`
- 新增：`parsing-core-app/src/test/setup.ts`
- 修改：`parsing-core-app/src/api/workbenchTypes.ts`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 修改：`parsing-core-app/src/store/useWorkbenchStore.ts`
- 新增：`parsing-core-app/src/store/useWorkbenchStore.test.ts`

- [ ] **步骤 1：安装测试依赖**

```bash
cd parsing-core-app
npm install --save-dev vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event
```

在 `package.json` 增加：

```json
"test": "vitest run"
```

- [ ] **步骤 2：配置测试环境**

```ts
// vitest.config.ts
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
```

```ts
// src/test/setup.ts
import '@testing-library/jest-dom/vitest'
```

- [ ] **步骤 3：先写 Store 失败测试**

覆盖：加载主题、生成主题、更新映射、确认主题目录、运行融合、失败时清除 `loading` 并保存错误信息。特别断言所有异步 action 使用 `try/finally`，避免界面永久骨架屏。

运行：

```bash
npm test -- src/store/useWorkbenchStore.test.ts
```

预期：缺少主题状态和 action 而失败。

- [ ] **步骤 4：实现类型、API 客户端和 Store**

核心前端类型：

```ts
export type TopicStatus =
  | 'DRAFT'
  | 'NOT_READY'
  | 'READY'
  | 'RUNNING'
  | 'COMPLETED'
  | 'STALE'
  | 'FAILED'

export interface CourseTopic {
  id: string
  course_id: string
  seq: number
  title: string
  description: string
  status: TopicStatus
  confirmed: boolean
  stale_reason: string | null
  chapter_ids: string[]
  blocking_chapter_ids: string[]
}
```

Store 增加 `topicsByCourse`、`topicBlocksById`、`topicCardsById`、`topicRunsById` 和对应异步 action。API 错误必须显示可理解的中文信息，不得只输出原始英文异常。

- [ ] **步骤 5：验证并提交**

```bash
npm test -- src/store/useWorkbenchStore.test.ts
npm run build
git add package.json package-lock.json vitest.config.ts src/test/setup.ts src/api/workbenchTypes.ts src/api/workbench.ts src/store/useWorkbenchStore.ts src/store/useWorkbenchStore.test.ts
git commit -m "test(app): add topic workflow frontend foundation"
```

以上 `git add` 在 `parsing-core-app` 目录执行。

## 任务 9：完成多教材选择、拖放和章节分组

**文件：**
- 新增：`parsing-core-app/src/components/workbench/ImportTextbooks.tsx`
- 新增：`parsing-core-app/src/components/workbench/ImportTextbooks.test.tsx`
- 新增：`parsing-core-app/src/components/workbench/SourceChapterTree.tsx`
- 新增：`parsing-core-app/src/components/workbench/SourceChapterTree.test.tsx`
- 修改：`parsing-core-app/src-tauri/src/main.rs`
- 修改：`parsing-core-app/src/components/workbench/SourceDetail.tsx`
- 修改：`parsing-core-app/src/components/workbench/ChapterConfirm.tsx`
- 修改：`parsing-core-app/src/components/workbench/ChapterWorkbench.tsx`
- 修改：`parsing-core-app/src/components/Layout.tsx`

- [ ] **步骤 1：写多文件导入组件失败测试**

覆盖：点击按钮调用系统选择器；一次接收两本书；拖放 PDF/DOCX；过滤不支持文件并显示错误；导入中显示逐文件状态；完成后刷新教材列表并自动识别各自章节。

运行：

```bash
cd parsing-core-app
npm test -- src/components/workbench/ImportTextbooks.test.tsx
```

预期：组件不存在而失败。

- [ ] **步骤 2：实现 Tauri 选择器过滤**

在 Rust 命令中使用文件过滤器：

```rust
FileDialogBuilder::new()
    .add_filter("教材", &["pdf", "doc", "docx"])
    .pick_files(move |paths| {
        let values = paths
            .unwrap_or_default()
            .into_iter()
            .map(|path| path.to_string_lossy().to_string())
            .collect::<Vec<String>>();
        let _ = app.emit("textbooks-selected", values);
    });
```

保留 Web 开发模式的 `<input type="file" multiple>` 回退。

- [ ] **步骤 3：实现导入队列**

组件使用现有图标库，提供文件选择按钮、拖放区域、文件列表、成功/失败状态和重试。不得保留可编辑的“本地路径”文本框。

- [ ] **步骤 4：写章节分组失败测试**

使用两本教材且都有“第 1 章”的数据，断言界面显示两个教材组、各自进度、教材名和章节名；点击章节时传递唯一 `chapter.id`，不按标题匹配。

运行：

```bash
npm test -- src/components/workbench/SourceChapterTree.test.tsx
```

预期：组件不存在而失败。

- [ ] **步骤 5：替换扁平章节展示**

`Layout.tsx`、`ChapterConfirm.tsx` 和 `ChapterWorkbench.tsx` 统一使用：

```ts
interface SourceChapterGroup {
  source: Source
  chapters: Chapter[]
  completedCount: number
}
```

教材组可折叠，选择器标签使用 `《教材名》 / 第 N 章 / 章节名`。不得继续使用 `Object.values(chapters).flat()` 直接渲染无来源章节。

- [ ] **步骤 6：验证并提交**

```bash
npm test -- src/components/workbench/ImportTextbooks.test.tsx src/components/workbench/SourceChapterTree.test.tsx
npm run build
cd src-tauri && cargo check
cd ..
git add src-tauri/src/main.rs src/components/workbench/ImportTextbooks.tsx src/components/workbench/ImportTextbooks.test.tsx src/components/workbench/SourceChapterTree.tsx src/components/workbench/SourceChapterTree.test.tsx src/components/workbench/SourceDetail.tsx src/components/workbench/ChapterConfirm.tsx src/components/workbench/ChapterWorkbench.tsx src/components/Layout.tsx
git commit -m "feat(app): import and group multiple textbooks"
```

以上最后两条命令在 `parsing-core-app` 目录执行。

## 任务 10：实现课程主题目录和映射编辑器

**文件：**
- 新增：`parsing-core-app/src/components/workbench/TopicMap.tsx`
- 新增：`parsing-core-app/src/components/workbench/TopicMap.test.tsx`
- 修改：`parsing-core-app/src/App.tsx`
- 修改：`parsing-core-app/src/components/Layout.tsx`

- [ ] **步骤 1：写主题编辑器失败测试**

覆盖：

- 所有章节完成前，“AI 生成课程主题”禁用并列出阻塞章节。
- AI 生成后显示主题列表、说明、建议理由、未覆盖章节。
- 可改名、新建、删除、排序、合并和拆分主题。
- 章节选择按教材分组，支持多对多勾选。
- 每章显示已关联主题数量。
- 修改已完成主题映射后显示 `需要更新`，旧结果入口仍可点击。
- 任一主题无章节时不能确认整个目录。

运行：

```bash
cd parsing-core-app
npm test -- src/components/workbench/TopicMap.test.tsx
```

预期：组件不存在而失败。

- [ ] **步骤 2：实现主题映射工作台**

三栏结构：左栏课程级入口；中栏主题目录和状态；右栏主题详情与按教材分组章节。排序使用上移/下移图标按钮，合并要求选中至少两个主题，拆分时新主题必须有名称和至少一个章节。

路由：

```text
/workbench/courses/:courseId/topics
```

左栏新增 `教材`、`课程主题`、`融合精读`、`写作卡片`，当前入口保持清晰选中状态。

- [ ] **步骤 3：验证并提交**

```bash
npm test -- src/components/workbench/TopicMap.test.tsx
npm run build
git add src/components/workbench/TopicMap.tsx src/components/workbench/TopicMap.test.tsx src/App.tsx src/components/Layout.tsx
git commit -m "feat(app): edit course topics and chapter mappings"
```

## 任务 11：实现融合精读预览和课程级卡片池

**文件：**
- 新增：`parsing-core-app/src/components/workbench/TopicFusion.tsx`
- 新增：`parsing-core-app/src/components/workbench/TopicFusion.test.tsx`
- 修改：`parsing-core-app/src/components/workbench/CardPool.tsx`
- 修改：`parsing-core-app/src/App.tsx`
- 修改：`src/parsing_core/workbench/repository.py`
- 修改：`src/parsing_core/serving/api/routes_workbench.py`
- 修改：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：写融合页失败测试**

覆盖：

- `未就绪` 显示阻塞章节且不允许运行。
- `可生成` 和 `需要更新` 可以手动运行。
- `失败` 显示本次错误，但继续展示上一版成功内容。
- 15 个栏目按固定顺序显示。
- 两段 Mermaid 使用现有 `MermaidBlock` 直接预览，源码可编辑。
- 点击来源标签跳转到正确教材章节精读页。
- 8 至 12 张卡片显示类型、标题、正文和来源。

运行：

```bash
cd parsing-core-app
npm test -- src/components/workbench/TopicFusion.test.tsx
```

预期：组件不存在而失败。

- [ ] **步骤 2：实现融合精读页**

路由：

```text
/workbench/courses/:courseId/topics/:topicId
```

中栏显示课程主题及状态，右栏顶部显示生成/重新生成、最后成功时间和过期原因；正文采用适合长文阅读的窄内容列，不把每个栏目做成嵌套卡片。

- [ ] **步骤 3：统一课程级卡片响应**

课程卡片接口返回章节卡片与主题卡片的联合类型：

```python
class CourseCardResponse(BaseModel):
    id: str
    origin_type: Literal["chapter", "topic"]
    origin_id: str
    origin_title: str
    card_type: str
    title: str
    content: str
    source_refs: list[str]
```

前端卡片池增加“全部 / 章节精读 / 融合精读”分段筛选，默认全部；点击来源进入对应章节或主题。

- [ ] **步骤 4：验证并提交**

```bash
cd /Users/laoer/Documents/PDF2MD
.venv/bin/pytest tests/test_workbench/test_api.py tests/test_workbench/test_topic_api.py -q
cd parsing-core-app
npm test -- src/components/workbench/TopicFusion.test.tsx
npm run build
git add ../src/parsing_core/workbench/repository.py ../src/parsing_core/serving/api/routes_workbench.py ../tests/test_workbench/test_api.py src/components/workbench/TopicFusion.tsx src/components/workbench/TopicFusion.test.tsx src/components/workbench/CardPool.tsx src/App.tsx
git commit -m "feat(app): preview topic fusion notes and cards"
```

## 任务 12：完成双教材端到端验收和桌面 App 构建

**文件：**
- 修改：`tests/test_workbench/test_topic_api.py`
- 新增：`tests/test_workbench/test_multi_textbook_flow.py`
- 按验证结果修复：本计划列出的后端和前端文件

- [ ] **步骤 1：写双教材端到端测试**

测试数据：课程“人力资源管理”，教材 A 和教材 B 各两个章节，其中两个课程主题分别映射多个章节，且至少一个章节映射两个主题。

完整流程：

```text
创建课程
-> 导入两本教材
-> 分别识别并确认章节
-> 完成四个章节的多轮精读和 review
-> 生成主题目录
-> 人工修改映射并确认
-> 运行两个主题融合
-> 重启 repository
-> 验证主题、映射、Markdown、两张 Mermaid 和卡片仍存在
-> 重跑一个章节
-> 验证两个受影响主题标记需要更新且旧输出仍存在
```

运行：

```bash
.venv/bin/pytest tests/test_workbench/test_multi_textbook_flow.py -q
```

预期：首次运行至少有一个未接通环节失败；修复后通过。

- [ ] **步骤 2：运行后端全量质量门禁**

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

预期：ruff 无错误，pytest 全部通过。

- [ ] **步骤 3：运行前端和 Tauri 质量门禁**

```bash
cd parsing-core-app
npm test
npm run build
cd src-tauri
cargo check
cd ..
npm run tauri build
```

预期：所有前端测试通过，TypeScript/Vite 构建成功，Rust 检查成功，生成 macOS `.app` 和 DMG。

- [ ] **步骤 4：安装并冷启动正式桌面构建**

先找到本次构建产物，不假设应用名：

```bash
find src-tauri/target/release/bundle -maxdepth 3 \( -name "*.app" -o -name "*.dmg" \) -print
```

关闭旧进程，将新 `.app` 替换到 `/Applications/PDF2MD.app`，双击冷启动，然后验证：

```bash
curl --fail --silent http://127.0.0.1:8000/health
```

预期：返回健康状态；`sidecar.log` 中出现 Uvicorn 启动和工作台 API 请求，不出现循环重启。

- [ ] **步骤 5：执行桌面端可视验收**

使用 Playwright 或应用内浏览器检查 1440×900 和 1024×768 两个视口，并保留截图证据：

1. 多教材文件选择和导入队列可见。
2. 两本教材及同名章节不会混淆。
3. 主题映射编辑器没有控件重叠或文字截断。
4. 融合笔记可滚动阅读，两张 Mermaid 非空并直接渲染。
5. 来源标签可跳转，卡片筛选正常。
6. 后端错误时骨架屏停止并显示恢复操作。

- [ ] **步骤 6：提交端到端测试和最终修复**

```bash
cd /Users/laoer/Documents/PDF2MD
git add tests/test_workbench/test_multi_textbook_flow.py tests/test_workbench/test_topic_api.py src parsing-core-app/src parsing-core-app/src-tauri/src parsing-core-app/package.json parsing-core-app/package-lock.json parsing-core-app/vitest.config.ts
git diff --cached --check
git commit -m "test(workbench): verify multi-textbook topic fusion flow"
```

不得提交 `target/`、`dist/`、本地数据库、日志或用户课程资料。

## 最终验收清单

- [ ] 一门课程可导入至少两本 PDF/DOCX 教材。
- [ ] 外部教材被复制进课程目录，原文件不被修改。
- [ ] 两本教材的章节树、进度和精读结果相互独立。
- [ ] AI 主题生成只读取已完成章节精读结果。
- [ ] 主题与章节支持真正的多对多映射。
- [ ] 用户可修改主题目录和映射，并明确确认。
- [ ] 未就绪主题不能运行，界面列出具体阻塞章节。
- [ ] 每份融合笔记严格包含 15 个栏目。
- [ ] 关键内容来源标签可跳转到正确教材章节。
- [ ] 每份融合笔记恰好包含两张可直接预览的 Mermaid 图。
- [ ] 每份融合笔记包含 8 至 12 张来源明确的写作卡片。
- [ ] 上游变化触发 `需要更新`，旧成功结果不丢失。
- [ ] 失败运行保留旧结果和完整运行记录。
- [ ] 重启桌面 App 后 SQLite 与 Markdown 结果保持一致。
- [ ] 所有后端、前端、Rust 和桌面冷启动验证通过。

## 计划自检

执行计划前后都运行：

```bash
placeholder_pattern='TO''DO|TB''D|待''定|稍后''补充|类似''任务'
rg -n "$placeholder_pattern" docs/superpowers/plans/2026-07-10-multi-textbook-course-theme-fusion.md
git diff --check
```

预期：占位词扫描无输出，`git diff --check` 无输出；不得存在未决需求或占位步骤。

规格覆盖核对：

```bash
for term in "多对多" "来源标签" "Mermaid" "8 至 12" "需要更新" "系统文件选择器" "原子发布" "Markdown"; do
  rg -q "$term" docs/superpowers/plans/2026-07-10-multi-textbook-course-theme-fusion.md || exit 1
done
```

预期：命令退出码为 0。
