import json
import sqlite3
import threading

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench import topic_pipeline as topic_pipeline_module
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_pipeline import (
    FIXED_TOPIC_KINDS,
    TopicFusionPipeline,
    TopicMarkdownSyncError,
    validate_mermaid_subset,
)
from parsing_core.workbench.topic_task_package import build_topic_task_package


def setup_topic(tmp_path, *, published=False):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapter = repo.create_chapter(course.id, source.id, 0, "战略基础", "/forbidden.md")
    repo.upsert_note_block(chapter.id, "summary", "摘要", "战略是选择。", 0)
    repo.upsert_run(chapter.id, "review", "test", "DONE", "", "", "ok")
    topic = repo.create_topic(course.id, 0, "战略选择", "融合精读")
    repo.update_topic(topic.id, confirmed=True, status="READY")
    repo.replace_topic_chapters(topic.id, [chapter.id])
    if published:
        repo.replace_topic_note_blocks(topic.id, {"old": "旧内容"})
        repo.replace_topic_cards(
            topic.id,
            [
                {
                    "card_type": "old",
                    "title": "旧卡",
                    "content": "旧卡内容",
                    "source_refs_json": [],
                }
            ],
        )
    return repo, topic, chapter


def test_pipeline_atomically_publishes_fourteen_blocks_and_cards(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).run(topic.id)

    assert {block.kind for block in repo.list_topic_note_blocks(topic.id)} == set(FIXED_TOPIC_KINDS)
    assert 8 <= len(repo.list_topic_cards(topic.id)) <= 12
    assert repo.get_topic(topic.id).status == "COMPLETED"
    assert [run.round_key for run in repo.list_topic_runs(topic.id)] == [
        "alignment",
        "comparison",
        "plain_cases",
        "framework_application",
        "mermaid",
        "cards",
        "review",
    ]
    assert all(run.status == "COMPLETED" for run in repo.list_topic_runs(topic.id))
    assert repo.get_topic_generation_lease(topic.id) is None


def test_pipeline_syncs_markdown_only_after_database_publication(tmp_path, monkeypatch):
    repo, topic, _ = setup_topic(tmp_path)
    observed = []

    def observe_sync(current_repo, topic_id, *, fence):
        fence()
        observed.append(
            (
                current_repo.get_topic(topic_id).status,
                len(current_repo.list_topic_note_blocks(topic_id)),
            )
        )

    monkeypatch.setattr(topic_pipeline_module, "sync_topic_markdown", observe_sync)
    TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).run(topic.id)
    assert observed == [("COMPLETED", 14)]


def test_markdown_sync_failure_does_not_rollback_or_mark_model_run_failed(tmp_path, monkeypatch):
    repo, topic, _ = setup_topic(tmp_path)

    def fail_sync(repo, topic_id, *, fence):
        fence()
        raise OSError("disk full")

    monkeypatch.setattr(topic_pipeline_module, "sync_topic_markdown", fail_sync)
    with pytest.raises(TopicMarkdownSyncError):
        TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).run(topic.id)
    assert repo.get_topic(topic.id).status == "COMPLETED"
    assert repo.list_topic_runs(topic.id)[-1].status == "COMPLETED"
    assert len(repo.list_topic_note_blocks(topic.id)) == 14
    assert repo.get_topic_markdown_sync_state(topic.id).status == "FAILED"


def test_retry_markdown_sync_uses_published_db_without_model(tmp_path, monkeypatch):
    repo, topic, _ = setup_topic(tmp_path)
    pipeline = TopicFusionPipeline(repo, StubIntensiveReadingExecutor())
    pipeline.run(topic.id)
    repo.set_topic_markdown_sync_state(topic.id, "FAILED", "disk failed")
    calls = []

    def observe_retry(repo, topic_id, *, fence):
        fence()
        calls.append(topic_id)

    monkeypatch.setattr(topic_pipeline_module, "sync_topic_markdown", observe_retry)
    pipeline.retry_markdown_sync(topic.id)
    assert calls == [topic.id]
    assert repo.get_topic_markdown_sync_state(topic.id).status == "SYNCED"


