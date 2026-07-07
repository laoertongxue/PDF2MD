from parsing_core.parser.chunker import split_sections


def test_split_by_h2():
    md = "# Doc\n\n## A\n\nfoo\n\n## B\n\nbar\n"
    chunks = split_sections(md)
    assert len(chunks) == 2
    assert chunks[0].title == "A"
    assert chunks[1].title == "B"
    assert "foo" in chunks[0].raw
    assert "bar" in chunks[1].raw


def test_split_by_h3():
    md = "## A.1\n\nfoo\n\n## A.2\n\nbar\n"
    chunks = split_sections(md)
    assert len(chunks) == 2
    assert chunks[0].title == "A.1"
    assert chunks[1].title == "A.2"


def test_split_table_as_own_section():
    md = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter\n"
    chunks = split_sections(md)
    assert any("|---" in c.raw for c in chunks)
    # 表格应单独成节
    table_chunks = [c for c in chunks if "|---" in c.raw]
    assert len(table_chunks) == 1


def test_long_section_splits():
    para = "word " * 1500  # ~7500 字符
    md = f"## Big\n\n{para}\n"
    chunks = split_sections(md)
    assert len(chunks) > 1
    for c in chunks:
        assert c.char_count <= 4500  # 留余量


def test_returns_raw_and_sha():
    md = "## A\n\nfoo\n"
    chunks = split_sections(md)
    assert chunks[0].raw
    assert len(chunks[0].sha256) == 64  # sha256 hexdigest
    assert chunks[0].seq == 0
    assert chunks[0].char_count == len(chunks[0].raw)


def test_empty_markdown_returns_empty():
    chunks = split_sections("")
    assert chunks == []


def test_seq_starts_at_zero_increments_by_one():
    md = "## A\n\nx\n\n## B\n\ny\n\n## C\n\nz\n"
    chunks = split_sections(md)
    assert [c.seq for c in chunks] == [0, 1, 2]
