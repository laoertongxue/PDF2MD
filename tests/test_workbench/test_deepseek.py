import json
import threading
from urllib.error import HTTPError

import pytest

from parsing_core.workbench.deepseek import (
    MAX_HTTP_RESPONSE_BYTES,
    MODEL_NAME,
    TOPIC_OUTLINE_MAX_TOKENS,
    DeepSeekClient,
    DeepSeekError,
    DeepSeekExecutor,
)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return json.dumps(self.payload).encode()


def test_deepseek_client_returns_message(monkeypatch):
    request_payload = {}

    def fake_urlopen(req, timeout):
        assert req.headers["Authorization"] == "Bearer sk-test"
        request_payload.update(json.loads(req.data))
        return FakeResponse({"choices": [{"message": {"content": "精读结果"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model=MODEL_NAME)
    assert client.complete("hello", max_tokens=321) == "精读结果"
    assert request_payload["max_tokens"] == 321


def test_deepseek_client_raises_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 401, "bad key", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model=MODEL_NAME)
    with pytest.raises(DeepSeekError):
        client.complete("hello")


def test_deepseek_client_raises_on_empty_choices(monkeypatch):
    def fake_urlopen(req, timeout):
        return FakeResponse({"choices": []})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model=MODEL_NAME)
    with pytest.raises(DeepSeekError, match="deepseek returned empty content"):
        client.complete("hello")


def test_deepseek_executor_uses_client():
    class Client:
        model = "deepseek-chat"

        def complete(self, prompt: str, *, max_tokens: int) -> str:
            assert "## 原文" in prompt
            assert max_tokens > 0
            return "结构理解"

    executor = DeepSeekExecutor(Client())

    assert executor.run("structure", "## 原文\n战略") == "结构理解"


def test_topic_outline_uses_fixed_output_budget():
    calls = []

    class Client:
        model = "deepseek-chat"

        def complete(self, prompt, *, max_tokens):
            calls.append((prompt, max_tokens))
            return "{}"

    executor = DeepSeekExecutor(Client())
    executor.validate_prompt("topic_outline", "small prompt")
    output = executor.run("topic_outline", "prompt")

    assert output == "{}"
    assert calls == [("prompt", TOPIC_OUTLINE_MAX_TOKENS)]


def test_validate_prompt_uses_conservative_fallback(monkeypatch):
    class Client:
        model = "deepseek-chat"

    executor = DeepSeekExecutor(Client())
    monkeypatch.setattr(executor, "_load_litellm", lambda: (_ for _ in ()).throw(ImportError()))

    with pytest.raises(DeepSeekError, match="fallback token estimate"):
        executor.validate_prompt("topic_outline", "汉" * 20_000)


def test_http_response_read_is_bounded_and_oversize_is_stable(monkeypatch):
    calls = []

    class OversizeResponse(FakeResponse):
        def read(self, size=-1):
            calls.append(size)
            return b"x" * size

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: OversizeResponse({}),
    )
    monkeypatch.setattr(
        "parsing_core.workbench.deepseek.json.loads",
        lambda value: (_ for _ in ()).throw(AssertionError("JSON must not be parsed")),
    )
    client = DeepSeekClient(api_key="sk-test", model=MODEL_NAME)

    with pytest.raises(DeepSeekError, match="deepseek response exceeds limit"):
        client.complete("hello")
    assert calls == [MAX_HTTP_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("body", [b"\xff", b"not-json"])
def test_malformed_http_response_has_stable_error(monkeypatch, body):
    class MalformedResponse(FakeResponse):
        def read(self, size=-1):
            return body

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: MalformedResponse({}),
    )
    client = DeepSeekClient(api_key="sk-secret", model=MODEL_NAME)

    with pytest.raises(DeepSeekError, match="deepseek returned malformed response") as error:
        client.complete("hello")
    assert "sk-secret" not in str(error.value)
    decoded = body.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in str(error.value)


def test_malformed_choice_item_has_stable_error(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: FakeResponse({"choices": ["not-an-object"]}),
    )
    client = DeepSeekClient(api_key="sk-secret", model=MODEL_NAME)
    with pytest.raises(DeepSeekError, match="malformed response"):
        client.complete("hello")


def test_retry_limit_is_bounded_and_cancellable(monkeypatch):
    calls = []

    def failing_urlopen(req, timeout):
        calls.append(1)
        raise HTTPError(req.full_url, 503, "server details", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)
    event = threading.Event()
    client = DeepSeekClient(api_key="sk-secret", model=MODEL_NAME)
    with pytest.raises(DeepSeekError, match="request failed"):
        client.complete("hello", retries=2)
    assert len(calls) == 3

    event.set()
    with pytest.raises(DeepSeekError, match="cancelled"):
        client.complete("hello", cancel_event=event)