@pytest.mark.parametrize("status", ["STALE", "FAILED"])
def test_retry_markdown_sync_accepts_stale_or_failed_complete_publication(
    tmp_path, monkeypatch, status
):
    repo, topic, _ = setup_topic(tmp_path)
    pipeline = TopicFusionPipeline(repo, StubIntensiveReadingExecutor())
    pipeline.run(topic.id)
    repo.update_topic(topic.id, status=status)
    assert repo.get_topic_markdown_sync_state(topic.id).status == "SYNCED"
    repo.set_topic_markdown_sync_state(topic.id, "FAILED", "retry requested")
    calls = []

    def observe_retry(repo, topic_id, *, fence):
        fence()
        calls.append(topic_id)

    monkeypatch.setattr(topic_pipeline_module, "sync_topic_markdown", observe_retry)

    pipeline.retry_markdown_sync(topic.id)

    assert calls == [topic.id]
    assert repo.get_topic_markdown_sync_state(topic.id).status == "SYNCED"


def test_retry_markdown_sync_rejects_failed_without_publication(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    repo.update_topic(topic.id, status="FAILED")
    with pytest.raises(ValueError, match="complete publication"):
        TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).retry_markdown_sync(topic.id)


def test_retry_markdown_sync_rejects_stale_incomplete_publication(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    pipeline = TopicFusionPipeline(repo, StubIntensiveReadingExecutor())
    pipeline.run(topic.id)
    repo.update_topic(topic.id, status="STALE")
    repo.conn.execute(
        "DELETE FROM wb_topic_note_blocks WHERE topic_id = ? AND kind = 'overview'",
        (topic.id,),
    )
    repo.conn.commit()
    repo.set_topic_markdown_sync_state(topic.id, "FAILED", "old sync failed")
    with pytest.raises(ValueError, match="complete publication"):
        pipeline.retry_markdown_sync(topic.id)


class FailingExecutor(StubIntensiveReadingExecutor):
    def __init__(self, round_key):
        self.round_key = round_key

    def run(self, round_key, task_package):
        if round_key == self.round_key:
            raise RuntimeError("secret /tmp/source.md")
        return super().run(round_key, task_package)


@pytest.mark.parametrize("round_key", ["comparison", "review"])
def test_round_failure_preserves_old_publication_and_records_safe_error(tmp_path, round_key):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    old_notes = repo.list_topic_note_blocks(topic.id)
    old_cards = repo.list_topic_cards(topic.id)

    with pytest.raises(RuntimeError):
        TopicFusionPipeline(repo, FailingExecutor(round_key)).run(topic.id)

    assert repo.list_topic_note_blocks(topic.id) == old_notes
    assert repo.list_topic_cards(topic.id) == old_cards
    assert repo.get_topic(topic.id).status == "STALE"
    failed = [run for run in repo.list_topic_runs(topic.id) if run.status == "FAILED"]
    assert failed and "/tmp" not in failed[0].error


def test_review_rejection_preserves_old_publication(tmp_path):
    class RejectingReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "review":
                return json.dumps({"passed": False, "issues": ["来源不足"]}, ensure_ascii=False)
            return super().run(round_key, task_package)

    repo, topic, _ = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)
    with pytest.raises(ValueError, match="topic review rejected"):
        TopicFusionPipeline(repo, RejectingReview()).run(topic.id)
    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old


def test_publish_rejects_dependency_change_during_run(tmp_path):
    repo, topic, chapter = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)

    class MutatingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "cards":
                repo.upsert_note_block(chapter.id, "summary", "摘要", "变化", 0)
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="topic input changed"):
        TopicFusionPipeline(repo, MutatingExecutor()).run(topic.id)
    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old


