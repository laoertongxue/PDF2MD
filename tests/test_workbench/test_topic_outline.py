import json
import sqlite3

import pytest
from pydantic import ValidationError

from parsing_core.storage.schema import init_db
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_outline import (
    MAX_CHAPTERS_PER_TOPIC,
    MAX_DESCRIPTION_CHARS,
    MAX_REASON_CHARS,
    MAX_TITLE_CHARS,
    MAX_TOPICS,
    MAX_UNMAPPED_CHAPTERS,
    TopicOutlineResult,
    _build_prompt,
    generate_topic_outline,
)


class RecordingExecutor:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def run(self, task_key, prompt):
        self.calls.append((task_key, prompt))
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def prepared_course(tmp_path):
    conn = init_db(str(tmp_path / "topic-outline.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "MBA 核心课", str(tmp_path / "course"))
    sources = [
        repo.create_source(course.id, "main", "/tmp/z-book.pdf", "Z 教材"),
        repo.create_source(course.id, "main", "/tmp/a-book.pdf", "A 教材"),
    ]
    chapters = [
        repo.create_chapter(course.id, sources[0].id, 1, "后章", "/tmp/ORIGINAL_SENTINEL.md"),
        repo.create_chapter(course.id, sources[1].id, 1, "乙章", "/tmp/b.md"),
        repo.create_chapter(course.id, sources[1].id, 0, "甲章", "/tmp/a.md"),
        repo.create_chapter(course.id, sources[0].id, 0, "前章", "/tmp/z.md"),
    ]
    for chapter in chapters:
        repo.update_chapter_status(chapter.id, "CONFIRMED")
        repo.upsert_note_block(chapter.id, "later", "后记", f"note:{chapter.title}:2", 2)
        repo.upsert_note_block(chapter.id, "first", "先记", f"note:{chapter.title}:1", 1)
        repo.upsert_run(chapter.id, "review", "stub", "DONE", "", "", "ok", stale=False)
    return repo, course, chapters


def valid_payload(chapters):
    return {
        "topics": [
            {
                "title": "竞争优势",
                "description": "跨教材理解优势",
                "chapter_ids": [chapters[2].id, chapters[0].id],
                "reason": "概念互补",
            },
            {
                "title": "组织执行",
                "description": "从战略到执行",
                "chapter_ids": [chapters[0].id, chapters[3].id],
                "reason": "同章可支撑多个主题",
            },
        ],
        "unmapped_chapter_ids": [chapters[1].id],
    }


def test_models_require_strict_nonempty_pure_json():
    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate_json(
            '{"topics":[{"title":"   ","description":"","chapter_ids":["c"],'
            '"reason":"why"}],"unmapped_chapter_ids":[]}'
        )
    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate_json(
            '{"topics":[{"title":"T","description":"","chapter_ids":[],'
            '"reason":"why","extra":1}],"unmapped_chapter_ids":[]}'
        )
    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate_json("```json\n{}\n```")
    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate_json(
            '{"topics":[{"title":"T","description":"","chapter_ids":["c"],'
            '"reason":"why"}],"unmapped_chapter_ids":[],"score":NaN}'
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "   "),
        ("chapter_ids", ["   "]),
        ("unmapped_chapter_ids", ["   "]),
    ],
)
def test_models_reject_blank_trimmed_fields(field, value):
    payload = {
        "topics": [
            {
                "title": "主题",
                "description": "说明",
                "chapter_ids": ["chapter-1"],
                "reason": "原因",
            }
        ],
        "unmapped_chapter_ids": [],
    }
    if field == "unmapped_chapter_ids":
        payload[field] = value
    else:
        payload["topics"][0][field] = value

    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field",
    ["chapter_ids", "unmapped_chapter_ids"],
)
def test_duplicate_ids_are_detected_after_trimming(tmp_path, field):
    repo, course, chapters = prepared_course(tmp_path)
    payload = valid_payload(chapters)
    if field == "chapter_ids":
        payload["topics"][0][field] = [chapters[2].id, f" {chapters[2].id} "]
    else:
        payload[field] = [chapters[1].id, f" {chapters[1].id} "]

    with pytest.raises(ValueError, match="duplicate"):
        generate_topic_outline(
            repo,
            course.id,
            RecordingExecutor(json.dumps(payload, ensure_ascii=False)),
        )


