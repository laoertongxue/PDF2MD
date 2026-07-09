import json
from urllib.error import HTTPError

import pytest

from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekError, DeepSeekExecutor


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_deepseek_client_returns_message(monkeypatch):
    def fake_urlopen(req, timeout):
        assert req.headers["Authorization"] == "Bearer sk-test"
        return FakeResponse({"choices": [{"message": {"content": "精读结果"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model="deepseek-chat")
    assert client.complete("hello") == "精读结果"


def test_deepseek_client_raises_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 401, "bad key", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model="deepseek-chat")
    with pytest.raises(DeepSeekError):
        client.complete("hello")


def test_deepseek_client_raises_on_empty_choices(monkeypatch):
    def fake_urlopen(req, timeout):
        return FakeResponse({"choices": []})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DeepSeekClient(api_key="sk-test", model="deepseek-chat")
    with pytest.raises(DeepSeekError, match="deepseek returned empty content"):
        client.complete("hello")


def test_deepseek_executor_uses_client():
    class Client:
        def complete(self, prompt: str) -> str:
            assert "## 原文" in prompt
            return "结构理解"

    executor = DeepSeekExecutor(Client())

    assert executor.run("structure", "## 原文\n战略") == "结构理解"
