import json
import sqlite3
import threading
from pathlib import Path

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench import pipeline as pipeline_module
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.hybrid import HybridIntensiveReadingExecutor
from parsing_core.workbench.pipeline import IntensiveReadingPipeline
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_state import NOT_READY, READY, STALE, refresh_topic_status


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


def setup_topic(repo, chapter, *, published=False):
    topic = repo.create_topic(
        chapter.course_id,
        len(repo.list_topics(chapter.course_id)),
        "竞争优势",
    )
    repo.update_topic(topic.id, confirmed=True)
    repo.replace_topic_chapters(topic.id, [chapter.id])
    if published:
        repo.replace_topic_note_blocks(topic.id, {"summary": "旧主题摘要"})
    return topic


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


def test_two_connections_compete_for_chapter_generation(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    second_conn = sqlite3.connect(tmp_path / "workbench.db", check_same_thread=False)
    second_conn.execute("PRAGMA foreign_keys = ON")
    second = WorkbenchRepository(second_conn)
    barrier = threading.Barrier(2)
    outcomes = []

    def claim(candidate):
        barrier.wait()
        try:
            outcomes.append(candidate.start_chapter_generation(chapter.id).owner_id)
        except ValueError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=claim, args=(candidate,)) for candidate in (repo, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(len(item) == 32 for item in outcomes) == 1
    assert any("already running" in item for item in outcomes)
    assert repo.get_chapter(chapter.id).status == "RUNNING"


def test_recover_expired_chapter_generation_marks_run_interrupted(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10)
    run = repo.create_chapter_generation_run(chapter.id, start.owner_id, "structure", now=100)

    with pytest.raises(ValueError, match="lease not expired"):
        repo.recover_interrupted_chapter_run(chapter.id, now=109)
    recovered = repo.recover_interrupted_chapter_run(chapter.id, now=111)

    assert recovered.status == "FAILED"
    stored = repo.get_chapter_generation_run(run.id)
    assert stored.status == "FAILED" and stored.error == "interrupted"
    assert repo.get_chapter_generation_lease(chapter.id) is None


def test_failed_generation_preserves_previous_published_chapter(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    repo.upsert_note_block(chapter.id, "summary", "本章概要", "旧成功内容", 0)
    repo.create_card(chapter.course_id, chapter.id, "topic", "旧卡片", "旧卡片内容")
    repo.update_chapter_status(chapter.id, "FAILED")

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "concepts":
                raise RuntimeError("boom")
            return super().run(round_key, task_package)

    with pytest.raises(RuntimeError, match="boom"):
        IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "runs").run_all(chapter.id)

    assert repo.list_note_blocks(chapter.id)[0].body == "旧成功内容"
    assert repo.list_cards_by_chapter(chapter.id)[0].title == "旧卡片"
    assert repo.get_chapter(chapter.id).status == "FAILED"


def test_review_receives_all_six_candidates_and_rejection_does_not_publish(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    seen = {}

    class RejectingReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "review":
                package = json.loads(task_package)
                seen.update(package["candidates"])
                return json.dumps(
                    {"passed": False, "issues": ["来源不足"], "revised_blocks": {}},
                    ensure_ascii=False,
                )
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="chapter review rejected"):
        IntensiveReadingPipeline(repo, RejectingReview(), tmp_path / "runs").run_all(chapter.id)

    assert set(seen) == {
        "structure",
        "concepts",
        "plain_explain",
        "application",
        "mermaid",
        "cards",
    }
    assert repo.list_note_blocks(chapter.id) == []
    assert repo.list_cards_by_chapter(chapter.id) == []
    assert repo.get_chapter(chapter.id).status == "FAILED"


def test_review_must_return_exact_fixed_blocks(tmp_path):
    repo, chapter = setup_chapter(tmp_path)

    class InvalidReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "review":
                return json.dumps({"passed": True, "issues": [], "revised_blocks": {}})
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="fixed chapter blocks"):
        IntensiveReadingPipeline(repo, InvalidReview(), tmp_path / "runs").run_all(chapter.id)

    assert repo.get_chapter(chapter.id).status == "FAILED"