def test_only_one_request_can_start_and_interrupted_run_can_be_recovered(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    fingerprint = repo.topic_input_snapshot(topic.id)[1]
    started = repo.start_topic_generation(topic.id, fingerprint, now=100, lease_ttl=100)
    with pytest.raises(ValueError, match="topic is already running"):
        repo.start_topic_generation(topic.id, fingerprint, now=100, lease_ttl=100)
    recovered = repo.recover_interrupted_topic_run(topic.id, now=201)
    assert recovered.status == "FAILED"
    assert started.owner_id


@pytest.mark.parametrize(
    "diagram",
    [
        "graph TD\n  A --> B\n  ???",
        "flowchart LR\n  A[未闭合 --> B",
        "graph TD\n  A[只有节点]",
        "graph TD\n  A --> B\n  click A https://evil.example",
        "graph TD\n  A --> B\n  这是任意自然语言",
        "graph TD\n  end\n  A --> B",
        "graph TD\n  subgraph S[一]\n  A --> B\n  end\n  end",
        "```mermaid\ngraph TD\nA --> B\n```",
        "graph TD\n  A[<script>alert(1)</script>] --> B",
    ],
)
def test_mermaid_subset_rejects_invalid_or_dangerous_syntax(diagram):
    with pytest.raises(ValueError, match="invalid Mermaid diagram"):
        validate_mermaid_subset(diagram)


def test_mermaid_subset_accepts_supported_chinese_subgraph_and_styles():
    validate_mermaid_subset(
        """flowchart LR
%% 合法注释
subgraph S[战略分析]
  A[识别问题] --> B(比较方案)
  B -->|形成选择| C{执行?}
end
classDef focus fill:#fff,stroke:#333
class A,B focus
style C fill:#eee
linkStyle 0 stroke:#333
"""
    )


def test_mermaid_subset_accepts_nested_subgraphs():
    validate_mermaid_subset(
        """graph TD
subgraph A[外层]
subgraph B[内层]
  X[开始] --> Y[结束]
end
end
"""
    )


def test_duplicate_source_labels_are_preserved_in_card_refs(tmp_path):
    repo, topic, chapter = setup_topic(tmp_path)
    source = repo.create_source(topic.course_id, "main", "/tmp/book2.pdf", "战略教材")
    second = repo.create_chapter(topic.course_id, source.id, 0, "另一章", "/other.md")
    repo.upsert_note_block(second.id, "summary", "摘要", "另一观点", 0)
    repo.upsert_run(second.id, "review", "test", "DONE", "", "", "ok")
    repo.replace_topic_chapters(topic.id, [chapter.id, second.id])

    TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).run(topic.id)

    refs = json.loads(repo.list_topic_cards(topic.id)[0].source_refs_json)
    assert refs == ["[《战略教材》·第 1 章]", "[《战略教材（2）》·第 1 章]"]


def _start(repo, topic):
    fingerprint = repo.topic_input_snapshot(topic.id)[1]
    return repo.start_topic_generation(topic.id, fingerprint, now=100, lease_ttl=100)


def test_live_lease_heartbeat_requires_owner_and_blocks_recovery(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    started = _start(repo, topic)
    repo.create_topic_run(topic.id, "alignment", started.input_fingerprint)

    lease = repo.heartbeat_topic_generation(topic.id, started.owner_id, now=150, lease_ttl=100)
    assert (lease.owner_id, lease.heartbeat_at, lease.expires_at) == (started.owner_id, 150, 250)
    with pytest.raises(ValueError, match="lease lost"):
        repo.heartbeat_topic_generation(topic.id, "wrong-owner", now=151, lease_ttl=100)
    with pytest.raises(ValueError, match="lease not expired"):
        repo.recover_interrupted_topic_run(topic.id, now=249)

    assert repo.get_topic(topic.id).status == "RUNNING"
    assert repo.list_topic_runs(topic.id)[0].status == "RUNNING"
    assert repo.get_topic_generation_lease(topic.id).owner_id == started.owner_id


def test_pipeline_stops_when_lease_owner_changes_during_model_call(tmp_path):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)
    calls = []

    class StealingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            calls.append(round_key)
            repo.conn.execute(
                "UPDATE wb_topic_generation_leases SET owner_id = 'replacement' WHERE topic_id = ?",
                (topic.id,),
            )
            repo.conn.commit()
            return super().run(round_key, task_package)

    with pytest.raises(ValueError, match="lease lost"):
        TopicFusionPipeline(repo, StealingExecutor(), clock=lambda: 100, lease_ttl=100).run(
            topic.id
        )

    assert calls == ["alignment"]
    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old
    assert repo.list_topic_runs(topic.id)[0].status == "FAILED"
    assert repo.get_topic(topic.id).status == "RUNNING"
    assert repo.get_topic_generation_lease(topic.id).owner_id == "replacement"


