# 课程资料精读工作台实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在现有 PDF2MD 解析、服务和 WebUI 基础上，新增课程资料精读工作台：课程组织、主资料章节确认、补充资料挂载、多轮精读、结构化笔记、Mermaid 预览、写作卡片池和 Markdown 同步。

**架构：** 保留现有 `parsing_core` 解析内核和 FastAPI 服务，新增 `workbench` 业务子包承载课程、资料、章节、块、卡片和运行记录。后端通过 SQLite 扩展表和薄 API 暴露工作台能力；前端新增课程、资料、章节确认、章节精读和卡片池页面。第一版使用本地确定性精读执行器打通多轮流水线，API/Codex/人工执行器以统一接口和任务包文件形态落地。

**技术栈：** Python 3.11+、SQLite WAL、FastAPI、Pydantic V2、pytest、React 19、TypeScript、Zustand、react-markdown、Mermaid、Tauri WebView。

---

## 文件结构

### 后端新增

| 文件 | 职责 |
|---|---|
| `src/parsing_core/workbench/__init__.py` | workbench 子包导出 |
| `src/parsing_core/workbench/schema.py` | 工作台 SQLite 表 DDL 与幂等迁移 |
| `src/parsing_core/workbench/models.py` | 课程、资料、章节、块、卡片、运行记录 dataclass |
| `src/parsing_core/workbench/repository.py` | 工作台 CRUD，避免污染现有 `Repository` |
| `src/parsing_core/workbench/markdown_sync.py` | SQLite 结构化内容同步到每章 Markdown 文件 |
| `src/parsing_core/workbench/chapter_detection.py` | 从 Markdown 生成章节候选 |
| `src/parsing_core/workbench/task_package.py` | 章节任务包生成、导出、导入 |
| `src/parsing_core/workbench/pipeline.py` | 多轮精读流水线、单轮重跑和过期标记 |
| `src/parsing_core/workbench/executors.py` | API/Codex/人工/Stub 执行器接口与第一版实现 |
| `src/parsing_core/serving/api/routes_workbench.py` | 工作台 REST API |

### 后端修改

| 文件 | 修改 |
|---|---|
| `src/parsing_core/serving/serve.py` | 初始化 workbench schema，并 include workbench router |
| `src/parsing_core/serving/models/api.py` | 增加工作台 API Pydantic 模型 |

### 后端测试新增

| 文件 | 职责 |
|---|---|
| `tests/test_workbench/test_schema.py` | 表迁移幂等性 |
| `tests/test_workbench/test_repository.py` | CRUD |
| `tests/test_workbench/test_chapter_detection.py` | 章节候选生成 |
| `tests/test_workbench/test_markdown_sync.py` | Markdown 文件同步 |
| `tests/test_workbench/test_task_package.py` | 任务包导出 |
| `tests/test_workbench/test_pipeline.py` | 多轮运行、单轮重跑、过期标记 |
| `tests/test_workbench/test_api.py` | REST API |

### 前端新增

| 文件 | 职责 |
|---|---|
| `parsing-core-app/src/api/workbench.ts` | 工作台 REST 客户端 |
| `parsing-core-app/src/api/workbenchTypes.ts` | 工作台 TS 类型 |
| `parsing-core-app/src/store/useWorkbenchStore.ts` | 课程、资料、章节、卡片状态 |
| `parsing-core-app/src/components/workbench/CourseList.tsx` | 课程列表 |
| `parsing-core-app/src/components/workbench/SourceDetail.tsx` | 主资料详情 |
| `parsing-core-app/src/components/workbench/ChapterConfirm.tsx` | 章节确认 |
| `parsing-core-app/src/components/workbench/ChapterWorkbench.tsx` | 章节精读页 |
| `parsing-core-app/src/components/workbench/CardPool.tsx` | 课程卡片池 |
| `parsing-core-app/src/components/workbench/MermaidEditor.tsx` | Mermaid 源码编辑与实时预览 |

### 前端修改

| 文件 | 修改 |
|---|---|
| `parsing-core-app/src/App.tsx` | 新增 workbench 路由 |
| `parsing-core-app/src/components/Layout.tsx` | 新增课程工作台导航 |

---

## 任务 1：工作台 SQLite schema

**文件：**
- 创建：`src/parsing_core/workbench/__init__.py`
- 创建：`src/parsing_core/workbench/schema.py`
- 测试：`tests/test_workbench/test_schema.py`

- [ ] **步骤 1：编写失败的 schema 测试**

创建 `tests/test_workbench/test_schema.py`：

```python
from parsing_core.storage.schema import init_db
from parsing_core.workbench.schema import apply_workbench_schema


def test_apply_workbench_schema_creates_tables(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert "wb_courses" in tables
    assert "wb_sources" in tables
    assert "wb_chapters" in tables
    assert "wb_attachments" in tables
    assert "wb_note_blocks" in tables
    assert "wb_cards" in tables
    assert "wb_runs" in tables


def test_apply_workbench_schema_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "serve.db"))
    apply_workbench_schema(conn)
    apply_workbench_schema(conn)

    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(wb_cards)").fetchall()
    }
    assert {"id", "course_id", "chapter_id", "kind", "title", "body", "favorite"} <= cols
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_schema.py -q`

预期：FAIL，报错包含 `ModuleNotFoundError: No module named 'parsing_core.workbench'`。

- [ ] **步骤 3：新增 schema 实现**

创建 `src/parsing_core/workbench/__init__.py`：

```python
"""Course intensive-reading workbench."""
```

创建 `src/parsing_core/workbench/schema.py`：

```python
import sqlite3


WORKBENCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wb_courses (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  root_dir TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_sources (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  file_path TEXT NOT NULL,
  title TEXT NOT NULL,
  markdown_path TEXT,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_chapters (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES wb_sources(id) ON DELETE CASCADE,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  source_md_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(source_id, seq)
);

CREATE TABLE IF NOT EXISTS wb_attachments (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  chapter_id TEXT REFERENCES wb_chapters(id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_note_blocks (
  id TEXT PRIMARY KEY,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  seq INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(chapter_id, kind)
);

CREATE TABLE IF NOT EXISTS wb_cards (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL REFERENCES wb_courses(id) ON DELETE CASCADE,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  favorite INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_runs (
  id TEXT PRIMARY KEY,
  chapter_id TEXT NOT NULL REFERENCES wb_chapters(id) ON DELETE CASCADE,
  round_key TEXT NOT NULL,
  executor TEXT NOT NULL,
  status TEXT NOT NULL,
  input_path TEXT NOT NULL DEFAULT '',
  output_path TEXT NOT NULL DEFAULT '',
  output TEXT NOT NULL DEFAULT '',
  stale INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(chapter_id, round_key)
);

CREATE INDEX IF NOT EXISTS idx_wb_sources_course ON wb_sources(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_chapters_course ON wb_chapters(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_cards_course ON wb_cards(course_id);
CREATE INDEX IF NOT EXISTS idx_wb_runs_chapter ON wb_runs(chapter_id);
"""


def apply_workbench_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(WORKBENCH_SCHEMA_SQL)
    conn.commit()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_schema.py -q`