@pytest.mark.parametrize(
    ("failure_step", "trigger_sql"),
    [
        (
            "blocks",
            "CREATE TRIGGER fail_publish BEFORE INSERT ON wb_note_blocks "
            "BEGIN SELECT RAISE(ABORT, 'blocks failed'); END",
        ),
        (
            "cards",
            "CREATE TRIGGER fail_publish BEFORE INSERT ON wb_cards "
            "BEGIN SELECT RAISE(ABORT, 'cards failed'); END",
        ),
        (
            "runs",
            "CREATE TRIGGER fail_publish BEFORE UPDATE ON wb_runs "
            "BEGIN SELECT RAISE(ABORT, 'runs failed'); END",
        ),
        (
            "review",
            "CREATE TRIGGER fail_publish BEFORE UPDATE ON wb_chapter_generation_runs "
            "BEGIN SELECT RAISE(ABORT, 'review failed'); END",
        ),
        (
            "chapter",
            "CREATE TRIGGER fail_publish BEFORE UPDATE ON wb_chapters "
            "WHEN NEW.status = 'COMPLETED' BEGIN SELECT RAISE(ABORT, 'chapter failed'); END",
        ),
        (
            "lease",
            "CREATE TRIGGER fail_publish BEFORE DELETE ON wb_chapter_generation_leases "
            "BEGIN SELECT RAISE(ABORT, 'lease failed'); END",
        ),
    ],
)
def test_publish_chapter_generation_rolls_back_every_table_on_each_step_failure(
    tmp_path, failure_step, trigger_sql
):
    repo, chapter = setup_chapter(tmp_path)
    repo.upsert_note_block(chapter.id, "summary", "本章概要", "旧块", 0)
    repo.create_card(chapter.course_id, chapter.id, "topic", "旧卡", "旧卡内容")
    repo.upsert_run(chapter.id, "structure", "old", "DONE", "old-in", "old-out", "旧轮次")
    repo.update_chapter_status(chapter.id, "FAILED")
    start = repo.start_chapter_generation(chapter.id, now=100, lease_ttl=10_000_000_000)
    candidate = repo.create_chapter_generation_run(chapter.id, start.owner_id, "structure", now=101)
    repo.finish_chapter_generation_run(
        candidate.id, start.owner_id, "COMPLETED", output="新候选", now=102
    )
    review = repo.create_chapter_generation_run(chapter.id, start.owner_id, "review", now=103)

    tracked_tables = (
        "wb_note_blocks",
        "wb_cards",
        "wb_runs",
        "wb_chapter_generation_runs",
        "wb_chapter_generation_candidates",
        "wb_chapters",
        "wb_chapter_generation_leases",
    )
    before = {
        table: repo.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in tracked_tables
    }
    repo.conn.execute(trigger_sql)

    with pytest.raises(sqlite3.IntegrityError, match=f"{failure_step} failed"):
        repo.publish_chapter_generation(
            chapter.id,
            start.owner_id,
            {"summary": ("本章概要", "新块", 0)},
            ("新卡", "新卡内容"),
            review.id,
            '{"passed":true,"issues":[],"revised_blocks":{}}',
            {"structure": ("new-in", "new-out")},
        )

    after = {
        table: repo.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in tracked_tables
    }
    assert after == before


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

    note_path = tmp_path / "out" / "教材" / "战略教材" / "01-第一章" / "intensive-note.md"
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
    assert "concepts" not in stale
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


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
def test_pipeline_rejects_missing_chapter_without_run_side_effects(tmp_path, operation):
    repo, _ = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    args = ("missing-chapter",) if operation == "run_all" else ("missing-chapter", "concepts")

    with pytest.raises(ValueError, match="^chapter not found$"):
        getattr(pipeline, operation)(*args)

    run_count = repo.conn.execute("SELECT COUNT(*) FROM wb_runs").fetchone()[0]
    assert run_count == 0


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
def test_pipeline_wraps_markdown_sync_filesystem_errors(tmp_path, monkeypatch, operation):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    def fail_sync(repo, chapter_id):
        raise OSError(f"cannot write {tmp_path}/private/intensive-note.md")

    monkeypatch.setattr(pipeline_module, "sync_chapter_markdown", fail_sync)
    args = (chapter.id,) if operation == "run_all" else (chapter.id, "concepts")

    with pytest.raises(pipeline_module.ChapterMarkdownSyncError):
        getattr(pipeline, operation)(*args)


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_pipeline_preserves_markdown_sync_business_errors(
    tmp_path, monkeypatch, operation, error_type
):
    repo, chapter = setup_chapter(tmp_path)
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")

    def fail_sync(repo, chapter_id):
        raise error_type("chapter not found")

    monkeypatch.setattr(pipeline_module, "sync_chapter_markdown", fail_sync)
    args = (chapter.id,) if operation == "run_all" else (chapter.id, "concepts")

    with pytest.raises(error_type, match="chapter not found"):
        getattr(pipeline, operation)(*args)


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