class FakeClock:
    def __init__(self, value):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = value


def test_blocking_executor_is_renewed_in_background_and_cannot_be_recovered(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    clock = FakeClock(100)
    entered = threading.Event()
    release = threading.Event()
    background_heartbeat = threading.Event()
    original_heartbeat = repo.heartbeat_topic_generation

    def observe_heartbeat(*args, **kwargs):
        lease = original_heartbeat(*args, **kwargs)
        if threading.current_thread().name.startswith("topic-lease-heartbeat-"):
            background_heartbeat.set()
        return lease

    repo.heartbeat_topic_generation = observe_heartbeat

    class BlockingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "alignment":
                entered.set()
                assert release.wait(2)
            return super().run(round_key, task_package)

    errors = []

    def run_pipeline():
        try:
            TopicFusionPipeline(
                repo,
                BlockingExecutor(),
                clock=clock,
                lease_ttl=100,
                heartbeat_interval=0.001,
            ).run(topic.id)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pipeline)
    thread.start()
    assert entered.wait(2)
    clock.set(150)
    assert background_heartbeat.wait(2)
    clock.set(201)

    with pytest.raises(ValueError, match="lease not expired"):
        repo.recover_interrupted_topic_run(topic.id, now=201)

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    assert repo.get_topic(topic.id).status == "COMPLETED"


@pytest.mark.parametrize("lease_change", ["replace", "delete"])
def test_background_heartbeat_detects_lost_lease_before_parsing_or_publish(
    tmp_path, lease_change, monkeypatch
):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)
    entered = threading.Event()
    release = threading.Event()
    lost = threading.Event()
    original_heartbeat = repo.heartbeat_topic_generation
    original_parse = topic_pipeline_module._parse_output
    parse_called = threading.Event()

    def observe_parse(*args, **kwargs):
        parse_called.set()
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(topic_pipeline_module, "_parse_output", observe_parse)

    def observe_loss(*args, **kwargs):
        try:
            return original_heartbeat(*args, **kwargs)
        except ValueError:
            if threading.current_thread().name.startswith("topic-lease-heartbeat-"):
                lost.set()
            raise

    repo.heartbeat_topic_generation = observe_loss

    class BlockingExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            entered.set()
            assert release.wait(2)
            return super().run(round_key, task_package)

    errors = []

    def run_pipeline():
        try:
            TopicFusionPipeline(
                repo,
                BlockingExecutor(),
                clock=lambda: 100,
                lease_ttl=100,
                heartbeat_interval=0.001,
            ).run(topic.id)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pipeline)
    thread.start()
    assert entered.wait(2)
    if lease_change == "replace":
        repo.conn.execute(
            "UPDATE wb_topic_generation_leases SET owner_id = 'replacement' WHERE topic_id = ?",
            (topic.id,),
        )
    else:
        repo.conn.execute("DELETE FROM wb_topic_generation_leases WHERE topic_id = ?", (topic.id,))
    repo.conn.commit()
    assert lost.wait(2)
    release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert len(errors) == 1 and "lease lost" in str(errors[0])
    assert not parse_called.is_set()
    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old
    assert repo.list_topic_runs(topic.id)[0].status == "FAILED"
    assert len(repo.list_topic_runs(topic.id)) == 1


def test_executor_error_stops_background_heartbeat_thread(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)

    class ExplodingExecutor:
        def run(self, round_key, task_package):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        TopicFusionPipeline(
            repo,
            ExplodingExecutor(),
            clock=lambda: 100,
            lease_ttl=100,
            heartbeat_interval=0.001,
        ).run(topic.id)

    assert not any(
        thread.name.startswith("topic-lease-heartbeat-") for thread in threading.enumerate()
    )


def _new_publication():
    return {"new": "新内容"}, [
        {
            "card_type": "new",
            "title": "新卡",
            "content": "新卡内容",
            "source_refs_json": [],
        }
    ]


