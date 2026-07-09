import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
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


def test_pipeline_materializes_generated_mermaid_output(tmp_path):
    class CustomMermaidExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if round_key == "mermaid":
                return """\
## 知识结构图

```mermaid
flowchart TD
  StrategyChoice[战略选择] --> TradeoffMap[取舍地图]
```

## 应用流程图

```mermaid
flowchart LR
  ScenarioScan[场景扫描] --> ActionLoop[行动闭环]
```
"""
            return super().run(round_key, task_package)

    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, CustomMermaidExecutor(), tmp_path / "runs")

    pipeline.run_all(chapter.id)

    note_path = tmp_path / "out" / "01-第一章" / "intensive-note.md"
    note = note_path.read_text(encoding="utf-8")
    assert "StrategyChoice[战略选择]" in note
    assert "ScenarioScan[场景扫描]" in note
    assert "A[概念] --> B[结构]" not in note


def test_pipeline_marks_round_failed_when_mermaid_output_is_incomplete(tmp_path):
    class BrokenMermaidExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if round_key == "mermaid":
                return "没有 Mermaid 图"
            return super().run(round_key, task_package)

    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, BrokenMermaidExecutor(), tmp_path / "runs")

    with pytest.raises(ValueError):
        pipeline.run_all(chapter.id)

    mermaid_run = [run for run in repo.list_runs(chapter.id) if run.round_key == "mermaid"][0]
    assert mermaid_run.status == "FAILED"


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


def test_pipeline_allows_failed_chapter_rerun(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    repo.update_chapter_status(chapter.id, "FAILED")
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    pipeline.run_all(chapter.id)

    assert len(repo.list_runs(chapter.id)) == 7


def test_pipeline_skips_codex_task_files_for_mermaid_and_review_rounds(tmp_path):
    class FakeDeepSeekExecutor(StubIntensiveReadingExecutor):
        pass

    class FakeCodexExecutor(StubIntensiveReadingExecutor):
        pass

    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(
        repo,
        HybridIntensiveReadingExecutor(FakeDeepSeekExecutor(), FakeCodexExecutor()),
        tmp_path / "runs",
    )

    pipeline.run_all(chapter.id)

    run_dir = tmp_path / "runs"
    assert not (run_dir / f"{chapter.id}-mermaid-task.md").exists()
    assert not (run_dir / f"{chapter.id}-review-task.md").exists()
    assert (run_dir / f"{chapter.id}-structure-task.md").exists()
    assert (run_dir / f"{chapter.id}-cards-task.md").exists()

    runs = {run.round_key: run for run in repo.list_runs(chapter.id)}
    assert runs["mermaid"].input_path == ""
    assert runs["review"].input_path == ""
    assert runs["structure"].input_path.endswith(f"{chapter.id}-structure-task.md")
