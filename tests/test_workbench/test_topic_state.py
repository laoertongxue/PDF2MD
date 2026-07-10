import sqlite3
import threading

import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_state import (
    COMPLETED,
    DRAFT,
    FAILED,
    NOT_READY,
    READY,
    RUNNING,
    STALE,
    TopicReadiness,
    evaluate_topic_readiness,
    mark_topic_stale,
    mark_topics_stale_for_chapter,
    refresh_topic_status,
)


def setup_topic(tmp_path, *, chapter_count=1, confirmed=True):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapters = [
        repo.create_chapter(course.id, source.id, seq, f"第 {seq + 1} 章", f"/tmp/ch{seq}.md")
        for seq in range(chapter_count)
    ]
    topic = repo.create_topic(course.id, 0, "竞争优势")
    repo.update_topic(topic.id, confirmed=confirmed)
    if chapters:
        repo.replace_topic_chapters(topic.id, [chapter.id for chapter in reversed(chapters)])
    return repo, topic, chapters


def set_review(repo, chapter_id, *, status="DONE", stale=False):
    return repo.upsert_run(
        chapter_id,
        "review",
        "test",
        status,
        "",
        "",
        "review output",
        stale=stale,
    )


def test_evaluate_topic_readiness_rejects_unknown_topic(tmp_path):
    repo, _, _ = setup_topic(tmp_path)

    with pytest.raises(ValueError, match="topic not found"):
        evaluate_topic_readiness(repo, "missing-topic")


@pytest.mark.parametrize("confirmed,with_mapping", [(False, True), (True, False)])
def test_topic_without_confirmed_mapping_is_draft(tmp_path, confirmed, with_mapping):
    repo, topic, chapters = setup_topic(tmp_path, confirmed=confirmed)
    if not with_mapping:
        repo.replace_topic_chapters(topic.id, [])
    set_review(repo, chapters[0].id)

    assert evaluate_topic_readiness(repo, topic.id) == TopicReadiness(DRAFT, [])


@pytest.mark.parametrize(
    "review_status,stale",
    [(None, False), ("FAILED", False), ("DONE", True)],
)
def test_incomplete_review_blocks_topic(tmp_path, review_status, stale):
    repo, topic, chapters = setup_topic(tmp_path)
    if review_status is not None:
        set_review(repo, chapters[0].id, status=review_status, stale=stale)

    assert evaluate_topic_readiness(repo, topic.id) == TopicReadiness(
        NOT_READY,
        [chapters[0].id],
    )


def test_blocking_chapter_ids_are_deduplicated_in_chapter_order(tmp_path):
    repo, topic, chapters = setup_topic(tmp_path, chapter_count=3)
    set_review(repo, chapters[1].id)

    readiness = evaluate_topic_readiness(repo, topic.id)

    assert readiness == TopicReadiness(NOT_READY, [chapters[0].id, chapters[2].id])


def test_all_current_reviews_make_confirmed_topic_ready(tmp_path):
    repo, topic, chapters = setup_topic(tmp_path, chapter_count=2)
    for chapter in chapters:
        set_review(repo, chapter.id)

    assert evaluate_topic_readiness(repo, topic.id) == TopicReadiness(READY, [])
    assert refresh_topic_status(repo, topic.id).status == READY


@pytest.mark.parametrize("explicit_status", [RUNNING, COMPLETED, STALE, FAILED])
def test_refresh_does_not_override_explicit_lifecycle_status(tmp_path, explicit_status):
    repo, topic, chapters = setup_topic(tmp_path)
    set_review(repo, chapters[0].id)
    repo.update_topic(topic.id, status=explicit_status)

    refreshed = refresh_topic_status(repo, topic.id)

    assert refreshed.status == explicit_status