预期：`2 passed`。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/__init__.py src/parsing_core/workbench/schema.py tests/test_workbench/test_schema.py
git commit -m "feat(workbench): add schema"
```

---

## 任务 2：工作台数据模型与 Repository

**文件：**
- 创建：`src/parsing_core/workbench/models.py`
- 创建：`src/parsing_core/workbench/repository.py`
- 测试：`tests/test_workbench/test_repository.py`

- [ ] **步骤 1：编写失败的 Repository 测试**

创建 `tests/test_workbench/test_repository.py`：

```python
from parsing_core.storage.schema import init_db
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


def repo(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    return WorkbenchRepository(conn)


def test_create_course_source_and_chapter(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "MBA 课程", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapter = r.create_chapter(course.id, source.id, 0, "第一章 战略是什么", "/tmp/ch1.md")

    assert r.get_course(course.id).title == "战略管理"
    assert r.list_sources(course.id)[0].title == "战略教材"
    assert r.list_chapters(source.id)[0].title == "第一章 战略是什么"
    assert chapter.status == "DRAFT"


def test_cards_can_be_edited_and_favorited(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("营销管理", "", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "营销教材")
    chapter = r.create_chapter(course.id, source.id, 0, "第一章", "/tmp/ch1.md")
    card = r.create_card(course.id, chapter.id, "viewpoint", "定位不是口号", "定位是选择。")

    r.update_card(card.id, title="定位是取舍", body="定位不是更多，而是更少。")
    r.set_card_favorite(card.id, True)

    cards = r.list_cards(course.id)
    assert cards[0].title == "定位是取舍"
    assert cards[0].favorite is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_repository.py -q`

预期：FAIL，报错包含 `No module named 'parsing_core.workbench.repository'`。

- [ ] **步骤 3：新增 dataclass 模型**

创建 `src/parsing_core/workbench/models.py`：

```python
from dataclasses import dataclass


@dataclass
class Course:
    id: str
    title: str
    description: str
    root_dir: str
    created_at: int
    updated_at: int


@dataclass
class Source:
    id: str
    course_id: str
    kind: str
    file_path: str
    title: str
    markdown_path: str | None
    status: str
    created_at: int
    updated_at: int


@dataclass
class Chapter:
    id: str
    source_id: str
    course_id: str
    seq: int
    title: str
    source_md_path: str
    status: str
    created_at: int
    updated_at: int


@dataclass
class Attachment:
    id: str
    course_id: str
    chapter_id: str | None
    file_path: str
    title: str
    kind: str
    created_at: int


@dataclass
class NoteBlock:
    id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    seq: int
    updated_at: int


@dataclass
class Card:
    id: str
    course_id: str
    chapter_id: str
    kind: str
    title: str
    body: str
    favorite: bool
    created_at: int
    updated_at: int


@dataclass
class RunRecord:
    id: str
    chapter_id: str
    round_key: str
    executor: str
    status: str
    input_path: str
    output_path: str
    output: str
    stale: bool
    created_at: int
    updated_at: int
```

- [ ] **步骤 4：新增 Repository 最小 CRUD**

创建 `src/parsing_core/workbench/repository.py`：

```python
import sqlite3
import time
import uuid

from parsing_core.workbench.models import Card, Chapter, Course, Source


def _now() -> int:
    return int(time.time())


class WorkbenchRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_course(self, title: str, description: str, root_dir: str) -> Course:
        now = _now()
        course = Course(str(uuid.uuid4()), title, description, root_dir, now, now)
        self.conn.execute(
            "INSERT INTO wb_courses (id, title, description, root_dir, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (course.id, course.title, course.description, course.root_dir, now, now),
        )
        self.conn.commit()
        return course

    def get_course(self, course_id: str) -> Course | None:
        row = self.conn.execute(
            "SELECT id, title, description, root_dir, created_at, updated_at "
            "FROM wb_courses WHERE id = ?",
            (course_id,),
        ).fetchone()
        return Course(*row) if row else None

    def list_courses(self) -> list[Course]:
        rows = self.conn.execute(
            "SELECT id, title, description, root_dir, created_at, updated_at "
            "FROM wb_courses ORDER BY updated_at DESC"
        ).fetchall()
        return [Course(*row) for row in rows]

    def create_source(self, course_id: str, kind: str, file_path: str, title: str) -> Source:
        now = _now()
        source = Source(str(uuid.uuid4()), course_id, kind, file_path, title, None, "IMPORTED", now, now)
        self.conn.execute(
            "INSERT INTO wb_sources (id, course_id, kind, file_path, title, markdown_path, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.id,
                source.course_id,
                source.kind,
                source.file_path,
                source.title,
                source.markdown_path,
                source.status,
                now,
                now,
            ),
        )
        self.conn.commit()
        return source

    def list_sources(self, course_id: str) -> list[Source]:
        rows = self.conn.execute(
            "SELECT id, course_id, kind, file_path, title, markdown_path, status, created_at, updated_at "
            "FROM wb_sources WHERE course_id = ? ORDER BY created_at DESC",
            (course_id,),
        ).fetchall()
        return [Source(*row) for row in rows]

    def create_chapter(
        self, course_id: str, source_id: str, seq: int, title: str, source_md_path: str
    ) -> Chapter:
        now = _now()
        chapter = Chapter(str(uuid.uuid4()), source_id, course_id, seq, title, source_md_path, "DRAFT", now, now)
        self.conn.execute(
            "INSERT INTO wb_chapters (id, source_id, course_id, seq, title, source_md_path, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chapter.id,
                chapter.source_id,
                chapter.course_id,
                chapter.seq,
                chapter.title,
                chapter.source_md_path,
                chapter.status,
                now,
                now,
            ),
        )
        self.conn.commit()
        return chapter

    def list_chapters(self, source_id: str) -> list[Chapter]:
        rows = self.conn.execute(
            "SELECT id, source_id, course_id, seq, title, source_md_path, status, created_at, updated_at "
            "FROM wb_chapters WHERE source_id = ? ORDER BY seq",
            (source_id,),
        ).fetchall()
        return [Chapter(*row) for row in rows]

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        row = self.conn.execute(
            "SELECT id, source_id, course_id, seq, title, source_md_path, status, created_at, updated_at "
            "FROM wb_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        return Chapter(*row) if row else None

    def update_chapter_status(self, chapter_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE wb_chapters SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), chapter_id),
        )
        self.conn.commit()

    def create_card(self, course_id: str, chapter_id: str, kind: str, title: str, body: str) -> Card:
        now = _now()
        card = Card(str(uuid.uuid4()), course_id, chapter_id, kind, title, body, False, now, now)
        self.conn.execute(
            "INSERT INTO wb_cards (id, course_id, chapter_id, kind, title, body, favorite, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (card.id, course_id, chapter_id, kind, title, body, 0, now, now),
        )
        self.conn.commit()
        return card

    def list_cards(self, course_id: str) -> list[Card]:
        rows = self.conn.execute(
            "SELECT id, course_id, chapter_id, kind, title, body, favorite, created_at, updated_at "
            "FROM wb_cards WHERE course_id = ? ORDER BY favorite DESC, updated_at DESC",
            (course_id,),
        ).fetchall()
        return [Card(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8]) for r in rows]

    def update_card(self, card_id: str, title: str, body: str) -> None:
        self.conn.execute(
            "UPDATE wb_cards SET title = ?, body = ?, updated_at = ? WHERE id = ?",
            (title, body, _now(), card_id),
        )
        self.conn.commit()

    def set_card_favorite(self, card_id: str, favorite: bool) -> None:
        self.conn.execute(
            "UPDATE wb_cards SET favorite = ?, updated_at = ? WHERE id = ?",
            (1 if favorite else 0, _now(), card_id),
        )
        self.conn.commit()
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_repository.py -q`

预期：`2 passed`。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/models.py src/parsing_core/workbench/repository.py tests/test_workbench/test_repository.py
git commit -m "feat(workbench): add repository"
```

