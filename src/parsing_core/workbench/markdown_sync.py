import re
from pathlib import Path

from parsing_core.workbench.models import Card, Chapter, NoteBlock
from parsing_core.workbench.repository import WorkbenchRepository

MERMAID_FENCE_RE = re.compile(r"^\s*```mermaid\s*\n(.*?)```\s*$", re.DOTALL | re.IGNORECASE)


def sync_chapter_markdown(repo: WorkbenchRepository, chapter_id: str) -> dict[str, str]:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")

    course = repo.get_course(chapter.course_id)
    if course is None:
        raise ValueError("course not found")

    chapter_dir = Path(course.root_dir) / f"{chapter.seq + 1:02d}-{_safe_name(chapter.title)}"
    attachments_dir = chapter_dir / "attachments"
    runs_dir = chapter_dir / "runs"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    source_path = chapter_dir / "source.md"
    source_md_path = Path(chapter.source_md_path)
    if source_md_path.exists():
        source_path.write_text(source_md_path.read_text(encoding="utf-8"), encoding="utf-8")

    note_path = chapter_dir / "intensive-note.md"
    note_path.write_text(
        _render_note(chapter, repo.list_note_blocks(chapter.id)),
        encoding="utf-8",
    )

    cards_path = chapter_dir / "cards.md"
    cards_path.write_text(
        _render_cards(chapter, repo.list_cards_by_chapter(chapter.id)),
        encoding="utf-8",
    )

    return {"source": str(source_path), "note": str(note_path), "cards": str(cards_path)}


def _safe_name(value: str) -> str:
    return value.replace("/", "-").replace("\\", "-")


def _render_note(chapter: Chapter, blocks: list[NoteBlock]) -> str:
    lines = [f"# {chapter.title}", ""]
    for block in blocks:
        lines.extend([f"## {block.title}", ""])
        if block.kind.endswith("_mermaid"):
            lines.extend(["```mermaid", _pure_mermaid(block.body), "```", ""])
        else:
            lines.extend([block.body, ""])
    return "\n".join(lines)


def _pure_mermaid(body: str) -> str:
    match = MERMAID_FENCE_RE.match(body)
    if match:
        return match.group(1).strip()
    return body.strip()


def _render_cards(chapter: Chapter, cards: list[Card]) -> str:
    lines = [f"# {chapter.title} 写作卡片", ""]
    for card in cards:
        favorite = "是" if card.favorite else "否"
        lines.extend(
            [
                f"## {card.title}",
                "",
                f"类型：{card.kind}",
                f"收藏：{favorite}",
                "",
                card.body,
                "",
            ]
        )
    return "\n".join(lines)
