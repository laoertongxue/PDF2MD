import pytest

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