---

## 任务 3：章节候选识别

**文件：**
- 创建：`src/parsing_core/workbench/chapter_detection.py`
- 测试：`tests/test_workbench/test_chapter_detection.py`

- [ ] **步骤 1：编写失败测试**

创建 `tests/test_workbench/test_chapter_detection.py`：

```python
from parsing_core.workbench.chapter_detection import detect_chapters


def test_detect_chapters_from_markdown_headings():
    md = """# Book

## 第一章 战略是什么
内容 A

### 1.1 战略的定义
内容 B

## 第二章 外部环境
内容 C
"""
    chapters = detect_chapters(md)

    assert [c.title for c in chapters] == ["第一章 战略是什么", "第二章 外部环境"]
    assert chapters[0].raw_md.startswith("## 第一章")
    assert "### 1.1 战略的定义" in chapters[0].raw_md
    assert chapters[1].seq == 1


def test_detect_chapters_falls_back_to_single_chapter():
    chapters = detect_chapters("没有标题的正文")
    assert len(chapters) == 1
    assert chapters[0].title == "全文"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_chapter_detection.py -q`

预期：FAIL，报错包含 `No module named 'parsing_core.workbench.chapter_detection'`。

- [ ] **步骤 3：实现 Markdown H2 章节识别**

创建 `src/parsing_core/workbench/chapter_detection.py`：

```python
import re
from dataclasses import dataclass


@dataclass
class ChapterCandidate:
    seq: int
    title: str
    raw_md: str


H2_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def detect_chapters(markdown: str) -> list[ChapterCandidate]:
    matches = list(H2_RE.finditer(markdown))
    if not matches:
        return [ChapterCandidate(seq=0, title="全文", raw_md=markdown.strip())]

    chapters: list[ChapterCandidate] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        raw = markdown[start:end].strip()
        chapters.append(ChapterCandidate(seq=i, title=match.group(1).strip(), raw_md=raw))
    return chapters
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_chapter_detection.py -q`

预期：`2 passed`。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/chapter_detection.py tests/test_workbench/test_chapter_detection.py
git commit -m "feat(workbench): detect chapter candidates"
```

---

## 任务 4：Markdown 同步

**文件：**
- 创建：`src/parsing_core/workbench/markdown_sync.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 测试：`tests/test_workbench/test_markdown_sync.py`

- [ ] **步骤 1：编写失败测试**

创建 `tests/test_workbench/test_markdown_sync.py`：

```python
from pathlib import Path

from parsing_core.storage.schema import init_db
from parsing_core.workbench.markdown_sync import sync_chapter_markdown
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


def test_sync_chapter_markdown_writes_note_cards_and_mermaid(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(tmp_path / "source.md"))

    Path(chapter.source_md_path).write_text("## 第一章\n原文", encoding="utf-8")
    repo.upsert_note_block(chapter.id, "summary", "本章概要", "战略是取舍。", 0)
    repo.upsert_note_block(chapter.id, "knowledge_mermaid", "知识结构图", "flowchart TD\nA-->B", 1)
    repo.create_card(course.id, chapter.id, "topic", "为什么战略不是口号", "一个可写选题。")

    paths = sync_chapter_markdown(repo, chapter.id)

    note = Path(paths["note"]).read_text(encoding="utf-8")
    cards = Path(paths["cards"]).read_text(encoding="utf-8")
    assert "## 本章概要" in note
    assert "```mermaid" in note
    assert "flowchart TD" in note
    assert "为什么战略不是口号" in cards
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_markdown_sync.py -q`

预期：FAIL，报错包含 `No module named 'parsing_core.workbench.markdown_sync'` 或 `upsert_note_block` 缺失。

- [ ] **步骤 3：给 Repository 增加块 CRUD**

修改 `src/parsing_core/workbench/repository.py`，追加 import 和方法：

```python
from parsing_core.workbench.models import Card, Chapter, Course, NoteBlock, Source
```

在 `WorkbenchRepository` 内追加：

```python
    def upsert_note_block(
        self, chapter_id: str, kind: str, title: str, body: str, seq: int
    ) -> NoteBlock:
        now = _now()
        existing = self.conn.execute(
            "SELECT id FROM wb_note_blocks WHERE chapter_id = ? AND kind = ?",
            (chapter_id, kind),
        ).fetchone()
        block_id = existing[0] if existing else str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO wb_note_blocks (id, chapter_id, kind, title, body, seq, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chapter_id, kind) DO UPDATE SET "
            "title = excluded.title, body = excluded.body, seq = excluded.seq, updated_at = excluded.updated_at",
            (block_id, chapter_id, kind, title, body, seq, now),
        )
        self.conn.commit()
        return NoteBlock(block_id, chapter_id, kind, title, body, seq, now)

    def list_note_blocks(self, chapter_id: str) -> list[NoteBlock]:
        rows = self.conn.execute(
            "SELECT id, chapter_id, kind, title, body, seq, updated_at "
            "FROM wb_note_blocks WHERE chapter_id = ? ORDER BY seq",
            (chapter_id,),
        ).fetchall()
        return [NoteBlock(*row) for row in rows]

    def list_cards_by_chapter(self, chapter_id: str) -> list[Card]:
        rows = self.conn.execute(
            "SELECT id, course_id, chapter_id, kind, title, body, favorite, created_at, updated_at "
            "FROM wb_cards WHERE chapter_id = ? ORDER BY updated_at DESC",
            (chapter_id,),
        ).fetchall()
        return [Card(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8]) for r in rows]
```

- [ ] **步骤 4：实现 Markdown 同步**

创建 `src/parsing_core/workbench/markdown_sync.py`：

```python
import re
from pathlib import Path

from parsing_core.workbench.repository import WorkbenchRepository


def _safe_name(value: str) -> str:
    return re.sub(r"[\\/]+", "-", value).strip() or "chapter"


def _chapter_dir(repo: WorkbenchRepository, chapter_id: str) -> Path:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError(f"chapter not found: {chapter_id}")
    course = repo.get_course(chapter.course_id)
    if course is None:
        raise ValueError(f"course not found: {chapter.course_id}")
    return Path(course.root_dir) / f"{chapter.seq + 1:02d}-{_safe_name(chapter.title)}"