@pytest.mark.parametrize(
    "equivalent_title",
    ["Cafe\u0301", "Ｃａｆé"],
)
def test_unicode_equivalent_topic_titles_are_rejected(tmp_path, equivalent_title):
    repo, course, chapters = prepared_course(tmp_path)
    payload = valid_payload(chapters)
    payload["topics"][0]["title"] = "Café"
    payload["topics"][1]["title"] = equivalent_title

    with pytest.raises(ValueError, match="unique"):
        generate_topic_outline(
            repo,
            course.id,
            RecordingExecutor(json.dumps(payload, ensure_ascii=False)),
        )


def test_unicode_text_is_normalized_before_return_and_storage(tmp_path):
    repo, course, chapters = prepared_course(tmp_path)
    payload = valid_payload(chapters)
    payload["topics"][0].update(
        title="  Ｃａｆｅ\u0301\t Strategy  ",
        description="  de\u0301scription  ",
        reason="  re\u0301ason  ",
    )

    result = generate_topic_outline(
        repo,
        course.id,
        RecordingExecutor(json.dumps(payload, ensure_ascii=False)),
    )

    assert result.topics[0].title == "Café Strategy"
    assert result.topics[0].description == "déscription"
    assert result.topics[0].reason == "réason"
    stored = repo.list_topics(course.id)[0]
    assert (stored.title, stored.description, stored.generation_reason) == (
        "Café Strategy",
        "déscription",
        "réason",
    )


@pytest.mark.parametrize(
    "field,limit",
    [
        ("title", MAX_TITLE_CHARS),
        ("description", MAX_DESCRIPTION_CHARS),
        ("reason", MAX_REASON_CHARS),
    ],
)
def test_topic_text_field_limits(field, limit):
    topic = {
        "title": "T",
        "description": "D",
        "chapter_ids": ["c"],
        "reason": "R",
    }
    topic[field] = "x" * (limit + 1)

    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate(
            {"topics": [topic], "unmapped_chapter_ids": []}
        )


@pytest.mark.parametrize(
    "field,size",
    [
        ("topics", MAX_TOPICS + 1),
        ("chapter_ids", MAX_CHAPTERS_PER_TOPIC + 1),
        ("unmapped_chapter_ids", MAX_UNMAPPED_CHAPTERS + 1),
    ],
)
def test_outline_structure_limits(field, size):
    topic = {"title": "T", "description": "D", "chapter_ids": ["c"], "reason": "R"}
    payload = {"topics": [topic], "unmapped_chapter_ids": []}
    if field == "topics":
        payload[field] = [{**topic, "title": f"T{i}"} for i in range(size)]
    elif field == "chapter_ids":
        topic[field] = [f"c{i}" for i in range(size)]
    else:
        payload[field] = [f"c{i}" for i in range(size)]

    with pytest.raises(ValidationError):
        TopicOutlineResult.model_validate(payload)


def test_prompt_limit_fails_before_executor_and_preserves_database(tmp_path, monkeypatch):
    repo, course, chapters = prepared_course(tmp_path)
    old = repo.create_topic(course.id, 0, "旧草稿", "保留")
    executor = RecordingExecutor(json.dumps(valid_payload(chapters)))
    monkeypatch.setattr("parsing_core.workbench.topic_outline.MAX_PROMPT_CHARS", 100)

    with pytest.raises(ValueError, match="prompt exceeds"):
        generate_topic_outline(repo, course.id, executor)
    assert executor.calls == []
    assert repo.list_topics(course.id) == [old]


def test_prompt_snapshot_uses_one_batch_select(tmp_path):
    repo, course, _ = prepared_course(tmp_path)
    statements = []
    repo.conn.set_trace_callback(statements.append)

    _build_prompt(repo, course.id)

    repo.conn.set_trace_callback(None)
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 1


def test_optional_executor_prompt_validation_runs_before_client(tmp_path):
    repo, course, chapters = prepared_course(tmp_path)
    calls = []

    class ValidatingExecutor(RecordingExecutor):
        def validate_prompt(self, task_key, prompt):
            calls.append(("validate", task_key))

        def run(self, task_key, prompt):
            calls.append(("run", task_key))
            return super().run(task_key, prompt)

    generate_topic_outline(
        repo,
        course.id,
        ValidatingExecutor(json.dumps(valid_payload(chapters), ensure_ascii=False)),
    )
    assert calls == [("validate", "topic_outline"), ("run", "topic_outline")]


