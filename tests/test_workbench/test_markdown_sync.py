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