def sync_chapter_markdown(repo: WorkbenchRepository, chapter_id: str) -> dict[str, str]:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError(f"chapter not found: {chapter_id}")

    chapter_dir = _chapter_dir(repo, chapter_id)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / "attachments").mkdir(exist_ok=True)
    (chapter_dir / "runs").mkdir(exist_ok=True)

    source_path = chapter_dir / "source.md"
    if chapter.source_md_path and Path(chapter.source_md_path).exists():
        source_path.write_text(Path(chapter.source_md_path).read_text(encoding="utf-8"), encoding="utf-8")

    note_lines = [f"# {chapter.title}", ""]
    for block in repo.list_note_blocks(chapter_id):
        note_lines.append(f"## {block.title}")
        note_lines.append("")
        if block.kind.endswith("_mermaid"):
            note_lines.append("```mermaid")
            note_lines.append(block.body.strip())
            note_lines.append("```")
        else:
            note_lines.append(block.body.strip())
        note_lines.append("")

    cards = repo.list_cards_by_chapter(chapter_id)
    card_lines = [f"# {chapter.title} 写作卡片", ""]
    for card in cards:
        card_lines.append(f"## {card.title}")
        card_lines.append("")
        card_lines.append(f"- 类型: {card.kind}")
        card_lines.append(f"- 收藏: {'是' if card.favorite else '否'}")
        card_lines.append("")
        card_lines.append(card.body.strip())
        card_lines.append("")

    note_path = chapter_dir / "intensive-note.md"
    cards_path = chapter_dir / "cards.md"
    note_path.write_text("\n".join(note_lines).rstrip() + "\n", encoding="utf-8")
    cards_path.write_text("\n".join(card_lines).rstrip() + "\n", encoding="utf-8")
    return {"source": str(source_path), "note": str(note_path), "cards": str(cards_path)}
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_markdown_sync.py -q`

预期：`1 passed`。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/repository.py src/parsing_core/workbench/markdown_sync.py tests/test_workbench/test_markdown_sync.py
git commit -m "feat(workbench): sync structured notes to markdown"
```

---

## 任务 5：任务包与执行器

**文件：**
- 创建：`src/parsing_core/workbench/task_package.py`
- 创建：`src/parsing_core/workbench/executors.py`
- 测试：`tests/test_workbench/test_task_package.py`

- [ ] **步骤 1：编写失败测试**

创建 `tests/test_workbench/test_task_package.py`：

```python
from pathlib import Path

from parsing_core.storage.schema import init_db
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.task_package import build_task_package, write_task_package


def test_task_package_contains_rules_and_source(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "MBA", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    source_md = tmp_path / "ch1.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(source_md))

    package = build_task_package(repo, chapter.id, "concepts")
    path = write_task_package(package, tmp_path)

    text = Path(path).read_text(encoding="utf-8")
    assert "战略是选择" in text
    assert "两张 Mermaid 图" in text


def test_stub_executor_returns_deterministic_output():
    output = StubIntensiveReadingExecutor().run("cards", "input")
    assert "选题卡" in output
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_task_package.py -q`

预期：FAIL，报错包含 `No module named 'parsing_core.workbench.task_package'`。

- [ ] **步骤 3：实现任务包**

创建 `src/parsing_core/workbench/task_package.py`：

```python
from dataclasses import dataclass
from pathlib import Path

from parsing_core.workbench.repository import WorkbenchRepository


READING_RULES = """你是用户的 MBA 精读助教。
要求：概念通俗、有趣、生活化；保留严谨性；结合案例；落到实际应用；服务贴文和公众号长文素材。
每章最终必须包含两张 Mermaid 图：知识结构图和应用流程图。
"""


@dataclass
class TaskPackage:
    chapter_id: str
    round_key: str
    title: str
    content: str


def build_task_package(repo: WorkbenchRepository, chapter_id: str, round_key: str) -> TaskPackage:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError(f"chapter not found: {chapter_id}")
    source = Path(chapter.source_md_path).read_text(encoding="utf-8")
    content = (
        f"# 精读任务包: {chapter.title}\n\n"
        f"## 轮次\n{round_key}\n\n"
        f"## 规则\n{READING_RULES}\n"
        f"## 原文\n{source}\n"
    )
    return TaskPackage(chapter_id=chapter_id, round_key=round_key, title=chapter.title, content=content)


def write_task_package(package: TaskPackage, base_dir: str | Path) -> str:
    target = Path(base_dir) / f"{package.chapter_id}-{package.round_key}-task.md"
    target.write_text(package.content, encoding="utf-8")
    return str(target)
```

- [ ] **步骤 4：实现执行器接口和 Stub**

创建 `src/parsing_core/workbench/executors.py`：

```python
from typing import Protocol


class IntensiveReadingExecutor(Protocol):
    def run(self, round_key: str, task_package: str) -> str:
        ...


class StubIntensiveReadingExecutor:
    def run(self, round_key: str, task_package: str) -> str:
        if round_key == "mermaid":
            return (
                "## Mermaid 图\n\n"
                "```mermaid\nflowchart TD\nA[核心概念] --> B[现实应用]\n```\n\n"
                "```mermaid\nflowchart LR\nP[问题] --> M[模型分析] --> A[行动]\n```\n"
            )
        if round_key == "cards":
            return "## 写作卡片\n\n### 选题卡\n为什么这个概念值得重写一遍？\n"
        return f"## {round_key}\n\n这是 {round_key} 的确定性精读输出。\n"


class ManualTaskPackageExecutor:
    def run(self, round_key: str, task_package: str) -> str:
        return task_package
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_task_package.py -q`

预期：`2 passed`。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/task_package.py src/parsing_core/workbench/executors.py tests/test_workbench/test_task_package.py
git commit -m "feat(workbench): add task packages and executors"
```

---

## 任务 6：多轮精读流水线

**文件：**
- 创建：`src/parsing_core/workbench/pipeline.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 测试：`tests/test_workbench/test_pipeline.py`

- [ ] **步骤 1：编写失败测试**

创建 `tests/test_workbench/test_pipeline.py`：

```python
from pathlib import Path

from parsing_core.storage.schema import init_db
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.pipeline import IntensiveReadingPipeline
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


def setup_chapter(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    source_md = tmp_path / "ch1.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(source_md))
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    return repo, chapter


def test_pipeline_creates_blocks_cards_and_runs(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    pipeline.run_all(chapter.id)

    blocks = repo.list_note_blocks(chapter.id)
    runs = repo.list_runs(chapter.id)
    cards = repo.list_cards_by_chapter(chapter.id)
    assert {b.kind for b in blocks} >= {"summary", "knowledge_mermaid", "application_mermaid"}
    assert len(cards) >= 1
    assert len(runs) == 7


def test_rerun_marks_later_rounds_stale(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)

    pipeline.rerun(chapter.id, "concepts")

    stale = {r.round_key for r in repo.list_runs(chapter.id) if r.stale}
    assert {"plain_explain", "application", "mermaid", "cards", "review"} <= stale
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_pipeline.py -q`

预期：FAIL，报错包含 `No module named 'parsing_core.workbench.pipeline'` 或 `list_runs` 缺失。

- [ ] **步骤 3：给 Repository 增加运行记录方法**

修改 `src/parsing_core/workbench/repository.py`，import 增加 `RunRecord`：

