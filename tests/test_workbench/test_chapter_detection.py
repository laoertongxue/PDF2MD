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