def test_deepseek_rejects_200k_chinese_prompt_before_client(tmp_path):
    from parsing_core.workbench.deepseek import DeepSeekError, DeepSeekExecutor

    repo, course, chapters = prepared_course(tmp_path)
    repo.upsert_note_block(chapters[0].id, "first", "先记", "汉" * 200_000, 1)
    calls = []

    class Client:
        model = "deepseek-chat"

        def complete(self, prompt, *, max_tokens):
            calls.append((prompt, max_tokens))
            return json.dumps(valid_payload(chapters), ensure_ascii=False)

    with pytest.raises(DeepSeekError, match="token budget"):
        generate_topic_outline(repo, course.id, DeepSeekExecutor(Client()))
    assert calls == []


def test_response_byte_limit_fails_before_json_parse_and_preserves_database(
    tmp_path, monkeypatch
):
    repo, course, _ = prepared_course(tmp_path)
    old = repo.create_topic(course.id, 0, "旧草稿", "保留")
    monkeypatch.setattr("parsing_core.workbench.topic_outline.MAX_RESPONSE_BYTES", 10)

    with pytest.raises(ValueError, match="response exceeds"):
        generate_topic_outline(repo, course.id, RecordingExecutor("not-json-over-limit"))
    assert repo.list_topics(course.id) == [old]


class MutatingExecutor(RecordingExecutor):
    def __init__(self, output, mutate):
        super().__init__(output)
        self.mutate = mutate

    def run(self, task_key, prompt):
        self.mutate()
        return super().run(task_key, prompt)


@pytest.mark.parametrize(
    "dependency",
    ["chapter", "review", "note-add", "note-modify", "note-delete", "source"],
)
def test_dependency_change_during_executor_rejects_stale_snapshot(
    tmp_path, dependency
):
    repo, course, chapters = prepared_course(tmp_path)
    old = repo.create_topic(course.id, 0, "旧草稿", "保留")
    chapter = chapters[0]

    def mutate():
        if dependency == "chapter":
            repo.update_chapter_status(chapter.id, "DRAFT")
        elif dependency == "review":
            repo.upsert_run(chapter.id, "review", "stub", "FAILED", "", "", "bad", stale=True)
        elif dependency == "note-add":
            repo.upsert_note_block(chapter.id, "new", "新增", "new body", 9)
        elif dependency == "note-modify":
            repo.upsert_note_block(chapter.id, "first", "先记", "changed body", 1)
        elif dependency == "note-delete":
            repo.conn.execute(
                "DELETE FROM wb_note_blocks WHERE chapter_id = ? AND kind = 'first'",
                (chapter.id,),
            )
            repo.conn.commit()
        else:
            repo.conn.execute(
                "UPDATE wb_sources SET title = ?, updated_at = updated_at + 1 WHERE id = ?",
                ("改名教材", chapter.source_id),
            )
            repo.conn.commit()

    executor = MutatingExecutor(
        json.dumps(valid_payload(chapters), ensure_ascii=False),
        mutate,
    )
    with pytest.raises(ValueError, match="snapshot changed"):
        generate_topic_outline(repo, course.id, executor)
    assert repo.list_topics(course.id) == [old]


def test_concurrent_connection_change_is_detected_before_atomic_replace(tmp_path):
    path = tmp_path / "concurrent-outline.db"
    first_conn = sqlite3.connect(path, timeout=2)
    apply_workbench_schema(first_conn)
    repo = WorkbenchRepository(first_conn)
    course = repo.create_course("战略", "课程", str(tmp_path))
    source = repo.create_source(course.id, "main", "/tmp/a.pdf", "教材")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", "/tmp/a.md")
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    repo.upsert_note_block(chapter.id, "summary", "概要", "old", 0)
    repo.upsert_run(chapter.id, "review", "stub", "DONE", "", "", "ok", stale=False)
    old = repo.create_topic(course.id, 0, "旧草稿", "保留")
    second_conn = sqlite3.connect(path, timeout=2)
    second = WorkbenchRepository(second_conn)
    payload = {
        "topics": [
            {"title": "主题", "description": "说明", "chapter_ids": [chapter.id], "reason": "原因"}
        ],
        "unmapped_chapter_ids": [],
    }
    executor = MutatingExecutor(
        json.dumps(payload, ensure_ascii=False),
        lambda: second.upsert_note_block(chapter.id, "summary", "概要", "changed", 0),
    )

    with pytest.raises(ValueError, match="snapshot changed"):
        generate_topic_outline(repo, course.id, executor)
    assert repo.list_topics(course.id) == [old]
    first_conn.close()
    second_conn.close()