```python
from parsing_core.workbench.models import Card, Chapter, Course, NoteBlock, RunRecord, Source
```

追加方法：

```python
    def upsert_run(
        self,
        chapter_id: str,
        round_key: str,
        executor: str,
        status: str,
        input_path: str,
        output_path: str,
        output: str,
        stale: bool = False,
    ) -> RunRecord:
        now = _now()
        existing = self.conn.execute(
            "SELECT id FROM wb_runs WHERE chapter_id = ? AND round_key = ?",
            (chapter_id, round_key),
        ).fetchone()
        run_id = existing[0] if existing else str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO wb_runs (id, chapter_id, round_key, executor, status, input_path, output_path, output, stale, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chapter_id, round_key) DO UPDATE SET "
            "executor = excluded.executor, status = excluded.status, input_path = excluded.input_path, "
            "output_path = excluded.output_path, output = excluded.output, stale = excluded.stale, updated_at = excluded.updated_at",
            (run_id, chapter_id, round_key, executor, status, input_path, output_path, output, 1 if stale else 0, now, now),
        )
        self.conn.commit()
        return RunRecord(run_id, chapter_id, round_key, executor, status, input_path, output_path, output, stale, now, now)

    def list_runs(self, chapter_id: str) -> list[RunRecord]:
        rows = self.conn.execute(
            "SELECT id, chapter_id, round_key, executor, status, input_path, output_path, output, stale, created_at, updated_at "
            "FROM wb_runs WHERE chapter_id = ? ORDER BY created_at",
            (chapter_id,),
        ).fetchall()
        return [RunRecord(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]), r[9], r[10]) for r in rows]

    def mark_runs_stale(self, chapter_id: str, round_keys: list[str]) -> None:
        if not round_keys:
            return
        placeholders = ",".join("?" for _ in round_keys)
        self.conn.execute(
            f"UPDATE wb_runs SET stale = 1, updated_at = ? WHERE chapter_id = ? AND round_key IN ({placeholders})",
            (_now(), chapter_id, *round_keys),
        )
        self.conn.commit()
```

- [ ] **步骤 4：实现流水线**

创建 `src/parsing_core/workbench/pipeline.py`：

```python
from pathlib import Path

from parsing_core.workbench.executors import IntensiveReadingExecutor
from parsing_core.workbench.markdown_sync import sync_chapter_markdown
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.task_package import build_task_package, write_task_package


ROUNDS = [
    "structure",
    "concepts",
    "plain_explain",
    "application",
    "mermaid",
    "cards",
    "review",
]


class IntensiveReadingPipeline:
    def __init__(
        self,
        repo: WorkbenchRepository,
        executor: IntensiveReadingExecutor,
        run_dir: str | Path,
    ) -> None:
        self.repo = repo
        self.executor = executor
        self.run_dir = Path(run_dir)

    def run_all(self, chapter_id: str) -> None:
        chapter = self.repo.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError(f"chapter not found: {chapter_id}")
        if chapter.status != "CONFIRMED":
            raise ValueError("chapter must be CONFIRMED before intensive reading")
        for round_key in ROUNDS:
            self._run_round(chapter_id, round_key)
        sync_chapter_markdown(self.repo, chapter_id)

    def rerun(self, chapter_id: str, round_key: str) -> None:
        self._run_round(chapter_id, round_key)
        index = ROUNDS.index(round_key)
        self.repo.mark_runs_stale(chapter_id, ROUNDS[index + 1 :])
        sync_chapter_markdown(self.repo, chapter_id)

    def _run_round(self, chapter_id: str, round_key: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        package = build_task_package(self.repo, chapter_id, round_key)
        input_path = write_task_package(package, self.run_dir)
        output = self.executor.run(round_key, package.content)
        output_path = self.run_dir / f"{chapter_id}-{round_key}-output.md"
        output_path.write_text(output, encoding="utf-8")
        self.repo.upsert_run(
            chapter_id,
            round_key,
            self.executor.__class__.__name__,
            "COMPLETED",
            input_path,
            str(output_path),
            output,
            stale=False,
        )
        self._materialize_round(chapter_id, round_key, output)

    def _materialize_round(self, chapter_id: str, round_key: str, output: str) -> None:
        if round_key == "structure":
            self.repo.upsert_note_block(chapter_id, "summary", "本章概要", output, 0)
        elif round_key == "concepts":
            self.repo.upsert_note_block(chapter_id, "concepts", "核心概念", output, 1)
        elif round_key == "plain_explain":
            self.repo.upsert_note_block(chapter_id, "plain_explain", "通俗解释", output, 2)
        elif round_key == "application":
            self.repo.upsert_note_block(chapter_id, "application", "现实应用", output, 3)
        elif round_key == "mermaid":
            self.repo.upsert_note_block(chapter_id, "knowledge_mermaid", "知识结构图", "flowchart TD\nA[核心概念] --> B[关键模型]", 4)
            self.repo.upsert_note_block(chapter_id, "application_mermaid", "应用流程图", "flowchart LR\nP[现实问题] --> M[模型分析] --> S[解决方案]", 5)
        elif round_key == "review":
            self.repo.upsert_note_block(chapter_id, "reflection", "延伸思考", output, 6)
        elif round_key == "cards":
            chapter = self.repo.get_chapter(chapter_id)
            if chapter is None:
                return
            self.repo.create_card(chapter.course_id, chapter_id, "topic", "选题卡：重读这一章", output)
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_pipeline.py -q`

预期：`2 passed`。

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/workbench/repository.py src/parsing_core/workbench/pipeline.py tests/test_workbench/test_pipeline.py
git commit -m "feat(workbench): add intensive reading pipeline"
```

---

## 任务 7：工作台 API

**文件：**
- 修改：`src/parsing_core/serving/models/api.py`
- 创建：`src/parsing_core/serving/api/routes_workbench.py`
- 修改：`src/parsing_core/serving/serve.py`
- 测试：`tests/test_workbench/test_api.py`

- [ ] **步骤 1：编写失败 API 测试**

创建 `tests/test_workbench/test_api.py`：

```python
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench.schema import apply_workbench_schema


def client(tmp_path):
    db_path = tmp_path / "serve.db"

    def factory():
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        return Orchestrator(Repository(conn), FsLayout(base_dir=str(tmp_path / "fs")), StubLLMClient(), str(db_path))

    return TestClient(build_app(factory))


def test_create_course_and_list(tmp_path):
    c = client(tmp_path)
    res = c.post("/api/workbench/courses", json={"title": "战略管理", "description": "MBA", "root_dir": str(tmp_path / "out")})
    assert res.status_code == 200
    course_id = res.json()["id"]

    res = c.get("/api/workbench/courses")
    assert res.status_code == 200
    assert res.json()[0]["id"] == course_id