def test_run_all_refreshes_unpublished_topic_after_review_completes(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    topic = setup_topic(repo, chapter)
    assert refresh_topic_status(repo, topic.id).status == NOT_READY

    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    pipeline.run_all(chapter.id)

    assert repo.get_topic(topic.id).status == READY


@pytest.mark.parametrize("operation", ["run_all", "rerun"])
def test_pipeline_invalidates_topics_before_execution(tmp_path, operation):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    published_topic = setup_topic(repo, chapter, published=True)
    unpublished_topic = setup_topic(repo, chapter)
    assert refresh_topic_status(repo, unpublished_topic.id).status == READY
    unrelated_source = repo.create_source(chapter.course_id, "attachment", "/tmp/case.pdf", "案例")
    unrelated_chapter = repo.create_chapter(
        chapter.course_id, unrelated_source.id, 0, "案例", str(tmp_path / "case.md")
    )
    unrelated = setup_topic(repo, unrelated_chapter, published=True)
    repo.update_topic(unrelated.id, status=READY)
    observed_statuses = []

    class AssertingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if not observed_statuses:
                observed_statuses.append(
                    (
                        repo.get_topic(unpublished_topic.id).status,
                        repo.get_topic(published_topic.id).status,
                        repo.get_topic(unrelated.id).status,
                    )
                )
            return super().run(round_key, task_package)

    pipeline = IntensiveReadingPipeline(repo, AssertingExecutor(), tmp_path / "changed")
    if operation == "run_all":
        repo.update_chapter_status(chapter.id, "FAILED")
    args = (chapter.id,) if operation == "run_all" else (chapter.id, "concepts")
    getattr(pipeline, operation)(*args)

    assert observed_statuses == [(NOT_READY, STALE, READY)]
    assert repo.get_topic(published_topic.id).status == STALE
    assert repo.get_topic(unrelated.id).status == READY


def test_failed_rerun_makes_unpublished_topic_not_ready(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    topic = setup_topic(repo, chapter)
    assert refresh_topic_status(repo, topic.id).status == READY

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            raise RuntimeError("executor failed")

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "failed")
    with pytest.raises(RuntimeError, match="executor failed"):
        pipeline.rerun(chapter.id, "concepts")

    assert repo.get_topic(topic.id).status == NOT_READY


def test_failed_rerun_keeps_published_topic_stale_and_outputs(tmp_path):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    topic = setup_topic(repo, chapter, published=True)
    old_notes = repo.list_topic_note_blocks(topic.id)

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            raise RuntimeError("executor failed")

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), tmp_path / "failed")
    with pytest.raises(RuntimeError, match="executor failed"):
        pipeline.rerun(chapter.id, "concepts")

    assert repo.get_topic(topic.id).status == STALE
    assert repo.list_topic_note_blocks(topic.id) == old_notes


@pytest.mark.parametrize(
    ("failure_mode", "error_type"),
    [
        ("missing_source", FileNotFoundError),
        ("mkdir", PermissionError),
        ("write_package", OSError),
        ("executor", RuntimeError),
    ],
)
def test_rerun_records_safe_failed_run_for_preparation_and_execution_errors(
    tmp_path,
    monkeypatch,
    failure_mode,
    error_type,
):
    repo, chapter = setup_chapter(tmp_path)
    IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "initial").run_all(
        chapter.id
    )
    run_dir = tmp_path / "failed"

    class FailingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key: str, task_package: str) -> str:
            if failure_mode == "executor":
                raise RuntimeError(f"cannot access {tmp_path}/private/model")
            return super().run(round_key, task_package)

    if failure_mode == "missing_source":
        Path(chapter.source_md_path).unlink()
    elif failure_mode == "mkdir":
        original_mkdir = Path.mkdir

        def fail_run_dir_mkdir(path, *args, **kwargs):
            if path == run_dir:
                raise PermissionError(f"cannot create {tmp_path}/private/runs")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_run_dir_mkdir)
    elif failure_mode == "write_package":

        def fail_write_package(package, base_dir):
            raise OSError(f"cannot write {tmp_path}/private/task.md")

        monkeypatch.setattr(pipeline_module, "write_task_package", fail_write_package)

    pipeline = IntensiveReadingPipeline(repo, FailingExecutor(), run_dir)
    with pytest.raises(error_type):
        pipeline.rerun(chapter.id, "concepts")

    runs = {run.round_key: run for run in repo.list_runs(chapter.id)}
    assert runs["concepts"].status == "FAILED"
    assert runs["concepts"].stale is False
    assert error_type.__name__ in runs["concepts"].output
    assert "intensive reading round failed" in runs["concepts"].output
    assert str(tmp_path) not in runs["concepts"].output
    assert runs["review"].stale is True
