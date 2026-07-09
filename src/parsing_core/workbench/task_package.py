from dataclasses import dataclass
from pathlib import Path

READING_RULES = """\
- 你是用户的 MBA 精读助教。
- 概念通俗、有趣、生活化。
- 保留严谨性。
- 结合案例。
- 落到实际应用。
- 服务贴文和公众号长文素材。
- 每章最终必须包含两张 Mermaid 图：知识结构图和应用流程图。
"""


@dataclass(frozen=True)
class TaskPackage:
    chapter_id: str
    round_key: str
    title: str
    content: str


def build_task_package(repo, chapter_id: str, round_key: str) -> TaskPackage:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")

    source_text = Path(chapter.source_md_path).read_text(encoding="utf-8")
    content = f"""\
# {chapter.title} - {round_key}

## 精读规则
{READING_RULES}
## 原文
{source_text}
"""
    return TaskPackage(chapter.id, round_key, chapter.title, content)


def write_task_package(package: TaskPackage, base_dir: str | Path) -> str:
    path = Path(base_dir) / f"{package.chapter_id}-{package.round_key}-task.md"
    path.write_text(package.content, encoding="utf-8")
    return str(path)