def test_confirm_chapter_then_run_pipeline(tmp_path):
    c = client(tmp_path)
    course = c.post("/api/workbench/courses", json={"title": "战略管理", "description": "", "root_dir": str(tmp_path / "out")}).json()
    source_md = tmp_path / "source.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    source = c.post(f"/api/workbench/courses/{course['id']}/sources", json={"kind": "main", "file_path": str(source_md), "title": "战略教材"}).json()

    res = c.post(f"/api/workbench/sources/{source['id']}/detect-chapters")
    assert res.status_code == 200
    chapter_id = res.json()[0]["id"]
    c.post(f"/api/workbench/chapters/{chapter_id}/confirm")

    res = c.post(f"/api/workbench/chapters/{chapter_id}/run", json={"executor": "stub"})
    assert res.status_code == 200
    assert res.json()["status"] == "COMPLETED"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/pytest tests/test_workbench/test_api.py -q`

预期：FAIL，返回 404 或 import 错误。

- [ ] **步骤 3：增加 Pydantic API 模型**

修改 `src/parsing_core/serving/models/api.py`，追加：

```python
class CourseCreateRequest(BaseModel):
    title: str
    description: str = ""
    root_dir: str


class CourseResponse(BaseModel):
    id: str
    title: str
    description: str
    root_dir: str


class SourceCreateRequest(BaseModel):
    kind: str = "main"
    file_path: str
    title: str


class SourceResponse(BaseModel):
    id: str
    course_id: str
    kind: str
    file_path: str
    title: str
    status: str


class ChapterResponse(BaseModel):
    id: str
    source_id: str
    course_id: str
    seq: int
    title: str
    status: str


class RunChapterRequest(BaseModel):
    executor: str = "stub"
```

- [ ] **步骤 4：实现 workbench router**

创建 `src/parsing_core/serving/api/routes_workbench.py`：

```python
from pathlib import Path

from fastapi import APIRouter, HTTPException

from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.serving.api.deps import SchedulerDep
from parsing_core.serving.models.api import (
    ChapterResponse,
    CourseCreateRequest,
    CourseResponse,
    RunChapterRequest,
    SourceCreateRequest,
    SourceResponse,
)
from parsing_core.workbench.chapter_detection import detect_chapters
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.pipeline import IntensiveReadingPipeline
from parsing_core.workbench.repository import WorkbenchRepository

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


def _repo(sch: SchedulerDep) -> WorkbenchRepository:
    return WorkbenchRepository(sch._query_orch.repo.conn)


@router.post("/courses", response_model=CourseResponse)
async def create_course(req: CourseCreateRequest, sch: SchedulerDep):
    c = _repo(sch).create_course(req.title, req.description, req.root_dir)
    return CourseResponse(id=c.id, title=c.title, description=c.description, root_dir=c.root_dir)


@router.get("/courses", response_model=list[CourseResponse])
async def list_courses(sch: SchedulerDep):
    return [
        CourseResponse(id=c.id, title=c.title, description=c.description, root_dir=c.root_dir)
        for c in _repo(sch).list_courses()
    ]


@router.post("/courses/{course_id}/sources", response_model=SourceResponse)
async def create_source(course_id: str, req: SourceCreateRequest, sch: SchedulerDep):
    repo = _repo(sch)
    if repo.get_course(course_id) is None:
        raise HTTPException(404, "course not found")
    s = repo.create_source(course_id, req.kind, req.file_path, req.title)
    return SourceResponse(id=s.id, course_id=s.course_id, kind=s.kind, file_path=s.file_path, title=s.title, status=s.status)


@router.post("/sources/{source_id}/detect-chapters", response_model=list[ChapterResponse])
async def detect_source_chapters(source_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    sources = [s for c in repo.list_courses() for s in repo.list_sources(c.id)]
    source = next((s for s in sources if s.id == source_id), None)
    if source is None:
        raise HTTPException(404, "source not found")
    path = Path(source.file_path)
    raw = path.read_text(encoding="utf-8") if path.suffix.lower() in {".md", ".txt"} else MarkItDownAdapter().parse(str(path))
    chapters = []
    for candidate in detect_chapters(raw):
        chapter_md = Path(repo.get_course(source.course_id).root_dir) / source.title / f"{candidate.seq + 1:02d}-{candidate.title}.md"
        chapter_md.parent.mkdir(parents=True, exist_ok=True)
        chapter_md.write_text(candidate.raw_md, encoding="utf-8")
        ch = repo.create_chapter(source.course_id, source.id, candidate.seq, candidate.title, str(chapter_md))
        chapters.append(ChapterResponse(id=ch.id, source_id=ch.source_id, course_id=ch.course_id, seq=ch.seq, title=ch.title, status=ch.status))
    return chapters


@router.post("/chapters/{chapter_id}/confirm", response_model=ChapterResponse)
async def confirm_chapter(chapter_id: str, sch: SchedulerDep):
    repo = _repo(sch)
    repo.update_chapter_status(chapter_id, "CONFIRMED")
    ch = repo.get_chapter(chapter_id)
    if ch is None:
        raise HTTPException(404, "chapter not found")
    return ChapterResponse(id=ch.id, source_id=ch.source_id, course_id=ch.course_id, seq=ch.seq, title=ch.title, status=ch.status)


@router.post("/chapters/{chapter_id}/run")
async def run_chapter(chapter_id: str, req: RunChapterRequest, sch: SchedulerDep):
    if req.executor != "stub":
        raise HTTPException(400, "only stub executor is wired in first implementation task")
    repo = _repo(sch)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), Path(sch._query_orch.fs.base_dir) / "workbench-runs")
    pipeline.run_all(chapter_id)
    return {"chapter_id": chapter_id, "status": "COMPLETED"}
```

- [ ] **步骤 5：接入 router 和 schema**

修改 `src/parsing_core/serving/serve.py`：

```python
from parsing_core.serving.api.routes_workbench import router as workbench_router
```

在 `build_app()` 中 include：

```python
    app.include_router(workbench_router)
```

在 `main()` 的 `orch_factory()` 中 `apply_serve_schema(conn)` 后追加：

```python
        from parsing_core.workbench.schema import apply_workbench_schema

        apply_workbench_schema(conn)
```

- [ ] **步骤 6：运行测试验证通过**

运行：`.venv/bin/pytest tests/test_workbench/test_api.py -q`

预期：`2 passed`。

- [ ] **步骤 7：Commit**

```bash
git add src/parsing_core/serving/models/api.py src/parsing_core/serving/api/routes_workbench.py src/parsing_core/serving/serve.py tests/test_workbench/test_api.py
git commit -m "feat(workbench): add REST API"
```

---

## 任务 8：前端 API、状态和路由骨架

**文件：**
- 创建：`parsing-core-app/src/api/workbenchTypes.ts`
- 创建：`parsing-core-app/src/api/workbench.ts`
- 创建：`parsing-core-app/src/store/useWorkbenchStore.ts`
- 修改：`parsing-core-app/src/App.tsx`
- 修改：`parsing-core-app/src/components/Layout.tsx`

- [ ] **步骤 1：新增 TypeScript 类型**

创建 `parsing-core-app/src/api/workbenchTypes.ts`：

```typescript
export interface Course {
  id: string;
  title: string;
  description: string;
  root_dir: string;
}

export interface Source {
  id: string;
  course_id: string;
  kind: string;
  file_path: string;
  title: string;
  status: string;
}

export interface Chapter {
  id: string;
  source_id: string;
  course_id: string;
  seq: number;
  title: string;
  status: string;
}

