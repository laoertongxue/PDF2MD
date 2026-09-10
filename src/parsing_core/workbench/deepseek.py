from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.request
from collections.abc import Mapping
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request

SYSTEM_PROMPT = "你是严谨的 MBA 课程精读助教。"
DEFAULT_MAX_TOKENS = 4_096
TOPIC_OUTLINE_MAX_TOKENS = 8_192
FALLBACK_MAX_INPUT_TOKENS = 65_536
MAX_HTTP_RESPONSE_BYTES = 3 * 1024 * 1024
MODEL_NAME = "deepseek-v4-pro"
MAX_TIMEOUT_SECONDS = 300
MAX_RETRIES = 3
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_RETRIES = 2
DEEPSEEK_CHECKPOINT_PROTOCOL_VERSION = 1
MAX_ENDPOINT_BYTES = 2_048


class _TokenCounter(Protocol):
    def __call__(self, *, model: str, text: str) -> int: ...


class _ModelInfoGetter(Protocol):
    def __call__(self, model: str) -> Mapping[str, object]: ...


class DeepSeekError(RuntimeError):
    pass


def _canonical_endpoint(value: str) -> str:
    if type(value) is not str:
        raise DeepSeekError("DeepSeek endpoint is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise DeepSeekError("DeepSeek endpoint is invalid") from None
    if not encoded or len(encoded) > MAX_ENDPOINT_BYTES or any(ord(char) <= 0x20 for char in value):
        raise DeepSeekError("DeepSeek endpoint is invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise DeepSeekError("DeepSeek endpoint is invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise DeepSeekError("DeepSeek endpoint must use HTTPS")
    try:
        normalized_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise DeepSeekError("DeepSeek endpoint is invalid") from None
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host if port in (None, 443) else f"{normalized_host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


class DeepSeekClient:
    __slots__ = ("_api_key", "_model", "_base_url")

    def __init__(
        self, api_key: str, model: str, base_url: str = "https://api.deepseek.com/chat/completions"
    ):
        if not api_key:
            raise DeepSeekError("deepseek api key missing")
        if model != MODEL_NAME:
            raise DeepSeekError(f"only {MODEL_NAME} is supported")
        self._api_key = api_key
        self._model = model
        self._base_url = _canonical_endpoint(base_url)

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def checkpoint_auth_binding(self) -> str:
        return hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()

    def complete(
        self,
        prompt: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        cancel_event: threading.Event | None = None,
        retries: int = DEFAULT_RETRIES,
    ) -> str:
        if not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
            raise DeepSeekError("deepseek timeout is invalid")
        if not isinstance(retries, int) or retries < 0 or retries > MAX_RETRIES:
            raise DeepSeekError("deepseek retry limit is invalid")
        if cancel_event is not None and cancel_event.is_set():
            raise DeepSeekError("deepseek request cancelled")
        payload = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }
        ).encode()
        req = Request(
            self.base_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        raw = None
        for attempt in range(retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DeepSeekError("deepseek request cancelled")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as res:
                    raw = res.read(MAX_HTTP_RESPONSE_BYTES + 1)
                break
            except (HTTPError, URLError, TimeoutError) as exc:
                if attempt >= retries or not _retryable(exc):
                    raise DeepSeekError("deepseek request failed") from exc
                delay = min(2**attempt, 4)
                if cancel_event is not None:
                    cancel_event.wait(delay)
                else:
                    time.sleep(delay)
        if raw is None:
            raise DeepSeekError("deepseek request failed")
        if cancel_event is not None and cancel_event.is_set():
            raise DeepSeekError("deepseek request cancelled")
        if len(raw) > MAX_HTTP_RESPONSE_BYTES:
            raise DeepSeekError("deepseek response exceeds limit")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeepSeekError("deepseek returned malformed response") from exc
        if not isinstance(data, dict):
            raise DeepSeekError("deepseek returned malformed response")
        choices = data.get("choices") or []
        if not isinstance(choices, list):
            raise DeepSeekError("deepseek returned malformed response")
        if choices and not isinstance(choices[0], dict):
            raise DeepSeekError("deepseek returned malformed response")
        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            raise DeepSeekError("deepseek returned malformed response")
        content = message.get("content", "")
        if not isinstance(content, str):
            raise DeepSeekError("deepseek returned malformed response")
        if not content.strip():
            raise DeepSeekError("deepseek returned empty content")
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        if first_choice.get("finish_reason") == "length":
            raise DeepSeekError("deepseek response was truncated")
        return content


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, (URLError, TimeoutError)) or (
        isinstance(exc, HTTPError) and (exc.code == 429 or exc.code >= 500)
    )


class DeepSeekExecutor:
    def __init__(self, client: DeepSeekClient):
        self.client = client

    def run(self, round_key: str, task_package: str) -> str:
        return self.client.complete(
            task_package,
            max_tokens=self._output_budget(round_key),
        )

    def checkpoint_identity(self) -> dict[str, object]:
        return {
            "executor": "deepseek",
            "protocol_version": DEEPSEEK_CHECKPOINT_PROTOCOL_VERSION,
            "model": self.client.model,
            "endpoint": self.client.base_url,
            "auth_binding": self.client.checkpoint_auth_binding,
            "system_prompt": SYSTEM_PROMPT,
            "request": {
                "roles": ["system", "user"],
                "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "default_retries": DEFAULT_RETRIES,
                "max_response_bytes": MAX_HTTP_RESPONSE_BYTES,
            },
            "output_budgets": {
                "default": DEFAULT_MAX_TOKENS,
                "topic_outline": TOPIC_OUTLINE_MAX_TOKENS,
            },
        }

    def validate_prompt(self, task_key: str, prompt: str) -> None:
        reserve = self._output_budget(task_key)
        model = self._normalized_model_name(self.client.model)
        source = "LiteLLM"
        try:
            token_counter, get_model_info = self._load_litellm()
            prompt_tokens = token_counter(model=model, text=SYSTEM_PROMPT) + token_counter(
                model=model,
                text=prompt,
            )
            max_input_tokens_value = get_model_info(model).get("max_input_tokens")
            if not isinstance(prompt_tokens, int) or not isinstance(max_input_tokens_value, int):
                raise ValueError("invalid LiteLLM token metadata")
            max_input_tokens = max_input_tokens_value
        except Exception:
            source = "fallback token estimate"
            prompt_tokens = len((SYSTEM_PROMPT + prompt).encode("utf-8"))
            max_input_tokens = FALLBACK_MAX_INPUT_TOKENS

        if prompt_tokens + reserve > max_input_tokens:
            raise DeepSeekError(
                "deepseek prompt exceeds token budget "
                f"({source}): {prompt_tokens} + {reserve} > {max_input_tokens}"
            )

    def _load_litellm(self) -> tuple[_TokenCounter, _ModelInfoGetter]:
        from litellm import get_model_info, token_counter

        return cast(_TokenCounter, token_counter), cast(_ModelInfoGetter, get_model_info)

    @staticmethod
    def _normalized_model_name(model: str) -> str:
        if model.startswith("deepseek/"):
            return model.removeprefix("deepseek/")
        return model

    @staticmethod
    def _output_budget(task_key: str) -> int:
        if task_key == "topic_outline":
            return TOPIC_OUTLINE_MAX_TOKENS
        return DEFAULT_MAX_TOKENS
