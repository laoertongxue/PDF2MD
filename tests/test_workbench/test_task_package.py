from pathlib import Path

from parsing_core.storage.schema import init_db
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema
from parsing_core.workbench.task_package import build_task_package, write_task_package


def test_task_package_contains_rules_and_source(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "MBA", str(tmp_path / "out"))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "战略教材")
    source_md = tmp_path / "ch1.md"
    source_md.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(source_md))

    package = build_task_package(repo, chapter.id, "concepts")
    path = write_task_package(package, tmp_path)

    text = Path(path).read_text(encoding="utf-8")
    assert "战略是选择" in text
    assert "MBA 精读助教" in text
    assert "两张 Mermaid 图" in text
    assert "知识结构图和应用流程图" in text
    assert "公众号长文素材" in text
    assert "[src:" in text


def test_stub_executor_returns_deterministic_output():
    output = StubIntensiveReadingExecutor().run("cards", "input")
    assert "选题卡" in output


def test_task_package_contains_attachment_citation_ids_and_fingerprint(tmp_path):
    conn = init_db(str(tmp_path / "workbench.db"))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path))
    source = repo.create_source(course.id, "main", "/tmp/book.pdf", "教材")
    chapter_md = tmp_path / "chapter.md"
    chapter_md.write_text("正文", encoding="utf-8")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", str(chapter_md))
    repo.create_attachment(
        course.id,
        source.id,
        chapter.id,
        "/tmp/case.pdf",
        "案例",
        "pdf",
        "案例文本",
        "hash-1",
        [{"citation_id": "att:case:p2:para3", "page": 2, "paragraph": 3, "text": "案例文本"}],
    )
    package = build_task_package(repo, chapter.id, "concepts")
    assert package.input_fingerprint
    assert package.citation_ids[0].startswith("src:")
    assert package.citation_ids[-1] == "att:case:p2:para3"
    assert "[att:case:p2:para3]" in package.content
