from unittest.mock import MagicMock, patch

from parsing_core.llm.litellm_client import RealLLMClient
from parsing_core.models.dataclasses import Section


def make_section(raw="## A\n\nbody"):
    return Section(
        id="s1",
        task_id="t1",
        seq=0,
        raw_md_path="/x.raw.md",
        sha256="h",
        char_count=len(raw),
        ai_status="PENDING",
    )


def test_interpret_calls_litellm_correct_model(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test-model")
    client = RealLLMClient("local")
    sec = make_section()

    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = "AI response"

    with patch(
        "parsing_core.llm.litellm_client.litellm.completion",
        return_value=[chunk],
    ) as mock_llm:
        artifact = client.interpret(sec, raw_md="## A\n\nbody")

    call_kwargs = mock_llm.call_args[1]
    assert "ollama" in call_kwargs["model"]
    assert call_kwargs["api_base"] == "http://localhost:11434"
    assert artifact.ai_md == "AI response"


def test_interpret_returns_ai_artifact(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test")
    client = RealLLMClient("local")
    sec = make_section()

    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = "response text"

    with patch("parsing_core.llm.litellm_client.litellm.completion", return_value=[chunk]):
        artifact = client.interpret(sec, raw_md="body")

    assert artifact.ai_md == "response text"
    assert "ollama" in artifact.model_name
    assert artifact.tokens_in > 0


def test_interpret_streaming_calls_on_progress(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test")
    tokens = []

    def cb(task_id, kind, payload):
        if kind == "LLM_TOKEN":
            tokens.append(payload["token"])

    client = RealLLMClient("local", on_progress=cb)
    sec = make_section()

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock()]
    chunk1.choices[0].delta = MagicMock()
    chunk1.choices[0].delta.content = "Hello"

    chunk2 = MagicMock()
    chunk2.choices = [MagicMock()]
    chunk2.choices[0].delta = MagicMock()
    chunk2.choices[0].delta.content = " World"

    with patch("parsing_core.llm.litellm_client.litellm.completion", return_value=[chunk1, chunk2]):
        artifact = client.interpret(sec, raw_md="body")

    assert tokens == ["Hello", " World"]
    assert artifact.ai_md == "Hello World"


def test_prompt_template_used_in_call(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_LOCAL_MODEL", "ollama/test")
    client = RealLLMClient("local")
    sec = make_section(raw="## Test\n\nsample content")

    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = "ok"

    with patch(
        "parsing_core.llm.litellm_client.litellm.completion",
        return_value=[chunk],
    ) as mock_llm:
        client.interpret(sec, raw_md="## Test\n\nsample content")

    call_kwargs = mock_llm.call_args[1]
    user_msg = call_kwargs["messages"][0]["content"]
    assert "## Test" in user_msg
    assert "sample content" in user_msg
    assert "### ▸ AI 解读" in user_msg


def test_stub_tier_returns(monkeypatch):
    client = RealLLMClient("stub")
    sec = make_section()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "ok"

    with patch(
        "parsing_core.llm.litellm_client.litellm.completion",
        return_value=mock_response,
    ) as mock_llm:
        artifact = client.interpret(sec, raw_md="body")

    assert mock_llm.call_args[1]["model"] == "stub"
    assert artifact.ai_md == "ok"


def test_public_tier_config(monkeypatch):
    monkeypatch.setenv("PARSING_CORE_PUBLIC_MODEL", "openai/gpt-4o")
    client = RealLLMClient("public")
    assert client.tier == "public"