def test_published_topic_becomes_stale_without_losing_outputs(tmp_path):
    repo, topic, chapters = setup_topic(tmp_path)
    repo.replace_topic_note_blocks(topic.id, {"summary": "旧摘要"})
    repo.replace_topic_cards(
        topic.id,
        [
            {
                "card_type": "viewpoint",
                "title": "旧卡片",
                "content": "旧内容",
                "source_refs_json": [],
            }
        ],
    )
    run = repo.create_topic_run(topic.id, "synthesis", "old-input")
    repo.finish_topic_run(run.id, COMPLETED, output="旧输出")
    old_notes = repo.list_topic_note_blocks(topic.id)
    old_cards = repo.list_topic_cards(topic.id)
    old_runs = repo.list_topic_runs(topic.id)

    marked = mark_topics_stale_for_chapter(repo, chapters[0].id, "chapter changed")

    assert [item.id for item in marked] == [topic.id]
    assert repo.get_topic(topic.id).status == STALE
    assert repo.get_topic(topic.id).stale_reason == "chapter changed"
    assert repo.list_topic_note_blocks(topic.id) == old_notes
    assert repo.list_topic_cards(topic.id) == old_cards
    assert repo.list_topic_runs(topic.id) == old_runs


def test_note_only_counts_as_published_topic_output(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    repo.replace_topic_note_blocks(topic.id, {"summary": "摘要"})

    assert repo.has_published_topic_output(topic.id) is True


def test_card_only_counts_as_published_topic_output(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    repo.replace_topic_cards(
        topic.id,
        [
            {
                "card_type": "viewpoint",
                "title": "卡片",
                "content": "内容",
                "source_refs_json": [],
            }
        ],
    )

    assert repo.has_published_topic_output(topic.id) is True


def test_completed_run_only_counts_as_published_topic_output(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    run = repo.create_topic_run(topic.id, "synthesis", "input")
    repo.finish_topic_run(run.id, COMPLETED, output="结果")

    assert repo.has_published_topic_output(topic.id) is True


def test_failed_run_only_does_not_count_as_published_topic_output(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    run = repo.create_topic_run(topic.id, "synthesis", "input")
    repo.finish_topic_run(run.id, FAILED, error="失败")

    assert repo.has_published_topic_output(topic.id) is False


def test_unpublished_topic_recomputes_instead_of_becoming_stale(tmp_path):
    repo, topic, chapters = setup_topic(tmp_path)
    set_review(repo, chapters[0].id, status="FAILED")

    marked = mark_topic_stale(repo, topic.id, "chapter changed")

    assert marked.status == NOT_READY
    assert marked.stale_reason == ""


def test_stale_reasons_are_nonempty_deduplicated_and_stably_accumulated(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    repo.replace_topic_note_blocks(topic.id, {"summary": "摘要"})

    with pytest.raises(ValueError, match="reason must be a nonempty single line"):
        mark_topic_stale(repo, topic.id, "  ")
    mark_topic_stale(repo, topic.id, "  chapter changed  ")
    mark_topic_stale(repo, topic.id, "chapter changed")
    marked = mark_topic_stale(repo, topic.id, "mapping changed")

    assert marked.status == STALE
    assert marked.stale_reason == "chapter changed\nmapping changed"


def test_topics_for_chapter_are_returned_in_topic_order(tmp_path):
    repo, first, chapters = setup_topic(tmp_path)
    second = repo.create_topic(first.course_id, 1, "组织能力")
    repo.replace_topic_chapters(second.id, [chapters[0].id])

    assert [topic.id for topic in repo.list_topics_for_chapter(chapters[0].id)] == [
        first.id,
        second.id,
    ]


def test_chapter_dependency_invalidation_rolls_back_runs_and_all_topics(tmp_path):
    repo, first, chapters = setup_topic(tmp_path)
    second = repo.create_topic(first.course_id, 1, "组织能力")
    repo.update_topic(second.id, confirmed=True)
    repo.replace_topic_chapters(second.id, [chapters[0].id])
    set_review(repo, chapters[0].id)
    assert refresh_topic_status(repo, first.id).status == READY
    assert refresh_topic_status(repo, second.id).status == READY
    repo.conn.execute(
        f"""
        CREATE TRIGGER fail_second_topic_invalidation
        BEFORE UPDATE OF status, stale_reason ON wb_topics
        WHEN OLD.id = '{second.id}'
        BEGIN
          SELECT RAISE(ABORT, 'forced topic invalidation failure');
        END
        """
    )
    repo.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced topic invalidation failure"):
        mark_topics_stale_for_chapter(
            repo,
            chapters[0].id,
            "chapter changed",
            round_keys=["review"],
        )

    assert repo.list_runs(chapters[0].id)[0].stale is False
    assert repo.get_topic(first.id).status == READY
    assert repo.get_topic(second.id).status == READY
    assert repo.get_topic(first.id).stale_reason == ""
    assert repo.get_topic(second.id).stale_reason == ""


def test_refresh_does_not_overwrite_topic_that_concurrently_enters_running(
    tmp_path,
):
    repo, topic, chapters = setup_topic(tmp_path)
    set_review(repo, chapters[0].id)
    other = WorkbenchRepository(init_db(str(tmp_path / "workbench.db")))
    other.conn.execute("PRAGMA busy_timeout = 0")
    evaluated = threading.Event()
    resume = threading.Event()
    original_list_reviews = repo.list_topic_chapter_reviews
    paused = False

    def pause_after_read(topic_id):
        nonlocal paused
        reviews = original_list_reviews(topic_id)
        if paused:
            return reviews
        paused = True
        evaluated.set()
        assert resume.wait(timeout=5)
        return reviews

    repo.list_topic_chapter_reviews = pause_after_read
    result = []
    errors = []

    def refresh():
        try:
            result.append(refresh_topic_status(repo, topic.id))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=refresh)
    thread.start()
    assert evaluated.wait(timeout=5)
    update_was_locked = False
    try:
        other.update_topic(topic.id, status=RUNNING)
    except sqlite3.OperationalError as error:
        assert "locked" in str(error)
        update_was_locked = True
    resume.set()
    thread.join(timeout=5)
    if update_was_locked:
        other.update_topic(topic.id, status=RUNNING)

    assert not thread.is_alive()
    assert errors == []
    assert result[0].status in {READY, RUNNING}
    assert repo.get_topic(topic.id).status == RUNNING


def test_refresh_and_dependency_invalidation_cannot_commit_ready_with_stale_review(tmp_path):
    repo, topic, chapters = setup_topic(tmp_path)
    set_review(repo, chapters[0].id)
    assert refresh_topic_status(repo, topic.id).status == READY
    other = WorkbenchRepository(init_db(str(tmp_path / "workbench.db")))
    other.conn.execute("PRAGMA busy_timeout = 0")
    dependencies_read = threading.Event()
    resume = threading.Event()
    original_list_reviews = repo.list_topic_chapter_reviews
    paused = False

    def pause_after_read(topic_id):
        nonlocal paused
        reviews = original_list_reviews(topic_id)
        if paused:
            return reviews
        paused = True
        dependencies_read.set()
        assert resume.wait(timeout=5)
        return reviews

    repo.list_topic_chapter_reviews = pause_after_read
    errors = []

    def refresh():
        try:
            refresh_topic_status(repo, topic.id)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=refresh)
    thread.start()
    assert dependencies_read.wait(timeout=5)
    invalidation_was_locked = False
    try:
        mark_topics_stale_for_chapter(
            other,
            chapters[0].id,
            "chapter changed",
            round_keys=["review"],
        )
    except sqlite3.OperationalError as error:
        assert "locked" in str(error)
        invalidation_was_locked = True
    resume.set()
    thread.join(timeout=5)
    if invalidation_was_locked:
        mark_topics_stale_for_chapter(
            other,
            chapters[0].id,
            "chapter changed",
            round_keys=["review"],
        )

    assert not thread.is_alive()
    assert errors == []
    review = repo.list_runs(chapters[0].id)[0]
    assert review.stale is True
    assert repo.get_topic(topic.id).status == NOT_READY


def test_running_published_topic_keeps_status_and_records_pending_stale_reason(tmp_path):
    repo, topic, _ = setup_topic(tmp_path)
    repo.replace_topic_note_blocks(topic.id, {"summary": "摘要"})
    repo.update_topic(topic.id, status=RUNNING)

    marked = mark_topic_stale(repo, topic.id, "chapter changed")

    assert marked.status == RUNNING
    assert marked.stale_reason == "chapter changed"
    assert repo.list_topic_note_blocks(topic.id)[0].content == "摘要"


@pytest.mark.parametrize("status", [COMPLETED, FAILED, STALE])
def test_published_explicit_topic_becomes_stale(tmp_path, status):
    repo, topic, _ = setup_topic(tmp_path)
    repo.replace_topic_note_blocks(topic.id, {"summary": "摘要"})
    repo.update_topic(topic.id, status=status)

    marked = mark_topic_stale(repo, topic.id, "chapter changed")

    assert marked.status == STALE
    assert marked.stale_reason == "chapter changed"
    assert repo.list_topic_note_blocks(topic.id)[0].content == "摘要"


@pytest.mark.parametrize("status", [COMPLETED, FAILED, STALE])
def test_unpublished_explicit_topic_recomputes_automatic_readiness(tmp_path, status):
    repo, topic, chapters = setup_topic(tmp_path)
    set_review(repo, chapters[0].id)
    repo.update_topic(topic.id, status=status, stale_reason="old reason")

    marked = mark_topic_stale(repo, topic.id, "chapter changed")

    assert marked.status == READY
    assert marked.stale_reason == ""


@pytest.mark.parametrize("reason", ["", "  ", "line one\nline two", "line one\rline two"])
def test_stale_reason_must_be_nonempty_single_line(tmp_path, reason):
    repo, topic, _ = setup_topic(tmp_path)

    with pytest.raises(ValueError, match="reason must be a nonempty single line"):
        mark_topic_stale(repo, topic.id, reason)


def test_concurrent_stale_reasons_from_two_connections_do_not_lose_updates(tmp_path):
    repo_a, topic, _ = setup_topic(tmp_path)
    repo_a.replace_topic_note_blocks(topic.id, {"summary": "摘要"})
    repo_b = WorkbenchRepository(init_db(str(tmp_path / "workbench.db")))
    first_update_reached = threading.Event()
    release_first_update = threading.Event()
    second_begin_attempted = threading.Event()
    second_completed = threading.Event()
    errors = []

    def pause_first_stale_update():
        first_update_reached.set()
        assert release_first_update.wait(timeout=5)

    repo_a.conn.create_function("pause_first_stale_update", 0, pause_first_stale_update)
    repo_a.conn.execute(
        f"""
        CREATE TEMP TRIGGER pause_first_stale_reason_update
        BEFORE UPDATE OF stale_reason ON main.wb_topics
        WHEN OLD.id = '{topic.id}' AND NEW.stale_reason = 'reason A'
        BEGIN
          SELECT pause_first_stale_update();
        END
        """
    )

    def trace_second_connection(statement):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            second_begin_attempted.set()

    repo_b.conn.set_trace_callback(trace_second_connection)

    def mark(repo, reason, completed=None):
        try:
            mark_topic_stale(repo, topic.id, reason)
        except BaseException as error:
            errors.append(error)
        finally:
            if completed is not None:
                completed.set()

    first_thread = threading.Thread(target=mark, args=(repo_a, "reason A"))
    second_thread = threading.Thread(
        target=mark,
        args=(repo_b, "reason B", second_completed),
    )
    first_thread.start()
    assert first_update_reached.wait(timeout=5)
    assert first_thread.is_alive()
    second_thread.start()
    assert second_begin_attempted.wait(timeout=5)
    assert second_thread.is_alive()
    assert not second_completed.is_set()

    release_first_update.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    repo_b.conn.set_trace_callback(None)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_completed.is_set()
    assert errors == []
    reasons = repo_a.get_topic(topic.id).stale_reason.splitlines()
    assert reasons == ["reason A", "reason B"]

    mark_topic_stale(repo_a, topic.id, "reason A")

    assert repo_a.get_topic(topic.id).stale_reason.splitlines() == reasons


def test_evaluate_topic_readiness_uses_constant_query_count_for_many_chapters(tmp_path):
    repo, topic, chapters = setup_topic(tmp_path, chapter_count=8)
    for chapter in chapters:
        set_review(repo, chapter.id)
    queries = []
    repo.conn.set_trace_callback(queries.append)

    readiness = evaluate_topic_readiness(repo, topic.id)

    repo.conn.set_trace_callback(None)
    selects = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
    assert readiness == TopicReadiness(READY, [])
    assert len(selects) == 2
