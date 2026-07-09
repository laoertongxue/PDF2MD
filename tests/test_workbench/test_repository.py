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
