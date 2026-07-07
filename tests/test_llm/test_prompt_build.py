from parsing_core.llm.prompt_templates import SECTION_INTERPRET_PROMPT


def test_prompt_contains_raw_md():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="## A\n\nbody text here")
    assert "## A" in prompt
    assert "body text here" in prompt
    assert "mermaid" in prompt


def test_prompt_contains_interpret_header():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="content")
    assert "### ▸ AI 解读" in prompt


def test_prompt_has_mermaid_instruction():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="x")
    assert "mermaid" in prompt.lower()


def test_prompt_formats_empty_raw_md():
    prompt = SECTION_INTERPRET_PROMPT.format(raw_md="")
    assert "<<" in prompt
