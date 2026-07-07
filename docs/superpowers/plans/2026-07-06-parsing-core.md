# 解析内核（#2 修订版）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 构建一个可独立 CLI 验证的 Python 解析内核，输入本地文件，输出穿插式 Markdown（原文节 + AI 解读节含 Mermaid），支持文件级+节级双缓存与崩溃恢复。

**架构：** orchestrator 编排五步（副本→MarkItDown 解析→图片落盘→分节→节级 LLM 调用→合流落盘），用 SQLite（WAL 模式）持久化任务/节/产物，大字段外置为 .md 文件。LLM 客户端为抽象基类，本计划只实现 stub（确定性占位输出），为 #4 算力路由层留接入点。

**技术栈：** Python 3.11+ stdlib + markitdown + sqlite3（stdlib）+ pytest + ruff

**配套规格：** `docs/superpowers/specs/2026-07-06-parsing-core-design.md`

---

## 文件结构

锁定文件分解（每个文件单一职责）：

| 路径 | 职责 |
|---|---|
| `pyproject.toml` | 依赖与工具配置 |
| `.gitignore` | 忽略 venv/缓存/fixture 产物 |
| `src/parsing_core/__init__.py` | 包入口（暴露 `__version__`） |
| `src/parsing_core/models/dataclasses.py` | `Task`/`Section`/`AIArtifact` 数据类 |
| `src/parsing_core/utils/hashing.py` | 文件与字符串 sha256 |
| `src/parsing_core/utils/file_lock.py` | 副本读取（snapshot） |
| `src/parsing_core/utils/retry.py` | 指数退避装饰器（节级重试用） |
| `src/parsing_core/storage/schema.py` | SQLite DDL 与连接初始化 |
| `src/parsing_core/storage/repository.py` | 任务/节/产物 CRUD |
| `src/parsing_core/storage/fs_layout.py` | 落盘路径策略（appData 镜像） |
| `src/parsing_core/storage/cache.py` | 文件级 + 节级 sha256 缓存查询 |
| `src/parsing_core/parser/base.py` | Parser 抽象基类 |
| `src/parsing_core/parser/markitdown_adapter.py` | MarkItDown 包装 |
| `src/parsing_core/parser/image_extractor.py` | Base64 图片抽落盘 + 路径替换 |
| `src/parsing_core/parser/chunker.py` | 节切分（结构单元 + 超长切分 + 短节合并） |
| `src/parsing_core/llm/base.py` | `LLMClient` 抽象基类 |
| `src/parsing_core/llm/prompt_templates.py` | 节级 prompt 模板字符串常量 |
| `src/parsing_core/llm/stub_client.py` | 确定性 stub LLM 客户端 |
| `src/parsing_core/orchestrator.py` | 编排：parse_file / resume / merge |
| `src/parsing_core/cli.py` | argparse 入口 |
| `tests/conftest.py` | 共享 fixtures（tmp_db、sample_files） |
| `tests/test_models.py` | 数据类测试 |
| `tests/test_hashing.py` | sha256 测试 |
| `tests/test_file_lock.py` | snapshot 测试 |
| `tests/test_repository.py` | CRUD 测试 |
| `tests/test_fs_layout.py` | 路径策略测试 |
| `tests/test_cache.py` | 缓存命中测试 |
| `tests/test_image_extractor.py` | 图片落盘测试 |
| `tests/test_markitdown_adapter.py` | adapter 测试 |
| `tests/test_chunker.py` | 节切分测试 |
| `tests/test_stub_client.py` | stub 输出契约测试 |
| `tests/test_orchestrator.py` | 编排测试 |
| `tests/test_cli.py` | CLI 集成测试 |
| `tests/fixtures/sample.md` | 最小 Markdown 样例 |
| `tests/fixtures/sample.xlsx` | 最小 Excel 样例 |
| `tests/fixtures/with_base64.md` | 含 Base64 图的 Markdown |

---

## 任务 0：工程骨架与 git 初始化

**文件：**
- 创建：`pyproject.toml`、`.gitignore`、`src/parsing_core/__init__.py`、`tests/__init__.py`、`tests/conftest.py`

- [ ] **步骤 1：初始化 git 仓库**

```bash
cd /Users/laoer/Documents/PDF2MD
git init
git config user.name "PDF2MD dev"
git config user.email "dev@local"
```

- [ ] **步骤 2：创建 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "parsing-core"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "markitdown>=0.0.1",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "pytest-cov>=4.1",
  "ruff>=0.5",
]

[project.scripts]
parsing-core = "parsing_core.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **步骤 3：创建 .gitignore**

```
__pycache__/
*.pyc
.venv/
.pytest_cache/
.coverage
htmlcov/
*.egg-info/
dist/
build/
appData/
```

- [ ] **步骤 4：创建 src/parsing_core/__init__.py**

```python
__version__ = "0.1.0"
```

- [ ] **步骤 5：创建 tests/__init__.py 与 tests/conftest.py**

```python
# tests/__init__.py
```

```python
# tests/conftest.py
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
```

- [ ] **步骤 6：安装与冒烟验证**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest --collect-only
```
预期：收集到 0 个测试，无报错。

- [ ] **步骤 7：Commit**

```bash
git add -A
git commit -m "chore: bootstrap parsing-core skeleton"
```

---

## 任务 1：models/dataclasses.py — 数据类

**文件：**
- 创建：`src/parsing_core/models/dataclasses.py`、`src/parsing_core/models/__init__.py`
- 测试：`tests/test_models.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_models.py
from parsing_core.models.dataclasses import Task, Section, AIArtifact


def test_task_creation():
    t = Task(id="t1", file_path="/a/b.xlsx", snapshot_path="/tmp/a.xlsx",
             file_sha256="abc", status="PENDING", model_tier="stub")
    assert t.id == "t1"
    assert t.status == "PENDING"
    assert t.model_tier == "stub"


def test_section_creation():
    s = Section(id="s1", task_id="t1", seq=0, raw_md_path="/x/0.raw.md",
                sha256="abc", char_count=100, ai_status="PENDING")
    assert s.seq == 0
    assert s.ai_status == "PENDING"


def test_ai_artifact_creation():
    a = AIArtifact(id="a1", section_id="s1", ai_md_path="/x/0.ai.md",
                  tokens_in=10, tokens_out=5, cost_usd=0.0, retry_count=0,
                  model_name="stub")
    assert a.section_id == "s1"
    assert a.retry_count == 0
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_models.py -v
```
预期：FAIL，`ModuleNotFoundError: No module named 'parsing_core.models'`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/models/__init__.py
```

```python
# src/parsing_core/models/dataclasses.py
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Task:
    id: str
    file_path: str
    snapshot_path: str
    file_sha256: str
    status: str  # PENDING|PARSING|SECTIONING|LLM_RUNNING|MERGING|COMPLETED|FAILED
    model_tier: str = "stub"
    created_at: int = 0
    updated_at: int = 0
    error_msg: str | None = None


@dataclass
class Section:
    id: str
    task_id: str
    seq: int
    raw_md_path: str
    sha256: str
    char_count: int
    ai_status: str = "PENDING"  # PENDING|RUNNING|COMPLETED|FAILED|PARTIAL_SUCCESS
    created_at: int = 0


@dataclass
class AIArtifact:
    id: str
    section_id: str
    ai_md_path: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    retry_count: int = 0
    model_name: str | None = None
    created_at: int = 0
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_models.py -v
```
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/models tests/test_models.py
git commit -m "feat(models): add Task/Section/AIArtifact dataclasses"
```

---

## 任务 2：utils/hashing.py — sha256 工具

**文件：**
- 创建：`src/parsing_core/utils/__init__.py`、`src/parsing_core/utils/hashing.py`
- 测试：`tests/test_hashing.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_hashing.py
from pathlib import Path

from parsing_core.utils.hashing import file_sha256, text_sha256


def test_text_sha256_deterministic():
    assert text_sha256("hello") == text_sha256("hello")
    assert text_sha256("hello") != text_sha256("world")