def _publication_state(repo, topic_id):
    return (
        repo.list_topic_note_blocks(topic_id),
        repo.list_topic_cards(topic_id),
        repo.get_topic(topic_id),
    )


def _publish(repo, topic, start, blocks, cards):
    review = repo.create_topic_run(topic.id, "review", start.input_fingerprint)
    return repo.publish_topic_generation(
        topic.id,
        start.input_fingerprint,
        start.stale_reason_baseline,
        blocks,
        cards,
        review_run_id=review.id,
        owner_id=start.owner_id,
        review_output='{"passed":true,"issues":[]}',
        now=150,
    )


def test_publish_rolls_back_old_publication_and_status_on_insert_trigger_failure(tmp_path):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    start = _start(repo, topic)
    before = _publication_state(repo, topic.id)
    repo.conn.execute("""
        CREATE TRIGGER fail_new_topic_block BEFORE INSERT ON wb_topic_note_blocks
        WHEN NEW.kind = 'new' BEGIN SELECT RAISE(ABORT, 'insert failed'); END
    """)
    blocks, cards = _new_publication()

    with pytest.raises(sqlite3.IntegrityError, match="insert failed"):
        _publish(repo, topic, start, blocks, cards)

    assert _publication_state(repo, topic.id) == before


def test_publish_rolls_back_on_deferred_foreign_key_commit_failure(tmp_path):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    start = _start(repo, topic)
    before = _publication_state(repo, topic.id)
    repo.conn.executescript("""
        CREATE TABLE publish_parent (id TEXT PRIMARY KEY);
        CREATE TABLE publish_audit (
          parent_id TEXT REFERENCES publish_parent(id) DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TRIGGER fail_publish_commit AFTER INSERT ON wb_topic_note_blocks
        WHEN NEW.kind = 'new' BEGIN INSERT INTO publish_audit VALUES ('missing'); END;
    """)
    blocks, cards = _new_publication()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _publish(repo, topic, start, blocks, cards)

    assert _publication_state(repo, topic.id) == before


def test_two_connections_compete_for_topic_start_without_double_success(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    path = tmp_path / "workbench.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    second = WorkbenchRepository(conn)
    fingerprint = repo.topic_input_snapshot(topic.id)[1]
    barrier = threading.Barrier(2)
    outcomes = []

    def start(candidate):
        barrier.wait()
        try:
            candidate.start_topic_generation(topic.id, fingerprint)
            outcomes.append("success")
        except ValueError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=start, args=(candidate,)) for candidate in (repo, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("success") == 1
    assert len(outcomes) == 2
    assert repo.get_topic(topic.id).status == "RUNNING"


@pytest.mark.parametrize("status", ["DRAFT", "NOT_READY", "RUNNING"])
def test_start_gate_rejects_blocked_topic_statuses(tmp_path, status):
    repo, topic, _ = setup_topic(tmp_path)
    repo.update_topic(topic.id, status=status)
    fingerprint = repo.topic_input_snapshot(topic.id)[1]
    with pytest.raises(ValueError):
        repo.start_topic_generation(topic.id, fingerprint)


@pytest.mark.parametrize("status", ["READY", "STALE", "FAILED"])
def test_start_gate_allows_ready_dependencies_for_retryable_statuses(tmp_path, status):
    repo, topic, _ = setup_topic(tmp_path)
    repo.update_topic(topic.id, status=status)
    assert _start(repo, topic).topic.status == "RUNNING"


def test_start_gate_rejects_unready_dependency(tmp_path):
    repo, topic, chapter = setup_topic(tmp_path)
    repo.upsert_run(chapter.id, "review", "test", "FAILED", "", "", "bad")
    repo.update_topic(topic.id, status="FAILED")
    with pytest.raises(ValueError, match="dependencies are not ready"):
        _start(repo, topic)


def test_interrupted_published_topic_becomes_stale_without_output_loss(tmp_path):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)
    started = _start(repo, topic)
    repo.create_topic_run(topic.id, "alignment", started.input_fingerprint)
    recovered = repo.recover_interrupted_topic_run(topic.id, now=201)
    assert recovered.status == "STALE"
    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old
    assert repo.list_topic_runs(topic.id)[-1].status == "FAILED"
    assert repo.list_topic_runs(topic.id)[-1].error == "interrupted"
    assert repo.get_topic_generation_lease(topic.id) is None


