import pytest

from parsing_core.storage.schema import init_db
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.topic_task_package import build_topic_task_package


def setup_ready_topic(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "课程说明", str(tmp_path))
    sources = [
        repo.create_source(course.id, "main", "/tmp/b.pdf", "竞争战略"),
        repo.create_source(course.id, "main", "/tmp/a.pdf", "管理学"),
    ]
    chapters = [
        repo.create_chapter(course.id, sources[0].id, 1, "竞争优势", "/forbidden/b.md"),
        repo.create_chapter(course.id, sources[1].id, 0, "战略基础", "/forbidden/a.md"),
    ]
    for chapter in chapters:
        repo.upsert_note_block(chapter.id, "summary", "摘要", chapter.title, 0)
        repo.upsert_run(chapter.id, "review", "test", "DONE", "", "", "ok")
    topic = repo.create_topic(course.id, 0, "竞争优势融合", "对照两本教材")
    repo.update_topic(topic.id, confirmed=True, status="READY")
    repo.replace_topic_chapters(topic.id, [chapters[0].id, chapters[1].id])
    return repo, topic, chapters


def test_task_package_uses_only_reviewed_note_blocks_in_stable_order(tmp_path):
    repo, topic, _ = setup_ready_topic(tmp_path)

    package = build_topic_task_package(repo, topic.id, {"alignment": {"overview": "前序"}})

    assert package.topic_title == "竞争优势融合"
    assert [chapter.source_title for chapter in package.source_chapters] == ["竞争战略", "管理学"]
    assert [chapter.source_label for chapter in package.source_chapters] == [
        "[《竞争战略》·第 2 章]",
        "[《管理学》·第 1 章]",
    ]
    assert package.source_chapters[0].note_blocks[0].body == "竞争优势"
    assert package.previous_outputs == {"alignment": {"overview": "前序"}}
    assert len(package.input_fingerprint) == 64


def test_task_package_fingerprint_changes_with_mapping_notes_and_review(tmp_path):
    repo, topic, chapters = setup_ready_topic(tmp_path)
    first = build_topic_task_package(repo, topic.id).input_fingerprint
    repo.upsert_note_block(chapters[0].id, "summary", "摘要", "新内容", 0)
    second = build_topic_task_package(repo, topic.id).input_fingerprint
    assert second != first


def test_duplicate_source_titles_get_unique_stable_labels(tmp_path):
    repo, topic, chapters = setup_ready_topic(tmp_path)
    duplicate = repo.create_source(topic.course_id, "main", "/tmp/c.pdf", "竞争战略")
    chapter = repo.create_chapter(topic.course_id, duplicate.id, 1, "竞争策略", "/x.md")
    repo.upsert_note_block(chapter.id, "summary", "摘要", "竞争策略", 0)
    repo.upsert_run(chapter.id, "review", "test", "DONE", "", "", "ok")
    repo.replace_topic_chapters(topic.id, [*[item.id for item in chapters], chapter.id])

    package = build_topic_task_package(repo, topic.id)
    labels = [item.source_label for item in package.source_chapters]

    assert [item.source_title for item in package.source_chapters].count("竞争战略") == 2
    assert [item.source_display_title for item in package.source_chapters].count(
        "竞争战略（2）"
    ) == 1
    assert labels.count("[《竞争战略》·第 2 章]") == 1
    assert labels.count("[《竞争战略（2）》·第 2 章]") == 1
    assert len(labels) == len(set(labels))
    assert len({item.chapter_id: item.source_label for item in package.source_chapters}) == 3


def test_reserved_real_titles_force_duplicate_suffixes_to_skip_collisions(tmp_path):
    repo, topic, chapters = setup_ready_topic(tmp_path)
    repo.replace_topic_chapters(topic.id, [])
    created = []
    for index, title in enumerate(["教材", "教材", "教材（2）", "教材（3）"]):
        source = repo.create_source(topic.course_id, "main", f"/tmp/{index}.pdf", title)
        chapter = repo.create_chapter(
            topic.course_id, source.id, 0, f"章节 {index}", f"/tmp/{index}.md"
        )
        repo.upsert_note_block(chapter.id, "summary", "摘要", chapter.title, 0)
        repo.upsert_run(chapter.id, "review", "test", "DONE", "", "", "ok")
        created.append(chapter)
    repo.replace_topic_chapters(topic.id, [chapter.id for chapter in created])

    package = build_topic_task_package(repo, topic.id)
    by_chapter = {item.chapter_id: item for item in package.source_chapters}

    assert [by_chapter[chapter.id].source_title for chapter in created] == [
        "教材", "教材", "教材（2）", "教材（3）"
    ]
    assert {by_chapter[chapter.id].source_display_title for chapter in created} == {
        "教材", "教材（2）", "教材（3）", "教材（4）"
    }
    duplicate_displays = [
        item.source_display_title
        for item in package.source_chapters
        if item.source_title == "教材"
    ]
    assert duplicate_displays == ["教材", "教材（4）"]
    label_to_chapter = {
        item.source_label: item.chapter_id for item in package.source_chapters
    }
    assert len(label_to_chapter) == 4
    for item in package.source_chapters:
        assert item.source_label == (
            f"[《{item.source_display_title}》·第 {item.seq + 1} 章]"
        )
        assert label_to_chapter[item.source_label] == item.chapter_id


def test_unicode_equivalent_titles_and_multiple_chapters_keep_exact_mapping(tmp_path):
    repo, topic, _ = setup_ready_topic(tmp_path)
    sources = [
        repo.create_source(topic.course_id, "main", "/tmp/full.pdf", "Ａ  教材"),
        repo.create_source(topic.course_id, "main", "/tmp/ascii.pdf", "a 教材"),
    ]
    chapters = [
        repo.create_chapter(topic.course_id, sources[0].id, seq, f"全角 {seq}", f"/f{seq}.md")
        for seq in (0, 1)
    ]
    chapters.append(repo.create_chapter(topic.course_id, sources[1].id, 0, "半角", "/a.md"))
    for chapter in chapters:
        repo.upsert_note_block(chapter.id, "summary", "摘要", chapter.title, 0)
        repo.upsert_run(chapter.id, "review", "test", "DONE", "", "", "ok")
    repo.replace_topic_chapters(topic.id, [chapter.id for chapter in chapters])

    package = build_topic_task_package(repo, topic.id)
    by_chapter = {item.chapter_id: item for item in package.source_chapters}

    fullwidth_display = by_chapter[chapters[0].id].source_display_title
    assert by_chapter[chapters[1].id].source_display_title == fullwidth_display
    equivalent_sources = []
    for item in package.source_chapters:
        if (
            item.source_title in {"Ａ  教材", "a 教材"}
            and item.source_display_title not in equivalent_sources
        ):
            equivalent_sources.append(item.source_display_title)
    assert equivalent_sources[0] in {"Ａ  教材", "a 教材"}
    assert equivalent_sources[1].endswith("（2）")
    assert by_chapter[chapters[0].id].source_label.endswith("第 1 章]")
    assert by_chapter[chapters[1].id].source_label.endswith("第 2 章]")
    assert len({item.source_label for item in package.source_chapters}) == 3


@pytest.mark.parametrize("status,stale", [("FAILED", False), ("DONE", True)])
def test_task_package_rejects_unready_chapter_review(tmp_path, status, stale):
    repo, topic, chapters = setup_ready_topic(tmp_path)
    repo.upsert_run(chapters[0].id, "review", "test", status, "", "", "bad", stale=stale)
    with pytest.raises(ValueError, match="topic dependencies are not ready"):
        build_topic_task_package(repo, topic.id)