export interface Card {
  id: string;
  course_id: string;
  chapter_id: string;
  kind: string;
  title: string;
  body: string;
  favorite: boolean;
}
```

- [ ] **步骤 2：新增 API 客户端**

创建 `parsing-core-app/src/api/workbench.ts`：

```typescript
import type { Chapter, Course, Source } from "./workbenchTypes";

const BASE = "http://127.0.0.1:8000";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export function listCourses(): Promise<Course[]> {
  return fetch(`${BASE}/api/workbench/courses`).then(json<Course[]>);
}

export function createCourse(input: { title: string; description: string; root_dir: string }): Promise<Course> {
  return fetch(`${BASE}/api/workbench/courses`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }).then(json<Course>);
}

export function createSource(courseId: string, input: { kind: string; file_path: string; title: string }): Promise<Source> {
  return fetch(`${BASE}/api/workbench/courses/${courseId}/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }).then(json<Source>);
}

export function detectChapters(sourceId: string): Promise<Chapter[]> {
  return fetch(`${BASE}/api/workbench/sources/${sourceId}/detect-chapters`, { method: "POST" }).then(json<Chapter[]>);
}

export function confirmChapter(chapterId: string): Promise<Chapter> {
  return fetch(`${BASE}/api/workbench/chapters/${chapterId}/confirm`, { method: "POST" }).then(json<Chapter>);
}

export function runChapter(chapterId: string): Promise<{ chapter_id: string; status: string }> {
  return fetch(`${BASE}/api/workbench/chapters/${chapterId}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ executor: "stub" }),
  }).then(json<{ chapter_id: string; status: string }>);
}
```

- [ ] **步骤 3：新增 Zustand store**

创建 `parsing-core-app/src/store/useWorkbenchStore.ts`：

```typescript
import { create } from "zustand";
import * as api from "../api/workbench";
import type { Chapter, Course, Source } from "../api/workbenchTypes";

interface WorkbenchState {
  courses: Course[];
  sources: Source[];
  chapters: Chapter[];
  selectedCourseId: string | null;
  loadCourses: () => Promise<void>;
  createCourse: (title: string, description: string, rootDir: string) => Promise<Course>;
  addSource: (courseId: string, filePath: string, title: string) => Promise<Source>;
  detectChapters: (sourceId: string) => Promise<void>;
  confirmChapter: (chapterId: string) => Promise<void>;
  runChapter: (chapterId: string) => Promise<void>;
}

export const useWorkbenchStore = create<WorkbenchState>((set, get) => ({
  courses: [],
  sources: [],
  chapters: [],
  selectedCourseId: null,

  loadCourses: async () => {
    set({ courses: await api.listCourses() });
  },

  createCourse: async (title, description, root_dir) => {
    const course = await api.createCourse({ title, description, root_dir });
    set((s) => ({ courses: [course, ...s.courses], selectedCourseId: course.id }));
    return course;
  },

  addSource: async (courseId, file_path, title) => {
    const source = await api.createSource(courseId, { kind: "main", file_path, title });
    set((s) => ({ sources: [source, ...s.sources] }));
    return source;
  },

  detectChapters: async (sourceId) => {
    const chapters = await api.detectChapters(sourceId);
    set({ chapters });
  },

  confirmChapter: async (chapterId) => {
    const chapter = await api.confirmChapter(chapterId);
    set((s) => ({ chapters: s.chapters.map((c) => (c.id === chapterId ? chapter : c)) }));
  },

  runChapter: async (chapterId) => {
    await api.runChapter(chapterId);
    await get().confirmChapter(chapterId);
  },
}));
```

- [ ] **步骤 4：创建临时页面组件**

创建 `parsing-core-app/src/components/workbench/CourseList.tsx`：

```tsx
import { useEffect, useState } from "react";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function CourseList() {
  const { courses, loadCourses, createCourse } = useWorkbenchStore();
  const [title, setTitle] = useState("");

  useEffect(() => { loadCourses(); }, [loadCourses]);

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-900">课程资料精读</h1>
      <div className="flex gap-2">
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="课程名称" className="rounded-md border border-zinc-200 px-3 py-2 text-sm" />
        <button onClick={() => title && createCourse(title, "", `${window.location.origin}/workbench-${Date.now()}`)} className="rounded-md bg-zinc-900 px-3 py-2 text-sm text-white">创建课程</button>
      </div>
      <div className="space-y-2">
        {courses.map((c) => (
          <div key={c.id} className="rounded-lg border border-zinc-200 bg-white p-4">
            <div className="font-medium text-zinc-900">{c.title}</div>
            <div className="text-xs text-zinc-400">{c.root_dir}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **步骤 5：接入路由和导航**

修改 `parsing-core-app/src/App.tsx`：

```tsx
import CourseList from "./components/workbench/CourseList";
```

在 `<Route element={<Layout />}>` 内追加：

```tsx
          <Route path="/workbench" element={<CourseList />} />
```

修改 `parsing-core-app/src/components/Layout.tsx`：

```tsx
import { LayoutDashboard, PlusCircle, FileText, Terminal, BookOpen } from "lucide-react";
```

在 `nav` 中追加：

```tsx
  { to: "/workbench", label: "课程精读", icon: BookOpen },
```

- [ ] **步骤 6：运行前端类型检查**

运行：`cd parsing-core-app && npm run build`

预期：build 成功。

- [ ] **步骤 7：Commit**

```bash
git add parsing-core-app/src/api/workbenchTypes.ts parsing-core-app/src/api/workbench.ts parsing-core-app/src/store/useWorkbenchStore.ts parsing-core-app/src/components/workbench/CourseList.tsx parsing-core-app/src/App.tsx parsing-core-app/src/components/Layout.tsx
git commit -m "feat(webui): add workbench route scaffold"
```

---

## 任务 9：前端章节确认、精读页和卡片池

**文件：**
- 创建：`parsing-core-app/src/components/workbench/SourceDetail.tsx`
- 创建：`parsing-core-app/src/components/workbench/ChapterConfirm.tsx`
- 创建：`parsing-core-app/src/components/workbench/ChapterWorkbench.tsx`
- 创建：`parsing-core-app/src/components/workbench/CardPool.tsx`
- 创建：`parsing-core-app/src/components/workbench/MermaidEditor.tsx`
- 修改：`parsing-core-app/src/App.tsx`

- [ ] **步骤 1：新增 Mermaid 编辑器**

创建 `parsing-core-app/src/components/workbench/MermaidEditor.tsx`：

```tsx
import { useState } from "react";
import MermaidBlock from "../MermaidBlock";

export default function MermaidEditor({ initial }: { initial: string }) {
  const [code, setCode] = useState(initial);
  return (
    <div className="grid grid-cols-2 gap-4">
      <textarea
        value={code}
        onChange={(e) => setCode(e.target.value)}
        className="h-64 rounded-md border border-zinc-200 p-3 font-mono text-xs"
      />
      <div className="rounded-md border border-zinc-200 bg-white p-4">
        <MermaidBlock code={code} />
      </div>
    </div>
  );
}
```

- [ ] **步骤 2：新增章节确认页**

创建 `parsing-core-app/src/components/workbench/ChapterConfirm.tsx`：

```tsx
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function ChapterConfirm() {
  const { chapters, confirmChapter, runChapter } = useWorkbenchStore();
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-zinc-900">章节确认</h1>
      {chapters.map((c) => (
        <div key={c.id} className="rounded-lg border border-zinc-200 bg-white p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="font-medium text-zinc-900">{c.seq + 1}. {c.title}</div>
              <div className="text-xs text-zinc-400">{c.status}</div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => confirmChapter(c.id)} className="rounded-md border border-zinc-200 px-3 py-1.5 text-sm">确认</button>
              <button onClick={() => runChapter(c.id)} className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm text-white">多轮精读</button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **步骤 3：新增主资料详情页**

创建 `parsing-core-app/src/components/workbench/SourceDetail.tsx`：

```tsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function SourceDetail() {
  const { courses, addSource, detectChapters } = useWorkbenchStore();
  const [filePath, setFilePath] = useState("");
  const [title, setTitle] = useState("");
  const navigate = useNavigate();
  const course = courses[0];

  const submit = async () => {
    if (!course || !filePath || !title) return;
    const source = await addSource(course.id, filePath, title);
    await detectChapters(source.id);
    navigate("/workbench/chapters");
  };

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-zinc-900">导入主资料</h1>
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="资料标题" className="block w-full rounded-md border border-zinc-200 px-3 py-2 text-sm" />
      <input value={filePath} onChange={(e) => setFilePath(e.target.value)} placeholder="PDF/Word/Markdown 绝对路径" className="block w-full rounded-md border border-zinc-200 px-3 py-2 text-sm" />
      <button onClick={submit} className="rounded-md bg-zinc-900 px-4 py-2 text-sm text-white">识别章节</button>
    </div>
  );
}
```

- [ ] **步骤 4：新增章节精读页**

创建 `parsing-core-app/src/components/workbench/ChapterWorkbench.tsx`：

```tsx
import MermaidEditor from "./MermaidEditor";

export default function ChapterWorkbench() {
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-zinc-900">章节精读</h1>
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h2 className="mb-3 text-sm font-medium text-zinc-700">知识结构图</h2>
        <MermaidEditor initial={"flowchart TD\nA[核心概念] --> B[关键模型]"} />
      </section>
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h2 className="mb-3 text-sm font-medium text-zinc-700">应用流程图</h2>
        <MermaidEditor initial={"flowchart LR\nP[现实问题] --> M[模型分析] --> S[解决方案]"} />
      </section>
    </div>
  );
}
```

- [ ] **步骤 5：新增卡片池页**

创建 `parsing-core-app/src/components/workbench/CardPool.tsx`：

```tsx
export default function CardPool() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-zinc-900">课程卡片池</h1>
      <div className="rounded-lg border border-dashed border-zinc-300 bg-white p-8 text-center text-sm text-zinc-400">
        暂无卡片。完成章节精读后，选题卡、观点卡、案例卡、类比卡和应用卡会汇总到这里。
      </div>
    </div>
  );
}
```

- [ ] **步骤 6：接入路由**

修改 `parsing-core-app/src/App.tsx`：

```tsx
import SourceDetail from "./components/workbench/SourceDetail";
import ChapterConfirm from "./components/workbench/ChapterConfirm";
import ChapterWorkbench from "./components/workbench/ChapterWorkbench";
import CardPool from "./components/workbench/CardPool";
```

追加路由：

```tsx
          <Route path="/workbench/source" element={<SourceDetail />} />
          <Route path="/workbench/chapters" element={<ChapterConfirm />} />
          <Route path="/workbench/chapter" element={<ChapterWorkbench />} />
          <Route path="/workbench/cards" element={<CardPool />} />