def test_interrupted_unpublished_unready_topic_becomes_not_ready(tmp_path):
    repo, topic, chapter = setup_topic(tmp_path)
    _start(repo, topic)
    repo.upsert_run(chapter.id, "review", "test", "FAILED", "", "", "bad")
    assert repo.recover_interrupted_topic_run(topic.id, now=201).status == "NOT_READY"


class MutatingJsonExecutor(StubIntensiveReadingExecutor):
    def __init__(self, target_round, mutate):
        self.target_round = target_round
        self.mutate = mutate

    def run(self, round_key, task_package):
        raw = super().run(round_key, task_package)
        if round_key != self.target_round:
            return raw
        value = json.loads(raw)
        self.mutate(value)
        return json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("count", [7, 13])
def test_local_validation_rejects_card_count_outside_bounds(tmp_path, count):
    repo, topic, _ = setup_topic(tmp_path)
    executor = MutatingJsonExecutor(
        "cards", lambda value: value.__setitem__("cards", value["cards"][:1] * count)
    )
    with pytest.raises(ValueError, match="invalid cards JSON output"):
        TopicFusionPipeline(repo, executor).run(topic.id)


@pytest.mark.parametrize("refs", [[], ["[《伪造教材》·第 1 章]"]])
def test_local_validation_rejects_empty_or_unknown_card_refs(tmp_path, refs):
    repo, topic, _ = setup_topic(tmp_path)

    def mutate(value):
        value["cards"][0]["source_refs"] = refs

    with pytest.raises(ValueError, match="invalid cards JSON|unknown source reference"):
        TopicFusionPipeline(repo, MutatingJsonExecutor("cards", mutate)).run(topic.id)


def test_local_validation_rejects_unknown_source_label_in_text(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)

    def mutate(value):
        value["core_concepts"] += " [《伪造教材》·第 9 章]"

    with pytest.raises(ValueError, match="unknown source label"):
        TopicFusionPipeline(repo, MutatingJsonExecutor("alignment", mutate)).run(topic.id)
    runs = repo.list_topic_runs(topic.id)
    assert [(run.round_key, run.status) for run in runs] == [("alignment", "FAILED")]


def test_publish_trigger_failure_marks_review_failed_and_preserves_old_version(tmp_path):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)

    class TriggeringReview(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            if round_key == "review":
                repo.conn.execute("""
                    CREATE TRIGGER reject_topic_publish
                    BEFORE INSERT ON wb_topic_note_blocks
                    WHEN NEW.kind = 'overview'
                    BEGIN SELECT RAISE(ABORT, 'publish rejected'); END
                """)
            return super().run(round_key, task_package)

    with pytest.raises(sqlite3.IntegrityError, match="publish rejected"):
        TopicFusionPipeline(repo, TriggeringReview()).run(topic.id)

    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old
    review = repo.list_topic_runs(topic.id)[-1]
    assert (review.round_key, review.status) == ("review", "FAILED")
    assert repo.get_topic(topic.id).status == "STALE"
    assert repo.get_topic_generation_lease(topic.id) is None


def test_deferred_publish_commit_failure_marks_review_failed(tmp_path):
    repo, topic, _ = setup_topic(tmp_path, published=True)
    old = repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)
    repo.conn.executescript("""
        CREATE TABLE review_parent (id TEXT PRIMARY KEY);
        CREATE TABLE review_audit (
          parent_id TEXT REFERENCES review_parent(id) DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TRIGGER reject_review_commit AFTER INSERT ON wb_topic_note_blocks
        WHEN NEW.kind = 'overview'
        BEGIN INSERT INTO review_audit VALUES ('missing'); END;
    """)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        TopicFusionPipeline(repo, StubIntensiveReadingExecutor()).run(topic.id)

    assert (repo.list_topic_note_blocks(topic.id), repo.list_topic_cards(topic.id)) == old
    assert repo.list_topic_runs(topic.id)[-1].status == "FAILED"
    assert repo.get_topic(topic.id).status == "STALE"