def test_generate_outline_uses_only_eligible_chapter_notes_in_stable_order(tmp_path):
    repo, course, chapters = prepared_course(tmp_path)
    draft = repo.create_chapter(
        course.id,
        repo.list_sources(course.id)[0].id,
        9,
        "未确认草稿",
        "/tmp/DRAFT_ORIGINAL_SENTINEL.md",
    )
    repo.upsert_note_block(draft.id, "summary", "草稿", "DRAFT_NOTE_SENTINEL", 0)
    payload = valid_payload(chapters)
    payload["topics"][0].update(
        title=" 竞争优势 ",
        description=" 跨教材理解优势 ",
        reason=" 概念互补 ",
        chapter_ids=[f" {chapters[2].id} ", chapters[0].id],
    )
    payload["unmapped_chapter_ids"] = [f" {chapters[1].id} "]
    executor = RecordingExecutor(json.dumps(payload, ensure_ascii=False))

    result = generate_topic_outline(repo, course.id, executor)

    assert result.topics[0].chapter_ids == [chapters[2].id, chapters[0].id]
    assert result.topics[0].title == "竞争优势"
    assert result.topics[0].description == "跨教材理解优势"
    assert result.topics[0].reason == "概念互补"
    assert result.unmapped_chapter_ids == [chapters[1].id]
    assert executor.calls[0][0] == "topic_outline"
    prompt = executor.calls[0][1]
    assert "战略管理" in prompt and "MBA 核心课" in prompt
    assert "A 教材" in prompt and "Z 教材" in prompt
    assert "ORIGINAL_SENTINEL" not in prompt
    assert "DRAFT_NOTE_SENTINEL" not in prompt
    assert prompt.index("A 教材") < prompt.index("Z 教材")
    assert prompt.index("甲章") < prompt.index("乙章") < prompt.index("前章") < prompt.index("后章")
    assert prompt.index("note:甲章:1") < prompt.index("note:甲章:2")
    topics = repo.list_topics(course.id)
    assert [(topic.seq, topic.title, topic.generation_reason) for topic in topics] == [
        (0, "竞争优势", "概念互补"),
        (1, "组织执行", "同章可支撑多个主题"),
    ]
    assert {chapter.id for chapter in repo.list_topic_chapters(topics[0].id)} == {
        chapters[2].id,
        chapters[0].id,
    }


@pytest.mark.parametrize("status,stale", [("FAILED", False), ("DONE", True)])
def test_generate_rejects_blocking_review_runs_before_executor(tmp_path, status, stale):
    repo, course, chapters = prepared_course(tmp_path)
    blocked = chapters[1]
    repo.upsert_run(blocked.id, "review", "stub", status, "", "", "bad", stale=stale)
    executor = RecordingExecutor("{}")

    with pytest.raises(ValueError, match=blocked.id):
        generate_topic_outline(repo, course.id, executor)
    assert executor.calls == []


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p, c: p["topics"][0].update(chapter_ids=[c[0].id, c[0].id]), "duplicate"),
        (lambda p, c: p["topics"][1].update(title=" 竞争优势 "), "unique"),
        (lambda p, c: p["topics"][0].update(chapter_ids=["unknown"]), "input"),
        (lambda p, c: p.update(unmapped_chapter_ids=[c[1].id, c[1].id]), "duplicate"),
        (lambda p, c: p.update(unmapped_chapter_ids=[c[0].id, c[1].id]), "overlap"),
        (lambda p, c: p.update(unmapped_chapter_ids=[]), "cover"),
    ],
)
def test_invalid_output_leaves_database_unchanged(tmp_path, mutate, match):
    repo, course, chapters = prepared_course(tmp_path)
    old = repo.create_topic(course.id, 0, "旧草稿", "保留")
    payload = valid_payload(chapters)
    mutate(payload, chapters)
    executor = RecordingExecutor(json.dumps(payload, ensure_ascii=False))

    with pytest.raises(ValueError, match=match):
        generate_topic_outline(repo, course.id, executor)
    assert repo.list_topics(course.id) == [old]


@pytest.mark.parametrize("output", [RuntimeError("offline"), "not json", "{}"])
def test_executor_and_json_failures_leave_database_unchanged(tmp_path, output):
    repo, course, _ = prepared_course(tmp_path)
    old = repo.create_topic(course.id, 0, "旧草稿", "保留")

    with pytest.raises((RuntimeError, ValidationError)):
        generate_topic_outline(repo, course.id, RecordingExecutor(output))
    assert repo.list_topics(course.id) == [old]


def test_generate_rejects_missing_course(tmp_path):
    repo, _, _ = prepared_course(tmp_path)
    with pytest.raises(ValueError, match="course not found"):
        generate_topic_outline(repo, "missing", RecordingExecutor("{}"))
