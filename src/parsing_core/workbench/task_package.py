import json
from dataclasses import dataclass
from pathlib import Path

from parsing_core.workbench.source_import import source_anchors

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
    input_fingerprint: str = ""
    citation_ids: tuple[str, ...] = ()


def build_task_package(repo, chapter_id: str, round_key: str) -> TaskPackage:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")

    source_text = Path(chapter.source_md_path).read_text(encoding="utf-8")
    snapshot, fingerprint = repo.chapter_input_snapshot(chapter_id)
    main_anchors = source_anchors(source_text, chapter.source_id[:12], prefix="src")
    attachment_citation_ids = tuple(
        anchor["citation_id"]
        for attachment in snapshot["attachments"]
        for anchor in attachment["anchors"]
    )
    attachment_text = "\n".join(
        f"[{anchor['citation_id']}] {anchor['text']}"
        for attachment in repo.list_attachments(chapter_id)
        for anchor in json.loads(attachment.anchors_json)
    )
    source_text_with_citations = "\n\n".join(
        f"[{anchor['citation_id']}] {anchor['text']}" for anchor in main_anchors
    )
    citation_ids = tuple(anchor["citation_id"] for anchor in main_anchors) + attachment_citation_ids
    content = f"""\
# {chapter.title} - {round_key}

## 精读规则
{READING_RULES}
## 原文
{source_text_with_citations}
## 附件来源
{attachment_text}
"""
    return TaskPackage(chapter.id, round_key, chapter.title, content, fingerprint, citation_ids)


def write_task_package(package: TaskPackage, base_dir: str | Path) -> str:
    path = Path(base_dir) / f"{package.chapter_id}-{package.round_key}-task.md"
    path.write_text(package.content, encoding="utf-8")
    return str(path)


def build_review_package(repo, chapter_id: str, candidates: dict[str, str]) -> str:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")
    expected = {"structure", "concepts", "plain_explain", "application", "mermaid", "cards"}
    if set(candidates) != expected:
        raise ValueError("review requires all six chapter candidates")
    return json.dumps(
        {
            "chapter_id": chapter_id,
            "chapter_title": chapter.title,
            "contract": {
                "passed": "boolean",
                "issues": "string[]",
                "revised_blocks": "object with all fixed chapter blocks",
            },
            "candidates": candidates,
        },
        ensure_ascii=False,
    )