def test_local_validation_rejects_missing_linked_source_label(tmp_path):
    repo, topic, chapter = setup_topic(tmp_path)
    source = repo.create_source(topic.course_id, "main", "/tmp/second.pdf", "第二教材")
    second = repo.create_chapter(topic.course_id, source.id, 0, "第二章", "/second.md")
    repo.upsert_note_block(second.id, "summary", "摘要", "第二观点", 0)
    repo.upsert_run(second.id, "review", "test", "DONE", "", "", "ok")
    repo.replace_topic_chapters(topic.id, [chapter.id, second.id])

    def mutate(value):
        value["linked_sources"] = "[《战略教材》·第 1 章]"

    with pytest.raises(ValueError, match="linked sources are incomplete"):
        TopicFusionPipeline(repo, MutatingJsonExecutor("alignment", mutate)).run(topic.id)
    assert [(run.round_key, run.status) for run in repo.list_topic_runs(topic.id)] == [
        ("alignment", "FAILED")
    ]


def test_invalid_mermaid_marks_mermaid_round_failed_before_later_rounds(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)

    def mutate(value):
        value["knowledge_diagram"] = "graph TD\n  end\n  A --> B"

    with pytest.raises(ValueError, match="invalid Mermaid diagram"):
        TopicFusionPipeline(repo, MutatingJsonExecutor("mermaid", mutate)).run(topic.id)

    assert [(run.round_key, run.status) for run in repo.list_topic_runs(topic.id)] == [
        ("alignment", "COMPLETED"),
        ("comparison", "COMPLETED"),
        ("plain_cases", "COMPLETED"),
        ("framework_application", "COMPLETED"),
        ("mermaid", "FAILED"),
    ]


def test_local_validation_rejects_total_output_over_limit(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)

    class OversizedExecutor(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            raw = super().run(round_key, task_package)
            if round_key not in {"alignment", "comparison", "plain_cases", "framework_application"}:
                return raw
            value = json.loads(raw)
            for key in value:
                value[key] += "字" * 10_500
            result = json.dumps(value, ensure_ascii=False)
            assert len(result) < 40_000
            return result

    with pytest.raises(ValueError, match="total size limit"):
        TopicFusionPipeline(repo, OversizedExecutor()).run(topic.id)


def test_mermaid_round_prompt_states_supported_subset(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    prompts = {}

    class RecordingStub(StubIntensiveReadingExecutor):
        def run(self, round_key, task_package):
            prompts[round_key] = task_package
            return super().run(round_key, task_package)

    TopicFusionPipeline(repo, RecordingStub()).run(topic.id)
    prompt = json.loads(prompts["mermaid"])["instructions"]
    assert "only graph or flowchart" in prompt
    assert "Do not use click, URLs, HTML, scripts" in prompt


def test_pipeline_rejects_duplicate_labels_before_any_model_round(tmp_path, monkeypatch):
    repo, topic, chapter = setup_topic(tmp_path)
    source = repo.create_source(topic.course_id, "main", "/tmp/second.pdf", "第二教材")
    second = repo.create_chapter(topic.course_id, source.id, 0, "第二章", "/second.md")
    repo.upsert_note_block(second.id, "summary", "摘要", "第二观点", 0)
    repo.upsert_run(second.id, "review", "test", "DONE", "", "", "ok")
    repo.replace_topic_chapters(topic.id, [chapter.id, second.id])
    package = build_topic_task_package(repo, topic.id)
    duplicate = package.source_chapters[1].model_copy(
        update={"source_label": package.source_chapters[0].source_label}
    )
    broken = package.model_copy(update={"source_chapters": [package.source_chapters[0], duplicate]})
    monkeypatch.setattr(topic_pipeline_module, "build_topic_task_package", lambda *args: broken)

    class MustNotRun:
        def run(self, round_key, task_package):
            raise AssertionError("executor must not run")

    with pytest.raises(ValueError, match="topic source labels are not unique"):
        TopicFusionPipeline(repo, MustNotRun()).run(topic.id)
    assert repo.get_topic(topic.id).status == "READY"
    assert repo.list_topic_runs(topic.id) == []
