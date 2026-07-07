# tests/test_stub_client.py
from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.models.dataclasses import Section


def make_section(seq=0, raw="## A\n\nbody"):
    return Section(
        id="s1",
        task_id="t1",
        seq=seq,
        raw_md_path="/x.raw.md",
        sha256="h",
        char_count=len(raw),
        ai_status="PENDING",
    )


def test_stub_output_has_ai_interpret_header():
    s = make_section(raw="## A\n\nbody")
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert "▸ AI 解读" in a.ai_md


def test_stub_output_has_mermaid_block():
    s = make_section()
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert "```mermaid" in a.ai_md
    assert a.ai_md.count("```") >= 2  # 至少一对代码块边界


def test_stub_output_includes_seq_number():
    s = make_section(seq=5)
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert "5" in a.ai_md


def test_stub_tokens_recorded():
    s = make_section()
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert a.tokens_in > 0
    assert a.tokens_out > 0


def test_stub_model_name():
    s = make_section()
    a = StubLLMClient().interpret(s, raw_md="## A\n\nbody")
    assert a.model_name == "stub"


def test_stub_is_deterministic():
    s = make_section(seq=3, raw="content")
    a1 = StubLLMClient().interpret(s, raw_md="content")
    a2 = StubLLMClient().interpret(s, raw_md="content")
    assert a1.ai_md == a2.ai_md
