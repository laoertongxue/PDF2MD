from parsing_core.workbench.chapter_detection import detect_chapters


def test_detect_chapters_from_markdown_headings():
    md = """# Book

## 第一章 战略是什么
内容 A

### 1.1 战略的定义
内容 B

## 第二章 外部环境
内容 C
"""
    chapters = detect_chapters(md)

    assert [c.title for c in chapters] == ["第一章 战略是什么", "第二章 外部环境"]
    assert chapters[0].raw_md.startswith("## 第一章")
    assert "### 1.1 战略的定义" in chapters[0].raw_md
    assert chapters[1].seq == 1


def test_detect_chapters_falls_back_to_single_chapter():
    chapters = detect_chapters("没有标题的正文")
    assert len(chapters) == 1
    assert chapters[0].title == "全文"


def test_detect_chapters_supports_h1_and_keeps_nested_h2_inside_chapter():
    markdown = "# 第一章\n导言\n## 第一节\n内容\n# 第二章\n结尾"
    chapters = detect_chapters(markdown)
    assert [chapter.title for chapter in chapters] == ["第一章", "第二章"]
    assert "## 第一节" in chapters[0].raw_md
    assert [(chapter.start, chapter.end) for chapter in chapters] == [
        (0, markdown.index("# 第二章")),
        (markdown.index("# 第二章"), len(markdown)),
    ]


def test_detect_chapters_uses_h2_when_h1_is_document_title():
    chapters = detect_chapters("# 战略管理\n前言\n## 第一章\nA\n## 第二章\nB")
    assert [chapter.title for chapter in chapters] == ["第一章", "第二章"]


def test_detect_chapters_prefers_h2_chapter_level_below_multiple_document_h1_sections():
    chapters = detect_chapters("# Strategy Management\n## Introduction\nA\n# Appendix\n## Cases\nB")
    assert [chapter.title for chapter in chapters] == ["Introduction", "Cases"]


def test_detect_chapters_handles_chapter_headings_mixed_across_h1_and_h2():
    chapters = detect_chapters("# 第一章 总论\n## 第一节\nA\n## 第二章 战略\n### 第二节\nB")
    assert [chapter.title for chapter in chapters] == ["第一章 总论", "第二章 战略"]
