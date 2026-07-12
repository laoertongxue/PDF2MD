import gc
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from parsing_core.storage import connection_lock
from parsing_core.storage.schema import init_db
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


def repo(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    return WorkbenchRepository(conn)


def test_create_course_source_and_chapter(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "MBA 课程", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    chapter = r.create_chapter(course.id, source.id, 0, "第一章 战略是什么", "/tmp/ch1.md")

    assert r.get_course(course.id).title == "战略管理"
    assert r.list_sources(course.id)[0].title == "战略教材"
    assert r.list_chapters(source.id)[0].title == "第一章 战略是什么"
    assert chapter.status == "DRAFT"


def test_chapter_drafts_can_be_replaced_and_confirmed_as_atomic_snapshot(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    first = r.create_chapter(course.id, source.id, 0, "第一章", "/tmp/1.md", 0, 20)
    second = r.create_chapter(course.id, source.id, 1, "第二章", "/tmp/2.md", 20, 40)
    fingerprint = r.chapter_draft_snapshot(source.id)[1]

    drafts = r.replace_chapter_drafts(
        source.id,
        [
            {"id": second.id, "title": "第二章（改）", "start": 0, "end": 10},
            {"id": first.id, "title": "第一章", "start": 10, "end": 40},
        ],
        expected_fingerprint=fingerprint,
    )
    confirmed = r.confirm_chapter_drafts(source.id, r.chapter_draft_snapshot(source.id)[1])

    assert [(item.seq, item.title, item.source_start, item.source_end) for item in drafts] == [
        (0, "第二章（改）", 0, 10),
        (1, "第一章", 10, 40),
    ]
    assert all(item.status == "CONFIRMED" and item.confirmed_snapshot_json for item in confirmed)
    with pytest.raises(ValueError, match="confirmed"):
        r.replace_chapter_drafts(
            source.id, [], expected_fingerprint=r.chapter_draft_snapshot(source.id)[1]
        )


def test_attachment_is_related_and_changes_chapter_input_fingerprint(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    chapter = r.create_chapter(course.id, source.id, 0, "第一章", "/tmp/1.md")
    before = r.chapter_input_snapshot(chapter.id)[1]
    attachment = r.create_attachment(
        course.id,
        source.id,
        chapter.id,
        "/tmp/case.pptx",
        "案例",
        "pptx",
        "第 1 页\n案例内容",
        "abc123",
        [{"citation_id": "att:x:p1", "page": 1, "paragraph": 1, "text": "案例内容"}],
    )
    assert r.list_attachments(chapter.id) == [attachment]
    assert r.chapter_input_snapshot(chapter.id)[1] != before


def test_cards_can_be_edited_and_favorited(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("营销管理", "", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "营销教材")
    chapter = r.create_chapter(course.id, source.id, 0, "第一章", "/tmp/ch1.md")
    card = r.create_card(course.id, chapter.id, "viewpoint", "定位不是口号", "定位是选择。")

    r.update_card(card.id, title="定位是取舍", body="定位不是更多，而是更少。")
    r.set_card_favorite(card.id, True)

    cards = r.list_cards(course.id)
    assert cards[0].title == "定位是取舍"
    assert cards[0].favorite is True


def test_course_cards_support_metadata_and_compare_and_swap(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("营销管理", "", str(tmp_path / "out"))
    source = r.create_source(course.id, "main", "/tmp/book.pdf", "营销教材")
    chapter = r.create_chapter(course.id, source.id, 0, "第一章", "/tmp/ch1.md")
    card = r.create_card(course.id, chapter.id, "viewpoint", "定位", "定位是选择。")

    updated = r.update_course_card(
        card.id,
        title="定位与取舍",
        content="战略意味着明确放弃。",
        tags=["战略", "定位"],
        status="ARCHIVED",
        expected_updated_at=card.updated_at,
    )

    assert updated["tags"] == ["战略", "定位"]
    assert updated["status"] == "ARCHIVED"
    assert updated["updated_at"] > card.updated_at
    with pytest.raises(ValueError, match="card changed"):
        r.set_course_card_favorite(card.id, True, card.updated_at)


def test_topic_course_cards_persist_edits_and_favorites(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")
    card = r.replace_topic_cards(
        topic.id,
        [
            {
                "card_type": "viewpoint",
                "title": "壁垒",
                "content": "优势需要持续。",
                "source_refs_json": "[]",
            }
        ],
    )[0]
    listed = next(item for item in r.list_course_cards(course.id) if item["id"] == card.id)

    edited = r.update_course_card(
        card.id,
        title="持续壁垒",
        content="优势需要难以复制。",
        tags=["战略"],
        status="ACTIVE",
        expected_updated_at=listed["updated_at"],
    )
    favorited = r.set_course_card_favorite(card.id, True, edited["updated_at"])

    assert favorited["origin_type"] == "topic"
    assert favorited["favorite"] is True
    assert r.list_topic_cards(topic.id)[0].title == "持续壁垒"


def test_chapter_source_must_belong_to_course(tmp_path):
    r = repo(tmp_path)
    course_a = r.create_course("战略管理", "", str(tmp_path / "a"))
    course_b = r.create_course("营销管理", "", str(tmp_path / "b"))
    source_a = r.create_source(course_a.id, "main", "/tmp/a.pdf", "战略教材")

    with pytest.raises(ValueError):
        r.create_chapter(course_b.id, source_a.id, 0, "第一章", "/tmp/ch1.md")


def test_card_chapter_must_belong_to_course(tmp_path):
    r = repo(tmp_path)
    course_a = r.create_course("战略管理", "", str(tmp_path / "a"))
    course_b = r.create_course("营销管理", "", str(tmp_path / "b"))
    source_a = r.create_source(course_a.id, "main", "/tmp/a.pdf", "战略教材")
    source_b = r.create_source(course_b.id, "main", "/tmp/b.pdf", "营销教材")
    r.create_chapter(course_a.id, source_a.id, 0, "第一章", "/tmp/a-ch1.md")
    chapter_b = r.create_chapter(course_b.id, source_b.id, 0, "第一章", "/tmp/b-ch1.md")

    with pytest.raises(ValueError):
        r.create_card(course_a.id, chapter_b.id, "viewpoint", "定位", "定位是选择。")


def make_course_with_chapters(r, tmp_path, title="战略管理"):
    course = r.create_course(title, "", str(tmp_path / title))
    source_a = r.create_source(course.id, "main", f"/tmp/{title}-a.pdf", "主教材")
    source_b = r.create_source(course.id, "attachment", f"/tmp/{title}-b.pdf", "补充材料")
    chapter_a2 = r.create_chapter(course.id, source_a.id, 2, "第三章", "/tmp/a2.md")
    chapter_a0 = r.create_chapter(course.id, source_a.id, 0, "第一章", "/tmp/a0.md")
    chapter_b0 = r.create_chapter(course.id, source_b.id, 0, "案例", "/tmp/b0.md")
    return course, (chapter_a2, chapter_a0, chapter_b0)


def outline_fingerprint(r, course_id):
    return r.course_topic_outline_snapshot(course_id)[1]


def test_topic_crud_and_defaults(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))

    topic = r.create_topic(course.id, 0, "竞争优势", "理解优势来源")

    assert topic.status == "DRAFT"
    assert topic.confirmed is False
    assert topic.stale_reason == ""
    assert topic.generation_reason == ""
    assert r.get_topic(topic.id) == topic
    assert r.list_topics(course.id) == [topic]

    updated = r.update_topic(
        topic.id,
        title="竞争优势与壁垒",
        description="连接资源与行业结构",
        status="READY",
        confirmed=True,
        stale_reason="source changed",
    )

    assert updated.title == "竞争优势与壁垒"
    assert updated.description == "连接资源与行业结构"
    assert updated.status == "READY"
    assert updated.confirmed is True
    assert updated.stale_reason == "source changed"


def test_list_course_chapters_includes_source_and_excludes_drafts(tmp_path):
    r = repo(tmp_path)
    course, chapters = make_course_with_chapters(r, tmp_path)
    for chapter in chapters[:2]:
        r.update_chapter_status(chapter.id, "CONFIRMED")

    rows = r.list_course_chapters(course.id)

    assert [(row.source_title, row.chapter.title) for row in rows] == [
        ("主教材", "第一章"),
        ("主教材", "第三章"),
    ]


def test_replace_course_topic_drafts_replaces_only_safe_drafts(tmp_path):
    r = repo(tmp_path)
    course, chapters = make_course_with_chapters(r, tmp_path)
    old = r.create_topic(course.id, 0, "旧草稿", "旧说明")
    r.replace_topic_chapters(old.id, [chapters[0].id])

    topics = r.replace_course_topic_drafts(
        course.id,
        [
            {
                "title": "新主题一",
                "description": "说明一",
                "reason": "原因一",
                "chapter_ids": [chapters[1].id, chapters[0].id],
            },
            {
                "title": "新主题二",
                "description": "说明二",
                "reason": "原因二",
                "chapter_ids": [chapters[0].id],
            },
        ],
        expected_fingerprint=outline_fingerprint(r, course.id),
    )

    assert r.get_topic(old.id) is None
    assert [(t.seq, t.title, t.generation_reason) for t in topics] == [
        (0, "新主题一", "原因一"),
        (1, "新主题二", "原因二"),
    ]
    assert [c.id for c in r.list_topic_chapters(topics[0].id)] == [
        chapters[1].id,
        chapters[0].id,
    ]


@pytest.mark.parametrize(
    "published_kind",
    ["confirmed", "non-draft", "note", "card", "completed-run"],
)
def test_replace_course_topic_drafts_protects_manual_and_published_topics(tmp_path, published_kind):
    r = repo(tmp_path)
    course, chapters = make_course_with_chapters(r, tmp_path)
    topic = r.create_topic(course.id, 0, "受保护主题", "")
    if published_kind == "confirmed":
        r.update_topic(topic.id, confirmed=True)
    elif published_kind == "non-draft":
        r.update_topic(topic.id, status="RUNNING")
    elif published_kind == "note":
        r.replace_topic_note_blocks(topic.id, {"summary": "published"})
    elif published_kind == "card":
        r.replace_topic_cards(
            topic.id,
            [{"card_type": "x", "title": "x", "content": "x", "source_refs_json": []}],
        )
    else:
        run = r.create_topic_run(topic.id, "draft", "fingerprint")
        r.finish_topic_run(run.id, "COMPLETED", output="published")

    with pytest.raises(ValueError, match="protected topic"):
        r.replace_course_topic_drafts(
            course.id,
            [{"title": "new", "description": "", "reason": "r", "chapter_ids": [chapters[0].id]}],
            expected_fingerprint=outline_fingerprint(r, course.id),
        )
    assert r.list_topics(course.id) == [r.get_topic(topic.id)]


def test_replace_course_topic_drafts_rolls_back_all_inserts(tmp_path):
    r = repo(tmp_path)
    course, chapters = make_course_with_chapters(r, tmp_path)
    old = r.create_topic(course.id, 0, "旧草稿", "")
    r.conn.execute(
        """
        CREATE TRIGGER fail_second_generated_topic
        BEFORE INSERT ON wb_topics WHEN NEW.title = '失败主题'
        BEGIN SELECT RAISE(ABORT, 'forced failure'); END
        """
    )
    r.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        r.replace_course_topic_drafts(
            course.id,
            [
                {
                    "title": "先插入",
                    "description": "",
                    "reason": "r",
                    "chapter_ids": [chapters[0].id],
                },
                {
                    "title": "失败主题",
                    "description": "",
                    "reason": "r",
                    "chapter_ids": [chapters[1].id],
                },
            ],
            expected_fingerprint=outline_fingerprint(r, course.id),
        )
    assert r.list_topics(course.id) == [old]


def test_replace_course_topic_drafts_rechecks_protection_after_begin_immediate(tmp_path):
    path = tmp_path / "concurrent.db"
    first_conn = sqlite3.connect(path, check_same_thread=False, timeout=2)
    second_conn = sqlite3.connect(path, check_same_thread=False, timeout=2)
    apply_workbench_schema(first_conn)
    apply_workbench_schema(second_conn)
    first = WorkbenchRepository(first_conn)
    second = WorkbenchRepository(second_conn)
    course = first.create_course("战略", "", str(tmp_path))
    source = first.create_source(course.id, "main", "/tmp/a", "教材")
    chapter = first.create_chapter(course.id, source.id, 0, "章", "/tmp/a.md")
    topic = first.create_topic(course.id, 0, "人工主题", "")

    second.update_topic(topic.id, confirmed=True)
    with pytest.raises(ValueError, match="protected topic"):
        first.replace_course_topic_drafts(
            course.id,
            [{"title": "AI 主题", "description": "", "reason": "r", "chapter_ids": [chapter.id]}],
            expected_fingerprint=outline_fingerprint(first, course.id),
        )
    assert first.get_topic(topic.id).confirmed is True
    first_conn.close()
    second_conn.close()


def test_reorder_topics_swaps_unique_sequences_and_rejects_invalid_lists_atomically(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topics = [r.create_topic(course.id, seq, f"主题 {seq}", "") for seq in range(3)]

    reordered = r.reorder_topics(course.id, [topics[2].id, topics[0].id, topics[1].id])
    assert [(topic.id, topic.seq) for topic in reordered] == [
        (topics[2].id, 0),
        (topics[0].id, 1),
        (topics[1].id, 2),
    ]

    before = [(topic.id, topic.seq) for topic in r.list_topics(course.id)]
    with pytest.raises(ValueError):
        r.reorder_topics(course.id, [topics[2].id, topics[2].id, topics[1].id])
    assert [(topic.id, topic.seq) for topic in r.list_topics(course.id)] == before

    other = r.create_course("营销管理", "", str(tmp_path / "other"))
    other_topic = r.create_topic(other.id, 0, "定位", "")
    with pytest.raises(ValueError):
        r.reorder_topics(course.id, [topics[2].id, topics[0].id, other_topic.id])
    assert [(topic.id, topic.seq) for topic in r.list_topics(course.id)] == before


def test_reorder_topics_handles_minimum_sqlite_integer_sequence(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    first = r.create_topic(course.id, -(2**63), "主题一", "")
    second = r.create_topic(course.id, 0, "主题二", "")

    reordered = r.reorder_topics(course.id, [second.id, first.id])

    assert [(topic.id, topic.seq) for topic in reordered] == [
        (second.id, 0),
        (first.id, 1),
    ]


def test_reorder_topics_uses_only_integer_temporary_sequences(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    first = r.create_topic(course.id, 0, "主题一", "")
    second = r.create_topic(course.id, 1, "主题二", "")
    r.conn.execute(
        """
        CREATE TRIGGER reject_non_integer_topic_seq
        BEFORE UPDATE OF seq ON wb_topics
        WHEN typeof(NEW.seq) != 'integer'
        BEGIN
          SELECT RAISE(ABORT, 'topic seq must stay integer');
        END
        """
    )
    r.conn.commit()

    reordered = r.reorder_topics(course.id, [second.id, first.id])

    assert [(topic.id, topic.seq) for topic in reordered] == [
        (second.id, 0),
        (first.id, 1),
    ]
    types = r.conn.execute(
        "SELECT DISTINCT typeof(seq) FROM wb_topics WHERE course_id = ?",
        (course.id,),
    ).fetchall()
    assert [row[0] for row in types] == ["integer"]


def test_repositories_share_locks_by_connection_identity(tmp_path):
    first_conn = sqlite3.connect(tmp_path / "first.db", check_same_thread=False)
    second_conn = sqlite3.connect(tmp_path / "second.db", check_same_thread=False)
    apply_workbench_schema(first_conn)
    apply_workbench_schema(second_conn)

    first = WorkbenchRepository(first_conn)
    shared = WorkbenchRepository(first_conn)
    separate = WorkbenchRepository(second_conn)

    assert first._connection_lock is shared._connection_lock
    assert first._connection_lock is not separate._connection_lock

    first_conn.close()
    second_conn.close()


def test_connection_lock_registry_releases_entries_with_repository_lifetime(tmp_path):
    conn = sqlite3.connect(tmp_path / "lifetime.db", check_same_thread=False)
    apply_workbench_schema(conn)
    first = WorkbenchRepository(conn)
    second = WorkbenchRepository(conn)
    connection_key = id(conn)

    assert connection_lock._connection_locks[connection_key].users == 2
    assert connection_lock._connection_locks[connection_key].connection is conn

    del first
    gc.collect()
    assert connection_lock._connection_locks[connection_key].users == 1

    del second
    gc.collect()
    assert connection_key not in connection_lock._connection_locks
    conn.close()


@pytest.mark.parametrize("a_fails", [False, True])
def test_shared_connection_serializes_topic_transactions_and_releases_after_failure(
    tmp_path,
    a_fails,
):
    conn = sqlite3.connect(tmp_path / "shared.db", check_same_thread=False)
    apply_workbench_schema(conn)
    first = WorkbenchRepository(conn)
    second = WorkbenchRepository(conn)
    course = first.create_course("战略管理", "", str(tmp_path / "out"))
    topic = first.create_topic(course.id, 0, "竞争优势", "")
    original = first.replace_topic_note_blocks(topic.id, {"old": "旧内容"})
    if a_fails:
        conn.execute(
            """
            CREATE TRIGGER fail_thread_a_note_insert
            BEFORE INSERT ON wb_topic_note_blocks
            WHEN NEW.kind = 'thread-a'
            BEGIN
              SELECT RAISE(ABORT, 'thread A rollback');
            END
            """
        )
        conn.commit()

    a_in_transaction = threading.Event()
    allow_a_to_finish = threading.Event()
    b_attempting_write = threading.Event()
    b_finished = threading.Event()
    a_errors = []
    b_errors = []
    original_atomic = first._atomic

    @contextmanager
    def pausing_atomic(*, immediate=False):
        with original_atomic(immediate=immediate):
            a_in_transaction.set()
            assert allow_a_to_finish.wait(timeout=2)
            yield

    first._atomic = pausing_atomic

    def write_from_a():
        try:
            first.replace_topic_note_blocks(topic.id, {"thread-a": "A"})
        except sqlite3.IntegrityError as exc:
            a_errors.append(str(exc))

    def write_from_b():
        b_attempting_write.set()
        try:
            second.replace_topic_cards(
                topic.id,
                [
                    {
                        "card_type": "insight",
                        "title": "线程 B",
                        "content": "必须持久化",
                        "source_refs_json": ["thread:b"],
                    }
                ],
            )
        except BaseException as exc:
            b_errors.append(exc)
        finally:
            b_finished.set()

    thread_a = threading.Thread(target=write_from_a)
    thread_b = threading.Thread(target=write_from_b)
    thread_a.start()
    assert a_in_transaction.wait(timeout=2)
    thread_b.start()
    assert b_attempting_write.wait(timeout=2)
    b_was_blocked = not b_finished.wait(timeout=0.1)

    allow_a_to_finish.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert b_was_blocked
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert b_errors == []
    assert [card.title for card in second.list_topic_cards(topic.id)] == ["线程 B"]
    if a_fails:
        assert a_errors == ["thread A rollback"]
        assert first.list_topic_note_blocks(topic.id) == original
    else:
        assert a_errors == []
        notes = first.list_topic_note_blocks(topic.id)
        assert [(block.kind, block.content) for block in notes] == [("thread-a", "A")]

    conn.close()


@pytest.mark.parametrize(
    "operation",
    ["reorder", "replace_chapters", "replace_notes", "replace_cards"],
)
def test_batch_methods_do_not_commit_outer_transaction(tmp_path, operation):
    r = repo(tmp_path)
    course, (chapter, _, _) = make_course_with_chapters(r, tmp_path)
    first = r.create_topic(course.id, 0, "主题一", "")
    second = r.create_topic(course.id, 1, "主题二", "")
    operations = {
        "reorder": lambda: r.reorder_topics(course.id, [second.id, first.id]),
        "replace_chapters": lambda: r.replace_topic_chapters(first.id, [chapter.id]),
        "replace_notes": lambda: r.replace_topic_note_blocks(first.id, {"summary": "摘要"}),
        "replace_cards": lambda: r.replace_topic_cards(
            first.id,
            [
                {
                    "card_type": "insight",
                    "title": "壁垒",
                    "content": "内容",
                    "source_refs_json": ["chapter:1"],
                }
            ],
        ),
    }
    r.conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )

    operations[operation]()
    r.conn.rollback()

    assert r.get_course(course.id).description == ""


def test_replace_topic_chapters_deduplicates_and_sorts_stably(tmp_path):
    r = repo(tmp_path)
    course, (chapter_a2, chapter_a0, chapter_b0) = make_course_with_chapters(r, tmp_path)
    topic = r.create_topic(course.id, 0, "战略分析", "")

    chapters = r.replace_topic_chapters(
        topic.id,
        [chapter_a2.id, chapter_b0.id, chapter_a0.id, chapter_a2.id],
    )

    expected = sorted(
        (chapter_a2, chapter_a0, chapter_b0),
        key=lambda chapter: (chapter.source_id, chapter.seq, chapter.id),
    )
    assert [chapter.id for chapter in chapters] == [chapter.id for chapter in expected]
    assert [chapter.id for chapter in r.list_topic_chapters(topic.id)] == [
        chapter.id for chapter in expected
    ]


def test_cross_course_topic_chapter_replacement_preserves_old_mapping(tmp_path):
    r = repo(tmp_path)
    course, (chapter_a2, chapter_a0, _) = make_course_with_chapters(r, tmp_path)
    other_course, (other_chapter, _, _) = make_course_with_chapters(r, tmp_path, "营销管理")
    assert other_course.id != course.id
    topic = r.create_topic(course.id, 0, "战略分析", "")
    r.replace_topic_chapters(topic.id, [chapter_a0.id, chapter_a2.id])

    with pytest.raises(ValueError):
        r.replace_topic_chapters(topic.id, [chapter_a0.id, other_chapter.id])

    assert [chapter.id for chapter in r.list_topic_chapters(topic.id)] == [
        chapter_a0.id,
        chapter_a2.id,
    ]


def test_unknown_topic_chapter_replacement_preserves_old_mapping(tmp_path):
    r = repo(tmp_path)
    course, (_, chapter, _) = make_course_with_chapters(r, tmp_path)
    topic = r.create_topic(course.id, 0, "战略分析", "")
    r.replace_topic_chapters(topic.id, [chapter.id])

    with pytest.raises(ValueError, match="all chapters must exist"):
        r.replace_topic_chapters(topic.id, ["missing-chapter"])

    assert [mapped.id for mapped in r.list_topic_chapters(topic.id)] == [chapter.id]


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_topic_chapter_validation_holds_write_lock_until_mapping_is_replaced(
    tmp_path,
    outer_transaction,
):
    db_path = tmp_path / "concurrent.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    r = WorkbenchRepository(conn)
    course, (chapter, _, _) = make_course_with_chapters(r, tmp_path)
    other = r.create_course("营销管理", "", str(tmp_path / "other"))
    topic = r.create_topic(course.id, 0, "战略分析", "")
    competing = sqlite3.connect(str(db_path), timeout=0)
    competing.execute("PRAGMA foreign_keys = ON")
    events = []

    def compete_during_chapter_validation(statement):
        if "SELECT * FROM wb_chapters WHERE id IN" not in statement or events:
            return
        try:
            competing.execute(
                "UPDATE wb_chapters SET course_id = ? WHERE id = ?",
                (other.id, chapter.id),
            )
            competing.commit()
            events.append("moved")
        except sqlite3.OperationalError as exc:
            competing.rollback()
            events.append(str(exc))

    if outer_transaction:
        conn.execute("BEGIN")
    conn.set_trace_callback(compete_during_chapter_validation)
    try:
        r.replace_topic_chapters(topic.id, [chapter.id])
    finally:
        conn.set_trace_callback(None)
        competing.close()

    assert events == ["database is locked"]
    mapped_course = conn.execute(
        """
        SELECT c.course_id
        FROM wb_topic_chapters tc
        JOIN wb_chapters c ON c.id = tc.chapter_id
        WHERE tc.topic_id = ?
        """,
        (topic.id,),
    ).fetchone()[0]
    assert mapped_course == course.id
    if outer_transaction:
        conn.rollback()


def test_replace_topic_note_blocks_upserts_and_removes_missing_kinds(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")

    first = r.replace_topic_note_blocks(topic.id, {"summary": "摘要", "diagram": "图一"})
    summary_id = next(block.id for block in first if block.kind == "summary")
    replaced = r.replace_topic_note_blocks(topic.id, {"summary": "新摘要", "application": "应用"})

    assert [(block.kind, block.content) for block in replaced] == [
        ("application", "应用"),
        ("summary", "新摘要"),
    ]
    assert next(block.id for block in replaced if block.kind == "summary") == summary_id


def test_replace_topic_note_blocks_rolls_back_on_database_failure(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")
    original = r.replace_topic_note_blocks(
        topic.id,
        {"diagram": "旧图", "summary": "旧摘要"},
    )
    r.conn.execute(
        """
        CREATE TRIGGER fail_topic_note_insert
        BEFORE INSERT ON wb_topic_note_blocks
        WHEN NEW.kind = 'boom'
        BEGIN
          SELECT RAISE(ABORT, 'forced topic note failure');
        END
        """
    )
    r.conn.commit()
    r.conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced topic note failure"):
        r.replace_topic_note_blocks(
            topic.id,
            {"replacement": "新内容", "boom": "触发失败"},
        )

    assert r.list_topic_note_blocks(topic.id) == original
    assert r.get_course(course.id).description == "outer pending"
    r.conn.rollback()


def test_replace_topic_cards_is_atomic_and_stores_valid_json(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")

    cards = r.replace_topic_cards(
        topic.id,
        [
            {
                "card_type": "insight",
                "title": "壁垒",
                "content": "优势必须可持续。",
                "source_refs_json": ["chapter:c1", "quote:原文"],
            },
            {
                "card_type": "question",
                "title": "边界",
                "content": "何时失效？",
                "source_refs_json": '["chapter:c2"]',
            },
        ],
    )

    assert [card.title for card in cards] == ["壁垒", "边界"]
    assert json.loads(cards[0].source_refs_json) == ["chapter:c1", "quote:原文"]
    assert json.loads(cards[1].source_refs_json) == ["chapter:c2"]

    with pytest.raises((TypeError, ValueError)):
        r.replace_topic_cards(
            topic.id,
            [{"card_type": "bad", "title": "坏卡片", "content": "", "source_refs_json": "{"}],
        )
    assert [card.title for card in r.list_topic_cards(topic.id)] == ["壁垒", "边界"]

    with pytest.raises(ValueError):
        r.replace_topic_cards(
            topic.id,
            [
                {
                    "card_type": "bad",
                    "title": "坏卡片",
                    "content": "",
                    "source_refs_json": [float("nan")],
                }
            ],
        )
    assert [card.title for card in r.list_topic_cards(topic.id)] == ["壁垒", "边界"]


@pytest.mark.parametrize(
    "source_refs",
    [
        {},
        None,
        1,
        [{"chapter_id": "c1"}],
        ["chapter:c1", 1],
        '{"chapter_id": "c1"}',
        "null",
        "1",
        '[{"chapter_id": "c1"}]',
        '["chapter:c1", 1]',
    ],
)
def test_replace_topic_cards_rejects_non_string_array_source_refs(tmp_path, source_refs):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")

    with pytest.raises((TypeError, ValueError), match="array of strings"):
        r.replace_topic_cards(
            topic.id,
            [
                {
                    "card_type": "bad",
                    "title": "坏卡片",
                    "content": "",
                    "source_refs_json": source_refs,
                }
            ],
        )

    assert r.list_topic_cards(topic.id) == []


def test_topic_run_history_and_finish_status_semantics(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")

    first = r.create_topic_run(topic.id, "synthesis", "fingerprint-1")
    second = r.create_topic_run(topic.id, "synthesis", "fingerprint-2")
    completed = r.finish_topic_run(first.id, "COMPLETED", output="完成", error="ignored")
    failed = r.finish_topic_run(second.id, "FAILED", output="ignored", error="模型超时")

    assert completed.status == "COMPLETED"
    assert completed.output == "完成"
    assert completed.error == ""
    assert completed.finished_at is not None
    assert failed.status == "FAILED"
    assert failed.output == ""
    assert failed.error == "模型超时"
    assert failed.finished_at is not None
    assert [run.id for run in r.list_topic_runs(topic.id)] == [first.id, second.id]

    original_completed = r.list_topic_runs(topic.id)[0]
    with pytest.raises(ValueError, match="not RUNNING"):
        r.finish_topic_run(first.id, "FAILED", error="不得覆盖")
    assert r.list_topic_runs(topic.id)[0] == original_completed

    with pytest.raises(ValueError, match="topic run not found"):
        r.finish_topic_run("missing-run", "FAILED", error="不存在")

    with pytest.raises(ValueError, match="COMPLETED or FAILED"):
        r.finish_topic_run(first.id, "RUNNING")


@pytest.mark.parametrize("confirmed", [0, 1, "true"])
def test_update_topic_confirmed_requires_bool(tmp_path, confirmed):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "竞争优势", "")

    with pytest.raises(TypeError, match="confirmed must be bool"):
        r.update_topic(topic.id, confirmed=confirmed)

    assert r.get_topic(topic.id).confirmed is False


def test_delete_topic_cascades_all_topic_children(tmp_path):
    r = repo(tmp_path)
    course, (chapter, _, _) = make_course_with_chapters(r, tmp_path)
    topic = r.create_topic(course.id, 0, "竞争优势", "")
    r.replace_topic_chapters(topic.id, [chapter.id])
    r.replace_topic_note_blocks(topic.id, {"summary": "摘要"})
    r.replace_topic_cards(
        topic.id,
        [{"card_type": "insight", "title": "壁垒", "content": "内容", "source_refs_json": []}],
    )
    r.create_topic_run(topic.id, "synthesis", "fingerprint")

    r.delete_topic(topic.id)

    assert r.get_topic(topic.id) is None
    topic_child_tables = (
        "wb_topic_chapters",
        "wb_topic_note_blocks",
        "wb_topic_cards",
        "wb_topic_runs",
    )
    for table in topic_child_tables:
        assert r.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_topic_unique_sequence_is_enforced(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    r.create_topic(course.id, 0, "主题一", "")

    with pytest.raises(sqlite3.IntegrityError):
        r.create_topic(course.id, 0, "主题二", "")


def test_create_topic_failure_restores_transaction_and_next_write_commits(tmp_path):
    db_path = tmp_path / "workbench.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    r = WorkbenchRepository(conn)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    r.create_topic(course.id, 0, "主题一", "")
    observer = sqlite3.connect(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        r.create_topic(course.id, 0, "重复主题", "")

    assert conn.in_transaction is False
    created = r.create_topic(course.id, 1, "主题二", "")
    visible = observer.execute("SELECT title FROM wb_topics WHERE id = ?", (created.id,)).fetchone()
    assert visible[0] == "主题二"
    observer.close()


def test_create_topic_run_fk_failure_restores_transaction_and_next_write_commits(tmp_path):
    db_path = tmp_path / "workbench.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    r = WorkbenchRepository(conn)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "主题一", "")
    observer = sqlite3.connect(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        r.create_topic_run("missing-topic", "synthesis", "bad-fingerprint")

    assert conn.in_transaction is False
    created = r.create_topic_run(topic.id, "synthesis", "good-fingerprint")
    visible = observer.execute(
        "SELECT status FROM wb_topic_runs WHERE id = ?",
        (created.id,),
    ).fetchone()
    assert visible[0] == "RUNNING"
    observer.close()


def test_single_topic_writes_do_not_commit_outer_transaction(tmp_path):
    db_path = tmp_path / "workbench.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    r = WorkbenchRepository(conn)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "主题一", "")
    observer = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )

    nested_topic = r.create_topic(course.id, 1, "外层主题", "")
    nested_run = r.create_topic_run(topic.id, "synthesis", "outer-fingerprint")

    assert conn.in_transaction is True
    conn.rollback()
    assert (
        observer.execute(
            "SELECT COUNT(*) FROM wb_topics WHERE id = ?",
            (nested_topic.id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        observer.execute(
            "SELECT COUNT(*) FROM wb_topic_runs WHERE id = ?",
            (nested_run.id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        observer.execute(
            "SELECT description FROM wb_courses WHERE id = ?",
            (course.id,),
        ).fetchone()[0]
        == ""
    )
    observer.close()


def test_single_topic_failures_preserve_outer_transaction(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("战略管理", "", str(tmp_path / "out"))
    r.create_topic(course.id, 0, "主题一", "")
    r.conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        r.create_topic(course.id, 0, "重复主题", "")
    assert r.conn.in_transaction is True
    with pytest.raises(sqlite3.IntegrityError):
        r.create_topic_run("missing-topic", "synthesis", "bad-fingerprint")

    assert r.conn.in_transaction is True
    assert r.get_course(course.id).description == "outer pending"
    r.conn.rollback()


def test_topic_markdown_sync_claim_lease_and_owner_cas(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("课程", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "主题")
    r.set_topic_markdown_sync_state(topic.id, "PENDING")

    first = r.claim_topic_markdown_sync(topic.id, now=100, lease_ttl=600)
    assert first.status == "SYNCING" and first.lease_expires_at == 700
    with pytest.raises(ValueError, match="already syncing"):
        r.claim_topic_markdown_sync(topic.id, now=699, lease_ttl=600)

    second = r.claim_topic_markdown_sync(topic.id, now=701, lease_ttl=600)
    assert second.owner_id != first.owner_id
    with pytest.raises(ValueError, match="owner lost"):
        r.finish_topic_markdown_sync(topic.id, first.owner_id, "SYNCED", now=702)
    finished = r.finish_topic_markdown_sync(topic.id, second.owner_id, "SYNCED", now=703)
    assert finished.status == "SYNCED" and finished.owner_id == ""


def test_topic_markdown_sync_fence_validates_owner_and_renews_lease(tmp_path):
    r = repo(tmp_path)
    course = r.create_course("课程", "", str(tmp_path / "out"))
    topic = r.create_topic(course.id, 0, "主题")
    r.set_topic_markdown_sync_state(topic.id, "PENDING")
    claim = r.claim_topic_markdown_sync(topic.id, now=100, lease_ttl=10)

    renewed = r.fence_topic_markdown_sync(topic.id, claim.owner_id, now=105, lease_ttl=20)
    assert renewed.lease_expires_at == 125
    with pytest.raises(ValueError, match="owner lost"):
        r.fence_topic_markdown_sync(topic.id, "old-owner", now=106, lease_ttl=20)
    with pytest.raises(ValueError, match="owner lost"):
        r.fence_topic_markdown_sync(topic.id, claim.owner_id, now=126, lease_ttl=20)