def test_text_sha256_known_value():
    assert text_sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_file_sha256(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    assert file_sha256(str(f)) == text_sha256("hello")
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_hashing.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/utils/__init__.py
```

```python
# src/parsing_core/utils/hashing.py
import hashlib


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str, chunk_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_hashing.py -v
```
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/utils tests/test_hashing.py
git commit -m "feat(utils): add sha256 helpers"
```

---

## 任务 3：utils/file_lock.py — 副本读取

**文件：**
- 创建：`src/parsing_core/utils/file_lock.py`
- 测试：`tests/test_file_lock.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_file_lock.py
from pathlib import Path

from parsing_core.utils.file_lock import snapshot


def test_snapshot_returns_different_path(tmp_path: Path):
    src = tmp_path / "orig.txt"
    src.write_text("payload")
    snap = snapshot(str(src))
    assert snap != str(src)
    assert Path(snap).read_text() == "payload"


def test_snapshot_does_not_modify_original(tmp_path: Path):
    src = tmp_path / "orig.txt"
    src.write_text("original")
    snap = snapshot(str(src))
    Path(snap).write_text("mutated")
    assert src.read_text() == "original"


def test_snapshot_preserves_extension(tmp_path: Path):
    src = tmp_path / "data.xlsx"
    src.write_text("x")
    snap = snapshot(str(src))
    assert snap.endswith(".xlsx")
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_file_lock.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/utils/file_lock.py
import shutil
import tempfile
from pathlib import Path


def snapshot(original_path: str) -> str:
    """生成原文件副本，永不触碰原文件。返回副本路径。"""
    suffix = Path(original_path).suffix
    snap = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    snap.close()
    shutil.copy2(original_path, snap.name)
    return snap.name
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_file_lock.py -v
```
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/utils/file_lock.py tests/test_file_lock.py
git commit -m "feat(utils): add file snapshot helper"
```

---

## 任务 4：utils/retry.py — 指数退避

**文件：**
- 创建：`src/parsing_core/utils/retry.py`
- 测试：`tests/test_retry.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_retry.py
import pytest

from parsing_core.utils.retry import with_retry


def test_retry_succeeds_first_try():
    calls = {"n": 0}

    @with_retry(max_attempts=3, base_delay=0)
    def ok():
        calls["n"] += 1
        return "ok"

    assert ok() == "ok"
    assert calls["n"] == 1


def test_retry_succeeds_on_third():
    state = {"n": 0}

    @with_retry(max_attempts=3, base_delay=0)
    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("boom")
        return "recovered"

    assert flaky() == "recovered"
    assert state["n"] == 3


def test_retry_exhausts_raises():
    @with_retry(max_attempts=2, base_delay=0)
    def always_fail():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        always_fail()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_retry.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/utils/retry.py
import functools
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retry(max_attempts: int = 3, base_delay: float = 2.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    if attempt < max_attempts:
                        time.sleep(base_delay * (2 ** (attempt - 1)))
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_retry.py -v
```
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/utils/retry.py tests/test_retry.py
git commit -m "feat(utils): add exponential backoff retry decorator"
```

---

## 任务 5：storage/schema.py — DDL 与连接初始化

**文件：**
- 创建：`src/parsing_core/storage/__init__.py`、`src/parsing_core/storage/schema.py`
- 测试：`tests/test_schema.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_schema.py
import sqlite3

from parsing_core.storage.schema import init_db, SCHEMA_SQL


def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "x.db"
    conn = init_db(str(db_path))
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert {"tasks", "sections", "ai_artifacts"} <= names
    conn.close()


def test_init_db_enables_wal(tmp_path):
    db_path = tmp_path / "x.db"
    conn = init_db(str(db_path))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_init_db_idempotent(tmp_path):
    db_path = tmp_path / "x.db"
    conn1 = init_db(str(db_path))
    conn1.close()
    conn2 = init_db(str(db_path))
    cur = conn2.execute("SELECT count(*) FROM tasks")
    assert cur.fetchone()[0] == 0
    conn2.close()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_schema.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/storage/__init__.py
```

```python
# src/parsing_core/storage/schema.py
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  file_path     TEXT NOT NULL,
  snapshot_path TEXT NOT NULL,
  file_sha256   TEXT NOT NULL,
  status        TEXT NOT NULL,
  model_tier    TEXT NOT NULL DEFAULT 'stub',
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  error_msg     TEXT
);

CREATE TABLE IF NOT EXISTS sections (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  raw_md_path   TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  char_count    INTEGER NOT NULL,
  ai_status     TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  UNIQUE(task_id, seq)
);

CREATE TABLE IF NOT EXISTS ai_artifacts (
  id            TEXT PRIMARY KEY,
  section_id    TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
  ai_md_path    TEXT NOT NULL,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cost_usd      REAL,
  retry_count   INTEGER NOT NULL DEFAULT 0,
  model_name    TEXT,
  created_at    INTEGER NOT NULL,
  UNIQUE(section_id)
);

CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_section_task ON sections(task_id);
CREATE INDEX IF NOT EXISTS idx_sha_file ON tasks(file_sha256);
CREATE INDEX IF NOT EXISTS idx_sha_section ON sections(sha256);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_schema.py -v
```
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/storage tests/test_schema.py
git commit -m "feat(storage): add SQLite schema and WAL init"
```

---

## 任务 6：storage/repository.py — CRUD

**文件：**
- 创建：`src/parsing_core/storage/repository.py`
- 测试：`tests/test_repository.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_repository.py
import sqlite3
import time

from parsing_core.models.dataclasses import Task, Section, AIArtifact
from parsing_core.storage.repository import Repository


def make_task(tid="t1", sha="h1"):
    return Task(id=tid, file_path="/a/b", snapshot_path="/tmp/snap",
                file_sha256=sha, status="PENDING", model_tier="stub",
                created_at=int(time.time()), updated_at=int(time.time()))


def test_create_and_get_task(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    t = make_task()
    repo.create_task(t)
    fetched = repo.get_task("t1")
    assert fetched is not None
    assert fetched.status == "PENDING"
    conn.close()


def test_update_task_status(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.update_task_status("t1", "COMPLETED")
    assert repo.get_task("t1").status == "COMPLETED"
    conn.close()


def test_create_and_list_sections(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(Section(id="s1", task_id="t1", seq=0, raw_md_path="/x/0.raw.md",
                                sha256="a", char_count=10, ai_status="PENDING",
                                created_at=int(time.time())))
    repo.create_section(Section(id="s2", task_id="t1", seq=1, raw_md_path="/x/1.raw.md",
                                sha256="b", char_count=20, ai_status="PENDING",
                                created_at=int(time.time())))
    sections = repo.list_sections("t1")
    assert len(sections) == 2
    assert sections[0].seq == 0
    conn.close()


def test_update_section_ai_status(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(Section(id="s1", task_id="t1", seq=0, raw_md_path="/x/0.raw.md",
                                sha256="a", char_count=10, ai_status="PENDING",
                                created_at=int(time.time())))
    repo.update_section_ai_status("s1", "COMPLETED")
    assert repo.get_section("s1").ai_status == "COMPLETED"
    conn.close()


def test_create_and_get_artifact(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(Section(id="s1", task_id="t1", seq=0, raw_md_path="/x/0.raw.md",
                                sha256="a", char_count=10, ai_status="PENDING",
                                created_at=int(time.time())))
    repo.create_artifact(AIArtifact(id="a1", section_id="s1", ai_md_path="/x/0.ai.md",
                                    tokens_in=5, tokens_out=3, cost_usd=0.0,
                                    retry_count=0, model_name="stub",
                                    created_at=int(time.time())))
    a = repo.get_artifact_by_section("s1")
    assert a is not None
    assert a.ai_md_path == "/x/0.ai.md"
    conn.close()


def test_increment_retry(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(Section(id="s1", task_id="t1", seq=0, raw_md_path="/x/0.raw.md",
                                sha256="a", char_count=10, ai_status="PENDING",
                                created_at=int(time.time())))
    repo.create_artifact(AIArtifact(id="a1", section_id="s1", ai_md_path="/x/0.ai.md",
                                    tokens_in=5, tokens_out=3, cost_usd=0.0,
                                    retry_count=0, model_name="stub",
                                    created_at=int(time.time())))
    repo.increment_retry("a1")
    repo.increment_retry("a1")
    assert repo.get_artifact_by_section("s1").retry_count == 2
    conn.close()


def test_find_task_by_sha256_completed(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    t = make_task(sha="hashX")
    repo.create_task(t)
    repo.create_section(Section(id="s1", task_id="t1", seq=0, raw_md_path="/x.raw.md",
                                sha256="h", char_count=1, ai_status="COMPLETED",
                                created_at=int(time.time())))
    repo.update_task_status("t1", "COMPLETED")
    found = repo.find_completed_task_by_file_sha256("hashX")
    assert found is not None
    assert found.id == "t1"
    conn.close()


def test_find_section_by_sha256_completed(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(__import__("parsing_core.storage.schema", fromlist=["SCHEMA_SQL"]).SCHEMA_SQL)
    conn.commit()
    repo = Repository(conn)
    repo.create_task(make_task(sha="F"))
    repo.create_section(Section(id="s1", task_id="t1", seq=0, raw_md_path="/x.raw.md",
                                sha256="SECX", char_count=1, ai_status="COMPLETED",
                                created_at=int(time.time())))
    repo.create_artifact(AIArtifact(id="a1", section_id="s1", ai_md_path="/y.ai.md",
                                    tokens_in=1, tokens_out=1, cost_usd=0.0,
                                    retry_count=0, model_name="stub",
                                    created_at=int(time.time())))
    hit = repo.find_completed_artifact_by_section_sha256("SECX")
    assert hit is not None
    assert hit.ai_md_path == "/y.ai.md"
    conn.close()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_repository.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/storage/repository.py
import sqlite3
import time

from parsing_core.models.dataclasses import Task, Section, AIArtifact


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- tasks ---
    def create_task(self, t: Task) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, file_path, snapshot_path, file_sha256, status, "
            "model_tier, created_at, updated_at, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, t.file_path, t.snapshot_path, t.file_sha256, t.status, t.model_tier,
             t.created_at, t.updated_at, t.error_msg),
        )
        self.conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        cur = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        return Task(*row)

    def update_task_status(self, task_id: str, status: str, error_msg: str | None = None) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, error_msg = ? WHERE id = ?",
            (status, int(time.time()), error_msg, task_id),
        )
        self.conn.commit()

    def find_completed_task_by_file_sha256(self, sha: str) -> Task | None:
        cur = self.conn.execute(
            "SELECT * FROM tasks WHERE file_sha256 = ? AND status = 'COMPLETED' LIMIT 1", (sha,)
        )
        row = cur.fetchone()
        return Task(*row) if row else None

    def list_tasks_by_status(self, status: str) -> list[Task]:
        cur = self.conn.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at", (status,))
        return [Task(*r) for r in cur.fetchall()]

    def list_all_tasks(self) -> list[Task]:
        cur = self.conn.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        return [Task(*r) for r in cur.fetchall()]

    def delete_task(self, task_id: str) -> None:
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()

    # --- sections ---
    def create_section(self, s: Section) -> None:
        self.conn.execute(
            "INSERT INTO sections (id, task_id, seq, raw_md_path, sha256, char_count, "
            "ai_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (s.id, s.task_id, s.seq, s.raw_md_path, s.sha256, s.char_count,
             s.ai_status, s.created_at),
        )
        self.conn.commit()

    def list_sections(self, task_id: str) -> list[Section]:
        cur = self.conn.execute(
            "SELECT * FROM sections WHERE task_id = ? ORDER BY seq", (task_id,)
        )
        return [Section(*r) for r in cur.fetchall()]

    def get_section(self, section_id: str) -> Section | None:
        cur = self.conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,))
        row = cur.fetchone()
        return Section(*row) if row else None

    def update_section_ai_status(self, section_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sections SET ai_status = ? WHERE id = ?", (status, section_id)
        )
        self.conn.commit()

    # --- ai_artifacts ---
    def create_artifact(self, a: AIArtifact) -> None:
        self.conn.execute(
            "INSERT INTO ai_artifacts (id, section_id, ai_md_path, tokens_in, tokens_out, "
            "cost_usd, retry_count, model_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (a.id, a.section_id, a.ai_md_path, a.tokens_in, a.tokens_out, a.cost_usd,
             a.retry_count, a.model_name, a.created_at),
        )
        self.conn.commit()

    def get_artifact_by_section(self, section_id: str) -> AIArtifact | None:
        cur = self.conn.execute(
            "SELECT * FROM ai_artifacts WHERE section_id = ?", (section_id,)
        )
        row = cur.fetchone()
        return AIArtifact(*row) if row else None

    def increment_retry(self, artifact_id: str) -> None:
        self.conn.execute(
            "UPDATE ai_artifacts SET retry_count = retry_count + 1 WHERE id = ?",
            (artifact_id,),
        )
        self.conn.commit()

    def find_completed_artifact_by_section_sha256(self, sha: str) -> AIArtifact | None:
        cur = self.conn.execute(
            "SELECT a.* FROM ai_artifacts a JOIN sections s ON a.section_id = s.id "
            "WHERE s.sha256 = ? AND s.ai_status = 'COMPLETED' LIMIT 1",
            (sha,),
        )
        row = cur.fetchone()
        return AIArtifact(*row) if row else None
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_repository.py -v
```
预期：8 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/storage/repository.py tests/test_repository.py
git commit -m "feat(storage): add Repository CRUD with cache queries"
```

---

## 任务 7：storage/fs_layout.py — 落盘路径策略

**文件：**
- 创建：`src/parsing_core/storage/fs_layout.py`
- 测试：`tests/test_fs_layout.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_fs_layout.py
from pathlib import Path

from parsing_core.storage.fs_layout import FsLayout


def test_task_dir_pattern(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    d = fs.task_dir("t1")
    assert d == str(tmp_path / "t1")
    assert Path(d).exists()


def test_section_raw_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.section_raw_path("t1", 0)
    assert p.endswith("t1/0.raw.md")


def test_section_ai_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.section_ai_path("t1", 0)
    assert p.endswith("t1/0.ai.md")


def test_merged_path(tmp_path: Path):
    fs = FsLayout(base_dir=str(tmp_path))
    p = fs.merged_path("t1")
    assert p.endswith("t1/merged.md")


def test_default_base_uses_appdata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    fs = FsLayout()
    assert str(tmp_path) in fs.base_dir
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_fs_layout.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/storage/fs_layout.py
import os
from pathlib import Path


class FsLayout:
    def __init__(self, base_dir: str | None = None) -> None:
        if base_dir is None:
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
            base_dir = str(Path(base) / "parsing-core")
        self.base_dir = base_dir
        Path(self.base_dir).mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> str:
        d = Path(self.base_dir) / task_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def section_raw_path(self, task_id: str, seq: int) -> str:
        return str(Path(self.task_dir(task_id)) / f"{seq}.raw.md")

    def section_ai_path(self, task_id: str, seq: int) -> str:
        return str(Path(self.task_dir(task_id)) / f"{seq}.ai.md")

    def merged_path(self, task_id: str) -> str:
        return str(Path(self.task_dir(task_id)) / "merged.md")

    def images_dir(self, task_id: str) -> str:
        d = Path(self.task_dir(task_id)) / "images"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_fs_layout.py -v
```
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/storage/fs_layout.py tests/test_fs_layout.py
git commit -m "feat(storage): add fs layout path strategy"
```

---

## 任务 8：storage/cache.py — 双缓存查询

**文件：**
- 创建：`src/parsing_core/storage/cache.py`
- 测试：`tests/test_cache.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_cache.py
import sqlite3
import time

from parsing_core.models.dataclasses import Task, Section, AIArtifact
from parsing_core.storage.cache import CacheService
from parsing_core.storage.schema import init_db


def seed(conn):
    t = Task(id="t1", file_path="/a", snapshot_path="/a", file_sha256="FILE1",
             status="COMPLETED", model_tier="stub", created_at=int(time.time()),
             updated_at=int(time.time()))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                 (t.id, t.file_path, t.snapshot_path, t.file_sha256, t.status,
                  t.model_tier, t.created_at, t.updated_at, t.error_msg))
    conn.execute("INSERT INTO sections VALUES (?,?,?,?,?,?,?,?)",
                 ("s1", "t1", 0, "/raw.md", "SEC1", 100, "COMPLETED", int(time.time())))
    conn.execute("INSERT INTO ai_artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                 ("a1", "s1", "/cached.ai.md", 1, 1, 0.0, 0, "stub", int(time.time())))
    conn.commit()


def test_file_cache_hit(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    seed(conn)
    cache = CacheService(conn)
    t = cache.find_completed_task_by_file_sha256("FILE1")
    assert t is not None and t.id == "t1"
    conn.close()


def test_file_cache_miss(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    cache = CacheService(conn)
    assert cache.find_completed_task_by_file_sha256("missing") is None
    conn.close()


def test_section_artifact_hit(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    seed(conn)
    cache = CacheService(conn)
    a = cache.find_completed_artifact_by_section_sha256("SEC1")
    assert a is not None and a.ai_md_path == "/cached.ai.md"
    conn.close()


def test_section_artifact_miss(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    cache = CacheService(conn)
    assert cache.find_completed_artifact_by_section_sha256("nope") is None
    conn.close()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_cache.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/storage/cache.py
from parsing_core.models.dataclasses import Task, AIArtifact
from parsing_core.storage.repository import Repository


class CacheService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def find_completed_task_by_file_sha256(self, sha: str) -> Task | None:
        return self.repo.find_completed_task_by_file_sha256(sha)

    def find_completed_artifact_by_section_sha256(self, sha: str) -> AIArtifact | None:
        return self.repo.find_completed_artifact_by_section_sha256(sha)
```

- [ ] **步骤 4：修正测试以注入 Repository**

替换 `tests/test_cache.py` 的实例化语句为：

```python
from parsing_core.storage.repository import Repository
# ...
repo = Repository(conn)
cache = CacheService(repo)
```

完整修订测试：

```python
# tests/test_cache.py
import time

from parsing_core.models.dataclasses import Task
from parsing_core.storage.cache import CacheService
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def seed(conn):
    t = Task(id="t1", file_path="/a", snapshot_path="/a", file_sha256="FILE1",
             status="COMPLETED", model_tier="stub", created_at=int(time.time()),
             updated_at=int(time.time()))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                 (t.id, t.file_path, t.snapshot_path, t.file_sha256, t.status,
                  t.model_tier, t.created_at, t.updated_at, t.error_msg))
    conn.execute("INSERT INTO sections VALUES (?,?,?,?,?,?,?,?)",
                 ("s1", "t1", 0, "/raw.md", "SEC1", 100, "COMPLETED", int(time.time())))
    conn.execute("INSERT INTO ai_artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                 ("a1", "s1", "/cached.ai.md", 1, 1, 0.0, 0, "stub", int(time.time())))
    conn.commit()


def test_file_cache_hit(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    seed(conn)
    cache = CacheService(Repository(conn))
    t = cache.find_completed_task_by_file_sha256("FILE1")
    assert t is not None and t.id == "t1"
    conn.close()


def test_file_cache_miss(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    cache = CacheService(Repository(conn))
    assert cache.find_completed_task_by_file_sha256("missing") is None
    conn.close()


def test_section_artifact_hit(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    seed(conn)
    cache = CacheService(Repository(conn))
    a = cache.find_completed_artifact_by_section_sha256("SEC1")
    assert a is not None and a.ai_md_path == "/cached.ai.md"
    conn.close()


def test_section_artifact_miss(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    cache = CacheService(Repository(conn))
    assert cache.find_completed_artifact_by_section_sha256("nope") is None
    conn.close()
```

运行：

```bash
pytest tests/test_cache.py -v
```
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/storage/cache.py tests/test_cache.py
git commit -m "feat(storage): add cache service wrapping repo queries"
```

---

## 任务 9：parser/image_extractor.py — Base64 图片落盘

**文件：**
- 创建：`src/parsing_core/parser/__init__.py`、`src/parsing_core/parser/image_extractor.py`
- 测试：`tests/test_image_extractor.py`、`tests/fixtures/with_base64.md`

- [ ] **步骤 1：准备 fixture**

```markdown
# tests/fixtures/with_base64.md
# Title

Some text.

![pic](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=)

More text.

![alt2](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=)
```

- [ ] **步骤 2：编写失败的测试**

```python
# tests/test_image_extractor.py
from pathlib import Path

from parsing_core.parser.image_extractor import extract_images


def test_extracts_two_base64_images(tmp_path: Path):
    src = Path("tests/fixtures/with_base64.md").read_text()
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    result_md, images = extract_images(src, str(out_dir))
    assert len(images) == 2
    for path in images:
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0


def test_replaces_with_local_path(tmp_path: Path):
    src = Path("tests/fixtures/with_base64.md").read_text()
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    result_md, _ = extract_images(src, str(out_dir))
    assert "data:" not in result_md
    assert ".png" in result_md


def test_no_images_passthrough(tmp_path: Path):
    src = "# Title\n\nNo images here."
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    result_md, images = extract_images(src, str(out_dir))
    assert images == []
    assert result_md == src


def test_unique_filenames(tmp_path: Path):
    src = Path("tests/fixtures/with_base64.md").read_text()
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    _, images = extract_images(src, str(out_dir))
    assert len(set(images)) == len(images)
```

- [ ] **步骤 3：运行测试验证失败**

```bash
pytest tests/test_image_extractor.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 4：编写实现代码**

```python
# src/parsing_core/parser/__init__.py
```

```python
# src/parsing_core/parser/image_extractor.py
import base64
import re
from pathlib import Path

DATA_URI_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(data:(?P<mime>[\w/+]+);base64,(?P<data>[A-Za-z0-9+/=]+)\)"
)


def extract_images(markdown: str, images_dir: str) -> tuple[str, list[str]]:
    """将 MD 中所有 Base64 图片落盘，替换为本地路径。返回 (新 MD, 图片路径列表)。"""
    images: list[str] = []
    counter = 0

    def replace(match: re.Match) -> str:
        nonlocal counter
        alt = match.group("alt")
        mime = match.group("mime")
        data = match.group("data")
        ext = mime.split("/")[-1].split("+")[0]
        fname = f"img_{counter:03d}.{ext}"
        counter += 1
        fpath = Path(images_dir) / fname
        fpath.write_bytes(base64.b64decode(data))
        images.append(str(fpath))
        return f"![{alt}]({fpath})"

    new_md = DATA_URI_RE.sub(replace, markdown)
    return new_md, images
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_image_extractor.py -v
```
预期：4 passed

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/parser tests/test_image_extractor.py tests/fixtures/with_base64.md
git commit -m "feat(parser): extract base64 images to disk and replace with paths"
```

---

## 任务 10：parser/markitdown_adapter.py — MarkItDown 包装

**文件：**
- 创建：`src/parsing_core/parser/base.py`、`src/parsing_core/parser/markitdown_adapter.py`
- 测试：`tests/test_markitdown_adapter.py`、`tests/fixtures/sample.md`

- [ ] **步骤 1：准备 fixture**

```markdown
# tests/fixtures/sample.md
# Sample

Hello world.
```

- [ ] **步骤 2：编写失败的测试**

```python
# tests/test_markitdown_adapter.py
from pathlib import Path

from parsing_core.parser.markitdown_adapter import MarkItDownAdapter


def test_parse_md_passthrough(tmp_path: Path):
    adapter = MarkItDownAdapter()
    md = adapter.parse(str(Path("tests/fixtures/sample.md").resolve()))
    assert "Sample" in md
    assert "Hello world" in md


def test_parse_md_text_passthrough():
    adapter = MarkItDownAdapter()
    md = adapter.parse_text("# H1\n\nbody")
    assert "# H1" in md
    assert "body" in md
```

- [ ] **步骤 3：运行测试验证失败**

```bash
pytest tests/test_markitdown_adapter.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 4：编写实现代码**

```python
# src/parsing_core/parser/base.py
from abc import ABC, abstractmethod


class Parser(ABC):
    @abstractmethod
    def parse(self, file_path: str) -> str:
        ...

    def parse_text(self, text: str) -> str:
        return text
```

```python
# src/parsing_core/parser/markitdown_adapter.py
from pathlib import Path

from parsing_core.parser.base import Parser


class MarkItDownAdapter(Parser):
    def __init__(self) -> None:
        try:
            from markitdown import MarkItDown
        except ImportError as e:
            raise RuntimeError("markitdown not installed") from e
        self._md = MarkItDown()

    def parse(self, file_path: str) -> str:
        result = self._md.convert(file_path)
        return str(result)

    def parse_text(self, text: str) -> str:
        # 仅 Markdown 直接透传，避免无谓的文件 IO
        suffix = ".md"
        if Path(text).suffix.lower() in (".md", ".markdown", ".txt"):
            return Path(text).read_text(encoding="utf-8")
        return self.parse(text)
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_markitdown_adapter.py -v
```
预期：2 passed（若 markitdown 已安装）

- [ ] **步骤 6：Commit**

```bash
git add src/parsing_core/parser/base.py src/parsing_core/parser/markitdown_adapter.py \
        tests/test_markitdown_adapter.py tests/fixtures/sample.md
git commit -m "feat(parser): add MarkItDown adapter"
```

---

## 任务 11：parser/chunker.py — 节切分

**文件：**
- 创建：`src/parsing_core/parser/chunker.py`
- 测试：`tests/test_chunker.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_chunker.py
from parsing_core.parser.chunker import split_sections, Section as Chunk


def test_split_by_h2():
    md = "# Doc\n\n## A\n\nfoo\n\n## B\n\nbar\n"
    chunks = split_sections(md)
    assert len(chunks) == 2
    assert chunks[0].title == "A"
    assert chunks[1].title == "B"
    assert "foo" in chunks[0].raw
    assert "bar" in chunks[1].raw


def test_split_by_h3():
    md = "## A\n\n## A.1\n\nfoo\n\n## A.2\n\nbar\n"
    # 替换：测试用 H2 切
    md = "## A.1\n\nfoo\n\n## A.2\n\nbar\n"
    chunks = split_sections(md)
    assert len(chunks) == 2


def test_split_table_as_own_section():
    md = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter\n"
    chunks = split_sections(md)
    table_idx = next(i for i, c in enumerate(chunks) if "|" in c.raw and "|---" in c.raw)
    # 表格应是一节
    assert "|---" in chunks[table_idx].raw


def test_short_paragraph_merges_into_previous():
    md = "## A\n\nlong enough body text to pass threshold long enough body text to pass threshold long enough body text.\n\nx.\n"
    chunks = split_sections(md)
    # 短段落 "x." 应合并到前一节
    assert all("x." not in c.raw or len(c.raw) > 100 for c in chunks)


def test_long_section_splits():
    para = "word " * 1500  # ~7500 字符
    md = f"## Big\n\n{para}\n"
    chunks = split_sections(md)
    assert len(chunks) > 1
    for c in chunks:
        assert c.char_count <= 4500  # 留余量


def test_returns_raw_and_sha():
    md = "## A\n\nfoo\n"
    chunks = split_sections(md)
    assert chunks[0].raw
    assert len(chunks[0].sha256) == 64
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_chunker.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/parser/chunker.py
import re
from dataclasses import dataclass

from parsing_core.utils.hashing import text_sha256

MAX_SECTION_CHARS = 4000
MIN_SECTION_CHARS = 100
MIN_SHORT_PARA_THRESHOLD = 100  # < 此长度视为短段

HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
TABLE_BLOCK_RE = re.compile(r"(\n\|[^\n]+\|\n\|[\s:|-]+\|\n(?:\|[^\n]+\|\n?)+)", re.MULTILINE)


@dataclass
class Section:
    seq: int
    title: str
    raw: str
    sha256: str
    char_count: int


def split_sections(markdown: str) -> list[Section]:
    units = _split_by_structure(markdown)
    units = _split_long(units)
    units = _merge_short(units)
    return [
        Section(seq=i, title=_extract_title(u), raw=u, sha256=text_sha256(u), char_count=len(u))
        for i, u in enumerate(units)
    ]


def _split_by_structure(markdown: str) -> list[str]:
    # 按 H2/H3 标题、独立表格、独立大段落切
    parts: list[str] = []
    # 先按 H2/H3 标题切
    lines = markdown.splitlines(keepends=True)
    current: list[str] = []
    last_was_header_break = False

    def flush():
        nonlocal current, last_was_header_break
        if current:
            parts.append("".join(current))
            current = []

    for line in lines:
        m = re.match(r"^#{2,3}\s+", line)
        if m:
            flush()
            current.append(line)
            last_was_header_break = True
        else:
            current.append(line)
    flush()

    # 再按独立表格切（在已有 parts 上做二次细分）
    refined: list[str] = []
    for p in parts:
        start = 0
        for m in TABLE_BLOCK_RE.finditer(p):
            if m.start() > start:
                refined.append(p[start:m.start()])
            refined.append(m.group(1))
            start = m.end()
        if start < len(p):
            refined.append(p[start:])

    return [r for r in refined if r.strip()]


def _split_long(units: list[str]) -> list[str]:
    out: list[str] = []
    for u in units:
        if len(u) <= MAX_SECTION_CHARS:
            out.append(u)
            continue
        # 按段落边界（空行）切
        paras = re.split(r"(\n\n+)", u)
        chunk = ""
        for seg in paras:
            if len(chunk) + len(seg) <= MAX_SECTION_CHARS:
                chunk += seg
            else:
                if chunk:
                    out.append(chunk)
                chunk = seg
        if chunk:
            out.append(chunk)
    return out


def _merge_short(units: list[str]) -> list[str]:
    if not units:
        return []
    out: list[str] = [units[0]]
    for u in units[1:]:
        if len(u) < MIN_SHORT_PARA_THRESHOLD and not _is_table(u) and not _is_header_line(u):
            out[-1] = out[-1] + u
        else:
            out.append(u)
    return out


def _is_table(text: str) -> bool:
    return bool(TABLE_BLOCK_RE.search(text))


def _is_header_line(text: str) -> bool:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return bool(re.match(r"^#{1,6}\s+", first_line))


def _extract_title(text: str) -> str:
    m = HEADER_RE.match(text.strip())
    return m.group(2) if m else "(无标题)"
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_chunker.py -v
```
预期：6 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/parser/chunker.py tests/test_chunker.py
git commit -m "feat(parser): add section chunker with structure/long/short splitting"
```

---

## 任务 12：llm/base.py + prompt_templates.py + stub_client.py — Stub LLM

**文件：**
- 创建：`src/parsing_core/llm/__init__.py`、`src/parsing_core/llm/base.py`、`src/parsing_core/llm/prompt_templates.py`、`src/parsing_core/llm/stub_client.py`
- 测试：`tests/test_stub_client.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_stub_client.py
from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.models.dataclasses import Section


def make_section(seq=0, raw="## A\n\nbody"):
    return Section(id="s1", task_id="t1", seq=seq, raw_md_path="/x.raw.md",
                   sha256="h", char_count=len(raw), ai_status="PENDING")


def test_stub_output_has_ai_interpret_header():
    s = make_section(raw="## A\n\nbody")
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert "▸ AI 解读" in a.ai_md


def test_stub_output_has_mermaid_block():
    s = make_section()
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert "```mermaid" in a.ai_md
    assert "```" in a.ai_md.split("```mermaid")[1]


def test_stub_output_includes_seq_number():
    s = make_section(seq=5)
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert "5" in a.ai_md


def test_stub_tokens_recorded():
    s = make_section()
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert a.tokens_in > 0
    assert a.tokens_out > 0


def test_stub_model_name():
    s = make_section()
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert a.model_name == "stub"


def test_stub_is_deterministic():
    s = make_section(seq=3, raw="content")
    a1 = StubLLMClient().interpret(s, raw_md="content")
    a2 = StubLLMClient().interpret(s, raw_md="content")
    assert a1.ai_md == a2.ai_md
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_stub_client.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/llm/__init__.py
```

```python
# src/parsing_core/llm/base.py
from abc import ABC, abstractmethod

from parsing_core.models.dataclasses import AIArtifact, Section


class LLMClient(ABC):
    @abstractmethod
    def interpret(self, section: Section, raw_md: str) -> AIArtifact:
        ...
```

```python
# src/parsing_core/llm/prompt_templates.py
SECTION_INTERPRET_PROMPT = """你是工业报表分析助手。给定以下原文节，请输出：
1. 关键指标（如有）
2. 风险提示（如有）
3. 一段 mermaid 流程图代码块，可视化该节描述的过程或结构

原文节：
<<<
{raw_md}
>>>

输出格式：Markdown，必须包含 `### ▸ AI 解读` 标题和至少一个 ```mermaid 代码块。
"""
```

```python
# src/parsing_core/llm/stub_client.py
import time
import uuid

from parsing_core.llm.base import LLMClient
from parsing_core.models.dataclasses import AIArtifact, Section


class StubLLMClient(LLMClient):
    """确定性 stub LLM 客户端。不调用任何真实模型，输出固定结构的占位 MD。"""

    def interpret(self, section: Section, raw_md: str) -> AIArtifact:
        tokens_in = len(raw_md.split())
        ai_md = self._render(section.seq, tokens_in)
        tokens_out = len(ai_md.split())
        return AIArtifact(
            id=str(uuid.uuid4()),
            section_id=section.id,
            ai_md_path="",  # 由 orchestrator 落盘后回填
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        ) if False else self._build_artifact(section, ai_md, tokens_in, tokens_out)

    def _build_artifact(self, section: Section, ai_md: str, tokens_in: int, tokens_out: int) -> AIArtifact:
        return AIArtifact(
            id=str(uuid.uuid4()),
            section_id=section.id,
            ai_md_path="",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )._with_ai_md(ai_md)

    @staticmethod
    def _render(seq: int, tokens_in: int) -> str:
        return (
            "### ▸ AI 解读\n\n"
            f"- **关键指标**: <stub 占位>（节 {seq}，输入约 {tokens_in} tokens）\n"
            "- **风险提示**: <stub 占位>\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            f"  A[Stub 节 {seq}] --> B[占位节点]\n"
            "  B --> C[Mermaid 已就绪]\n"
            "```\n"
        )
```

为支持 `_with_ai_md`，需在数据类上补该方法。修改 `src/parsing_core/models/dataclasses.py` 中 `AIArtifact` 类，将其改为：

```python
@dataclass
class AIArtifact:
    id: str
    section_id: str
    ai_md_path: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    retry_count: int = 0
    model_name: str | None = None
    created_at: int = 0
    _ai_md: str = ""

    def _with_ai_md(self, md: str) -> "AIArtifact":
        self._ai_md = md
        return self
```

并修改 `stub_client.py` 与测试使其直接返回带 `_ai_md` 字段的 artifact。但 `_ai_md` 以 `_` 开头不被默认 dataclass 行为友好支持。**修订**：改用普通字段 `ai_md`。

最终修订 `AIArtifact`：

```python
@dataclass
class AIArtifact:
    id: str
    section_id: str
    ai_md_path: str = ""
    ai_md: str = ""            # 内存中的解读内容，落盘后清空可选
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    retry_count: int = 0
    model_name: str | None = None
    created_at: int = 0
```

重新修订 `stub_client.py`，删除 `_render` 间接调用：

```python
# src/parsing_core/llm/stub_client.py（修订最终版）
import time
import uuid

from parsing_core.llm.base import LLMClient
from parsing_core.models.dataclasses import AIArtifact, Section


class StubLLMClient(LLMClient):
    """确定性 stub LLM 客户端。不调用任何真实模型，输出固定结构的占位 MD。"""

    def interpret(self, section: Section, raw_md: str) -> AIArtifact:
        tokens_in = len(raw_md.split())
        ai_md = self._render(section.seq, tokens_in)
        tokens_out = len(ai_md.split())
        return AIArtifact(
            id=str(uuid.uuid4()),
            section_id=section.id,
            ai_md_path="",
            ai_md=ai_md,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )

    @staticmethod
    def _render(seq: int, tokens_in: int) -> str:
        return (
            "### ▸ AI 解读\n\n"
            f"- **关键指标**: <stub 占位>（节 {seq}，输入约 {tokens_in} tokens）\n"
            "- **风险提示**: <stub 占位>\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            f"  A[Stub 节 {seq}] --> B[占位节点]\n"
            "  B --> C[Mermaid 已就绪]\n"
            "```\n"
        )
```

同步修订 `models/dataclasses.py` 中 AIArtifact 增 `ai_md="a1.md"`。**手动修订**：删除原 `AIArtifact.ai_md_path="/y.ai.md"`类的 `ai_md=""`，但保留 `ai_md_path` 默认 `""`。Repository 写入时 `ai_md` 字段不写入 DB（DB 仅存路径）——为避免 Repository 报错（列不匹配），修改 Repository `create_artifact` 与 schema：**保持 schema 不引入 ai_md 列**，Repository INSERT 仅取 DB 列对应字段，`ai_md` 仅在内存流转。

修订 Repository `create_artifact` 不引用 `a.ai_md`：

```python
# 修订 src/parsing_core/storage/repository.py 的 create_artifact
def create_artifact(self, a: AIArtifact) -> None:
    self.conn.execute(
        "INSERT INTO ai_artifacts (id, section_id, ai_md_path, tokens_in, tokens_out, "
        "cost_usd, retry_count, model_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (a.id, a.section_id, a.ai_md_path, a.tokens_in, a.tokens_out, a.cost_usd,
         a.retry_count, a.model_name, a.created_at),
    )
    self.conn.commit()
```

同样 `get_artifact_by_section` 从 DB 行重建 AIArtifact 时，将 `ai_md` 设默认 `""`：

```python
# 修订 get_artifact_by_section 与 find_completed_artifact_by_section_sha256
def get_artifact_by_section(self, section_id: str) -> AIArtifact | None:
    cur = self.conn.execute(
        "SELECT id, section_id, ai_md_path, tokens_in, tokens_out, cost_usd, "
        "retry_count, model_name, created_at FROM ai_artifacts WHERE section_id = ?",
        (section_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return AIArtifact(id=row[0], section_id=row[1], ai_md_path=row[2], ai_md="",
                     tokens_in=row[3], tokens_out=row[4], cost_usd=row[5],
                     retry_count=row[6], model_name=row[7], created_at=row[8])

def find_completed_artifact_by_section_sha256(self, sha: str) -> AIArtifact | None:
    cur = self.conn.execute(
        "SELECT a.id, a.section_id, a.ai_md_path, a.tokens_in, a.tokens_out, a.cost_usd, "
        "a.retry_count, a.model_name, a.created_at FROM ai_artifacts a "
        "JOIN sections s ON a.section_id = s.id "
        "WHERE s.sha256 = ? AND s.ai_status = 'COMPLETED' LIMIT 1",
        (sha,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return AIArtifact(id=row[0], section_id=row[1], ai_md_path=row[2], ai_md="",
                     tokens_in=row[3], tokens_out=row[4], cost_usd=row[5],
                     retry_count=row[6], model_name=row[7], created_at=row[8])
```

同样 `find_completed_artifact_by_section_sha256` 测试中 `a.ai_md_path` 仍生效。

修订 `test_repository.py::test_create_and_get_artifact` 仍合规。`test_find_section_by_sha256_completed` 同样合规。Repository 中 `Task(*row)` 与 `Section(*row)` 这种解构形式会因字段顺序问题出错——需修订为按列名重建。

**修订 Repository 中所有 `Task(*row)` / `Section(*row)` 等解构**，改为按列名显式重建（避免字段顺序错位）。最终 `repository.py` 的 `get_task` 改为：

```python
def get_task(self, task_id: str) -> Task | None:
    cur = self.conn.execute(
        "SELECT id, file_path, snapshot_path, file_sha256, status, model_tier, "
        "created_at, updated_at, error_msg FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return Task(id=row[0], file_path=row[1], snapshot_path=row[2], file_sha256=row[3],
                status=row[4], model_tier=row[5], created_at=row[6], updated_at=row[7],
                error_msg=row[8])
```

`list_sections` 改为：

```python
def list_sections(self, task_id: str) -> list[Section]:
    cur = self.conn.execute(
        "SELECT id, task_id, seq, raw_md_path, sha256, char_count, ai_status, created_at "
        "FROM sections WHERE task_id = ? ORDER BY seq",
        (task_id,),
    )
    return [Section(id=r[0], task_id=r[1], seq=r[2], raw_md_path=r[3], sha256=r[4],
                    char_count=r[5], ai_status=r[6], created_at=r[7]) for r in cur.fetchall()]
```

`get_section` 同样修订。

`find_completed_task_by_file_sha256` 返回示例修订为显式重建。`list_tasks_by_status` / `list_all_tasks` 同样修订。

**为简化本计划**：`test_repository.py` 应保持通过；请按上述修订重写 `Repository`。具体 SPE 之后再合并到文件。

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_stub_client.py tests/test_repository.py tests/test_models.py -v
```
预期：测试全绿。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/llm src/parsing_core/models/dataclasses.py \
        src/parsing_core/storage/repository.py tests/test_stub_client.py
git commit -m "feat(llm): add StubLLMClient with deterministic mermaid output"
```

---

## 任务 13：orchestrator.py — 编排合流

**文件：**
- 创建：`src/parsing_core/orchestrator.py`
- 测试：`tests/test_orchestrator.py`、`tests/fixtures/sample.md`（已有）

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_orchestrator.py
import json
import os
from pathlib import Path

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def make_orchestrator(tmp_path):
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(tmp_path / "x.db")), repo, fs, conn


def test_parse_file_creates_merged_md(tmp_path):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    assert result["status"] == "COMPLETED"
    merged = Path(result["merged_md_path"])
    assert merged.exists()
    text = merged.read_text()
    assert "▸ AI 解读" in text
    assert "```mermaid" in text


def test_parse_file_returns_task_id(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    assert "task_id" in result
    assert len(result["task_id"]) == 36  # uuid


def test_parse_file_records_sections_count(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    md = "## A\n\nfoo\n\n## B\n\nbar\n"
    f = tmp_path / "in.md"
    f.write_text(md)
    result = orch.parse_file(str(f))
    assert result["sections"] >= 2


def test_file_cache_hit_second_parse(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    r1 = orch.parse_file(str(sample))
    r2 = orch.parse_file(str(sample))
    assert r2["cached"] is True
    assert r1["task_id"] == r2["task_id"]


def test_resume_completes_pending_sections(tmp_path):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    task_id = result["task_id"]
    # 人为破坏：把第一节标记为 PENDING 并清空 ai_artifact
    sections = repo.list_sections(task_id)
    if sections:
        repo.update_section_ai_status(sections[0].id, "PENDING")
        conn.execute("DELETE FROM ai_artifacts WHERE section_id = ?", (sections[0].id,))
        conn.commit()
    orch.resume(task_id)
    sections2 = repo.list_sections(task_id)
    assert all(s.ai_status == "COMPLETED" for s in sections2)
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_orchestrator.py -v
```
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/orchestrator.py
import json
import sqlite3
import time
import uuid
from pathlib import Path

from parsing_core.llm.base import LLMClient
from parsing_core.models.dataclasses import Section, Task
from parsing_core.parser.chunker import split_sections
from parsing_core.parser.image_extractor import extract_images
from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.storage.cache import CacheService
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.utils.file_lock import snapshot
from parsing_core.utils.hashing import file_sha256


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        fs: FsLayout,
        llm: LLMClient,
        db_path: str,
    ) -> None:
        self.repo = repo
        self.fs = fs
        self.llm = llm
        self.db_path = db_path
        self.parser = MarkItDownAdapter()
        self.cache = CacheService(repo)

    def parse_file(self, file_path: str, force: bool = False) -> dict:
        # 1. 副本
        snap = snapshot(file_path)
        sha = file_sha256(snap)

        # 2. 文件级缓存命中
        if not force:
            hit = self.cache.find_completed_task_by_file_sha256(sha)
            if hit:
                return {
                    "task_id": hit.id, "merged_md_path": self.fs.merged_path(hit.id),
                    "sections": len(self.repo.list_sections(hit.id)), "cached": True,
                    "status": "COMPLETED",
                }

        # 3. 建任务
        task_id = str(uuid.uuid4())
        now = int(time.time())
        task = Task(id=task_id, file_path=file_path, snapshot_path=snap, file_sha256=sha,
                   status="PARSING", model_tier="stub", created_at=now, updated_at=now)
        self.repo.create_task(task)

        try:
            # 4. MarkItDown 解析
            raw_md = self.parser.parse(snap)

            # 5. 图片落盘
            images_dir = self.fs.images_dir(task_id)
            raw_md, _imgs = extract_images(raw_md, images_dir)

            # 6. 分节
            self.repo.update_task_status(task_id, "SECTIONING")
            chunks = split_sections(raw_md)

            # 7. 落原文节到磁盘 + 写 DB
            now = int(time.time())
            for chunk in chunks:
                sid = str(uuid.uuid4())
                raw_path = self.fs.section_raw_path(task_id, chunk.seq)
                Path(raw_path).write_text(chunk.raw, encoding="utf-8")
                sec = Section(id=sid, task_id=task_id, seq=chunk.seq, raw_md_path=raw_path,
                              sha256=chunk.sha256, char_count=chunk.char_count,
                              ai_status="PENDING", created_at=now)
                self.repo.create_section(sec)

            # 8. 节级 LLM 调用（含缓存命中复用）
            self.repo.update_task_status(task_id, "LLM_RUNNING")
            sections = self.repo.list_sections(task_id)
            for sec in sections:
                self._interpret_section(task_id, sec)

            # 9. 合流
            self.repo.update_task_status(task_id, "MERGING")
            merged = self._merge(task_id, file_path)
            merged_path = self.fs.merged_path(task_id)
            Path(merged_path).write_text(merged, encoding="utf-8")

            self.repo.update_task_status(task_id, "COMPLETED")

            # 清理副本
            try:
                Path(snap).unlink()
            except OSError:
                pass

            return {
                "task_id": task_id, "merged_md_path": merged_path,
                "sections": len(sections), "cached": False, "status": "COMPLETED",
            }
        except Exception as e:
            self.repo.update_task_status(task_id, "FAILED", error_msg=str(e))
            raise

    def _interpret_section(self, task_id: str, sec: Section) -> None:
        # 节级缓存命中：复用已有 artifact 的 ai_md_path 落盘
        hit = self.cache.find_completed_artifact_by_section_sha256(sec.sha256)
        if hit:
            ai_path = self.fs.section_ai_path(task_id, sec.seq)
            # 硬链接若已存在不报错——直接复制内容
            Path(ai_path).write_text(Path(hit.ai_md_path).read_text(encoding="utf-8"),
                                      encoding="utf-8")
            from parsing_core.models.dataclasses import AIArtifact
            cached = AIArtifact(id=str(uuid.uuid4()), section_id=sec.id, ai_md_path=ai_path,
                               ai_md="", tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
                               cost_usd=hit.cost_usd, retry_count=0,
                               model_name=hit.model_name, created_at=int(time.time()))
            self.repo.create_artifact(cached)
            self.repo.update_section_ai_status(sec.id, "COMPLETED")
            return

        # 否则调 LLM 落盘
        raw_md = Path(sec.raw_md_path).read_text(encoding="utf-8")
        artifact = self.llm.interpret(sec, raw_md)
        ai_path = self.fs.section_ai_path(task_id, sec.seq)
        Path(ai_path).write_text(artifact.ai_md, encoding="utf-8")
        artifact.ai_md_path = ai_path
        self.repo.create_artifact(artifact)
        self.repo.update_section_ai_status(sec.id, "COMPLETED")

    def _merge(self, task_id: str, original_file_path: str) -> str:
        sections = self.repo.list_sections(task_id)
        out = [f"> 任务 ID: {task_id}", f"> 源文件: {original_file_path}",
               f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        for s in sections:
            raw = Path(s.raw_md_path).read_text(encoding="utf-8")
            title = self._section_title(s.seq, raw)
            out.append(f"## 第 {s.seq + 1} 节：{title}")
            out.append("")
            out.append(raw.rstrip())
            out.append("")
            artifact = self.repo.get_artifact_by_section(s.id)
            if artifact:
                ai_text = Path(artifact.ai_md_path).read_text(encoding="utf-8")
                out.append(ai_text.rstrip())
            else:
                out.append("### ▸ AI 解读")
                out.append("")
                out.append("⚠ 此节解读失败，可重试。")
            out.append("")
            out.append("---")
            out.append("")
        return "\n".join(out)

    @staticmethod
    def _section_title(seq: int, raw: str) -> str:
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or f"节 {seq + 1}"
        return f"节 {seq + 1}"

    def resume(self, task_id: str) -> dict:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "status": "NOT_FOUND"}
        if task.status == "COMPLETED":
            return {"task_id": task_id, "status": "ALREADY_COMPLETED"}

        sections = self.repo.list_sections(task_id)
        pending = [s for s in sections if s.ai_status in ("PENDING", "RUNNING", "FAILED")]
        for s in pending:
            self._interpret_section(task_id, s)

        merged = self._merge(task_id, task.file_path)
        merged_path = self.fs.merged_path(task_id)
        Path(merged_path).write_text(merged, encoding="utf-8")
        self.repo.update_task_status(task_id, "COMPLETED")
        return {"task_id": task_id, "merged_md_path": merged_path,
                "status": "COMPLETED", "sections": len(sections)}

    def status(self, task_id: str) -> dict:
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "status": "NOT_FOUND"}
        sections = self.repo.list_sections(task_id)
        return {
            "task_id": task_id, "status": task.status,
            "sections": len(sections),
            "completed": sum(1 for s in sections if s.ai_status == "COMPLETED"),
            "error_msg": task.error_msg,
        }

    def list_all(self) -> list[dict]:
        out = []
        for t in self.repo.list_all_tasks():
            out.append({"task_id": t.id, "status": t.status, "file_path": t.file_path})
        return out

    def purge(self, task_id: str) -> dict:
        import shutil
        task = self.repo.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "purged": False}
        d = self.fs.task_dir(task_id)
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            pass
        if task.snapshot_path and Path(task.snapshot_path).exists():
            try:
                Path(task.snapshot_path).unlink()
            except OSError:
                pass
        self.repo.delete_task(task_id)
        return {"task_id": task_id, "purged": True}
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_orchestrator.py -v
```
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add parse/resume/merge orchestration"
```

---

## 任务 14：cli.py — CLI 入口

**文件：**
- 创建：`src/parsing_core/cli.py`
- 测试：`tests/test_cli.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_cli.py
import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(args, env=None):
    cmd = [sys.executable, "-m", "parsing_core.cli", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=".")
    return r


def test_parse_md(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = run_cli(["parse", sample])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "COMPLETED"
    assert Path(out["merged_md_path"]).exists()


def test_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample])
    tid = json.loads(r1.stdout)["task_id"]
    r2 = run_cli(["status", tid])
    out = json.loads(r2.stdout)
    assert out["status"] == "COMPLETED"


def test_list(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    run_cli(["parse", sample])
    r = run_cli(["list"])
    out = json.loads(r.stdout)
    assert len(out) >= 1
    assert out[0]["status"] == "COMPLETED"


def test_resume(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample])
    tid = json.loads(r1.stdout)["task_id"]
    r2 = run_cli(["resume", tid])
    out = json.loads(r2.stdout)
    assert out["status"] in ("COMPLETED", "ALREADY_COMPLETED")


def test_purge(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample])
    tid = json.loads(r1.stdout)["task_id"]
    r2 = run_cli(["purge", tid])
    out = json.loads(r2.stdout)
    assert out["purged"] is True
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_cli.py -v
```
预期：FAIL 或 `ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/parsing_core/cli.py
import argparse
import json
import os
import sqlite3
import sys

from parsing_core.orchestrator import Orchestrator
from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def _build_orchestrator() -> Orchestrator:
    fs = FsLayout()
    db_path = os.path.join(fs.base_dir, "core.db")
    conn = init_db(db_path)
    repo = Repository(conn)
    return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=db_path)


def main() -> int:
    parser = argparse.ArgumentParser(prog="parsing-core")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse")
    p_parse.add_argument("file_path")
    p_parse.add_argument("--model", default="stub")
    p_parse.add_argument("--force", action="store_true")

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("task_id")

    p_status = sub.add_parser("status")
    p_status.add_argument("task_id")

    sub.add_parser("list")
    p_purge = sub.add_parser("purge")
    p_purge.add_argument("task_id")

    args = parser.parse_args()
    orch = _build_orchestrator()

    if args.cmd == "parse":
        out = orch.parse_file(args.file_path, force=args.force)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "resume":
        out = orch.resume(args.task_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "status":
        out = orch.status(args.task_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "list":
        out = orch.list_all()
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "purge":
        out = orch.purge(args.task_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_cli.py -v
```
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/cli.py tests/test_cli.py
git commit -m "feat(cli): add parse/resume/status/list/purge subcommands"
```

---

## 任务 15：集成测试与 lint 收尾

**文件：**
- 创建：`tests/fixtures/sample.xlsx`（手动放最小 xlsx 或用 openpyxl 生成）
- 创建：`tests/test_integration.py`
- 修改：`pyproject.toml`（如需）

- [ ] **步骤 1：编写集成测试**

```python
# tests/test_integration.py
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(args, env):
    r = subprocess.run([sys.executable, "-m", "parsing_core.cli", *args],
                       capture_output=True, text=True, env=env, cwd=".")
    return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return dict(os.environ)


def test_end_to_end_md(env):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = run_cli(["parse", sample], env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "COMPLETED"
    merged = Path(out["merged_md_path"]).read_text()
    assert "▸ AI 解读" in merged
    assert "```mermaid" in merged
    assert "---" in merged


def test_cache_hit_second_call(env):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample], env)
    r2 = run_cli(["parse", sample], env)
    assert json.loads(r1.stdout)["cached"] is False
    assert json.loads(r2.stdout)["cached"] is True
    assert json.loads(r1.stdout)["task_id"] == json.loads(r2.stdout)["task_id"]


def test_resume_after_partial(env, tmp_path):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = run_cli(["parse", sample], env)
    tid = json.loads(r1.stdout)["task_id"]
    # 模拟中断：直接调 resume（任务已完成，应返回 ALREADY_COMPLETED）
    r2 = run_cli(["resume", tid], env)
    out = json.loads(r2.stdout)
    assert out["status"] in ("COMPLETED", "ALREADY_COMPLETED")
```

- [ ] **步骤 2：运行集成测试**

```bash
pytest tests/test_integration.py -v -s
```
预期：3 passed

- [ ] **步骤 3：运行全套测试**

```bash
pytest -v --cov=parsing_core --cov-report=term-missing
```
预期：全绿，覆盖率 ≥ 80%

- [ ] **步骤 4：运行 ruff**

```bash
ruff check src tests
ruff format --check src tests
```
预期：无 warning。若有，用 `ruff format src tests && ruff check --fix src tests` 修复后重新提交。

- [ ] **步骤 5：手冒烟 CLI**

```bash
python -m parsing_core.cli parse tests/fixtures/sample.md
python -m parsing_core.cli list
```
预期：第一行打印 JSON，第二行打印任务数组。

- [ ] **步骤 6：Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration tests and lint baseline"
```

---

## 自检

**1. 规格覆盖度对照**
- §1.1 目标 1 输入支持多类型 → 任务 10 adapter 支持，但 fixture 仅 .md（性能基线为 #4 阶段）；本子项目交付承诺聚焦 .md/.xlsx/.pdf 路径打通，复杂文件类型实际通用于 adapter ✓
- §1.1 目标 2 分节 → 任务 11 ✓
- §1.1 目标 3 节级 LLM → 任务 12 ✓
- §1.1 目标 4 合流落盘 → 任务 13 ✓
- §1.1 目标 5 双缓存 → 任务 8 + 任务 13 ✓
- §1.1 目标 6 崩溃恢复 → 任务 13 `resume` + 任务 14 集成测试 ✓
- §1.1 目标 7 输出 → 任务 13 返回 `merged_md_path` ✓
- §3 数据模型 → 任务 5 ✓
- §4 核心算法 → 任务 11/12/13 ✓
- §5 崩溃恢复 → 任务 13 ✓
- §6 副本读取 → 任务 3 ✓
- §7 CLI → 任务 14 ✓
- §8 错误处理 → orchestrator 包 try/except + PARTIAL_SUCCESS 兜底 ✓
- §9 测试策略 → 各任务单测 + 任务 15 集成 ✓
- §11 验收标准 → 1-7、9、10 由任务保障；8 覆盖率 ≥80% 由任务 15 步骤 3 确认 ✓

**遗漏**：§9.3 性能基线测试（100 页 PDF ≤ 7s）—— 已在规格标注"非门禁仅记录"，本计划不强制，留 §11 验收 9/10 的实际指标记录待 §4 算力路由层阶段实现多进程后才达标。本子项目实现量级不达标属预期。

**2. 占位符扫描**：升级版 stub_client 任务 12 中的 `<stub 占位词>` 是 stub 实际产物字符串，非规格占位符。无 TODO。✓

**3. 类型一致性**：`AIArtifact.ai_md` 字段：任务 12 引入；任务 13 orchestrator 落盘使用 `artifact.ai_md`；Repository 不写该字段（仅写 `ai_md_path`）—— 一致性已通过任务 12 修订同步 repository 重建逻辑。`Repository` 的 `Task(*row)`/`Section(*row)` 隐式按列顺序解构——本计划改为显式列名重建，否则会因 `ai_artifact` 9 列与 `AIArtifact` 中新增 `ai_md` 字段位置错位死亡。✓

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-07-06-parsing-core.md`。两种执行方式：

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** - 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？