```

- [ ] **步骤 7：运行前端构建**

运行：`cd parsing-core-app && npm run build`

预期：build 成功，Mermaid 组件无 TS 错误。

- [ ] **步骤 8：Commit**

```bash
git add parsing-core-app/src/components/workbench parsing-core-app/src/App.tsx
git commit -m "feat(webui): add workbench pages"
```

---

## 任务 10：全量验证与收尾

**文件：**
- 修改：`README.md`
- 修改：`docs/superpowers/specs/2026-07-08-course-intensive-reading-workbench-design.md`（仅当实现发现规格需要同步）

- [ ] **步骤 1：运行后端 workbench 测试**

运行：

```bash
.venv/bin/pytest tests/test_workbench -q
```

预期：全部通过。

- [ ] **步骤 2：运行后端全量测试**

运行：

```bash
.venv/bin/pytest -q
```

预期：现有 154 个测试加新测试全部通过。

- [ ] **步骤 3：运行前端构建**

运行：

```bash
cd parsing-core-app && npm run build
```

预期：build 成功。

- [ ] **步骤 4：运行 Rust 检查**

运行：

```bash
cd parsing-core-app/src-tauri && cargo check
```

预期：check 成功。允许既有 sidecar dead-code warning 存在；不要在本任务中重构 Tauri 生命周期。

- [ ] **步骤 5：更新 README 的产品目标段**

修改 `README.md` 顶部简介，把“商业报表”改成课程资料精读方向：

```markdown
# PDF2MD —— 课程资料精读与写作辅助桌面应用

将 PDF、Word、PPT、Excel、图片等多格式资料转为 Markdown，并围绕课程/教材章节生成高质量精读笔记、Mermaid 知识图、应用流程图和写作卡片。
```

保留现有快速开始和技术栈，不在 README 中扩写完整规格。

- [ ] **步骤 6：最终状态检查**

运行：

```bash
git status --short
```

预期：只出现本计划相关文件修改。

- [ ] **步骤 7：Commit**

```bash
git add README.md
git commit -m "docs: update workbench product positioning"
```

---

## 自检

### 规格覆盖

- 课程级组织：任务 1、2、7、8。
- 多格式主资料/补充资料：任务 1、2、7 建立数据基础；本计划主资料导入支持 Markdown/PDF/Word 走 MarkItDown，补充资料挂载先落到后端表结构。
- 章节识别和人工确认：任务 3、7、9。
- 多轮精读：任务 5、6。
- Mermaid 两图和直接预览：任务 6、9。
- API/Codex/人工执行器：任务 5 建统一接口和 Stub/Manual；真实 DeepSeek/OpenAI API 与 Codex CLI 调用是独立执行器实现计划。
- 结构化编辑：任务 4、9 建块级编辑和 Mermaid 预览基础。
- SQLite + Markdown 双轨：任务 1、4、6。
- 课程卡片池：任务 2、6、9。

### 范围说明

本计划产出第一阶段可运行骨架和核心后端能力。规格中的真实 DeepSeek/OpenAI API 执行器、Codex CLI 调用器、补充资料前端挂载界面、块级保存 API、卡片池搜索 API 分属独立子系统，不混入本计划；这些能力必须分别生成独立实现计划后再开发。

### 命令汇总

```bash
.venv/bin/pytest tests/test_workbench -q
.venv/bin/pytest -q
cd parsing-core-app && npm run build
cd parsing-core-app/src-tauri && cargo check
```
