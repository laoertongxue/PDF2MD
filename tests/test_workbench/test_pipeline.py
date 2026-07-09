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
    assert {b.kind for b in blocks} >= {
        "summary",
        "knowledge_mermaid",
        "application_mermaid",
    }
    assert len(cards) >= 1
    assert len(runs) == 7


def test_rerun_marks_later_rounds_stale(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)

    pipeline.rerun(chapter.id, "concepts")

    stale = {r.round_key for r in repo.list_runs(chapter.id) if r.stale}
    assert {"plain_explain", "application", "mermaid", "cards", "review"} <= stale


def test_rerun_cards_does_not_duplicate_cards(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)
    assert len(repo.list_cards_by_chapter(chapter.id)) == 1

    pipeline.rerun(chapter.id, "cards")

    assert len(repo.list_cards_by_chapter(chapter.id)) == 1
