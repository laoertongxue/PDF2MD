import json
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.request import Request

SYSTEM_PROMPT = "你是严谨的 MBA 课程精读助教。"
DEFAULT_MAX_TOKENS = 4_096
TOPIC_OUTLINE_MAX_TOKENS = 8_192
FALLBACK_MAX_INPUT_TOKENS = 65_536
MAX_HTTP_RESPONSE_BYTES = 3 * 1024 * 1024


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com/chat/completions"):
        if not api_key:
            raise DeepSeekError("deepseek api key missing")
        if not model:
            raise DeepSeekError("deepseek model missing")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def complete(
        self,
        prompt: str,
        timeout: int = 120,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise DeepSeekError(str(exc)) from exc
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
        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            raise DeepSeekError("deepseek returned malformed response")
        content = message.get("content", "")
        if not isinstance(content, str):
            raise DeepSeekError("deepseek returned malformed response")
        if not content.strip():
            raise DeepSeekError("deepseek returned empty content")
        return content


class DeepSeekExecutor:
    def __init__(self, client: DeepSeekClient):
        self.client = client

    def run(self, round_key: str, task_package: str) -> str:
        return self.client.complete(
            task_package,
            max_tokens=self._output_budget(round_key),
        )

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
            max_input_tokens = get_model_info(model)["max_input_tokens"]
            if not isinstance(prompt_tokens, int) or not isinstance(max_input_tokens, int):
                raise ValueError("invalid LiteLLM token metadata")
        except Exception:
            source = "fallback token estimate"
            prompt_tokens = len((SYSTEM_PROMPT + prompt).encode("utf-8"))
            max_input_tokens = FALLBACK_MAX_INPUT_TOKENS

        if prompt_tokens + reserve > max_input_tokens:
            raise DeepSeekError(
                "deepseek prompt exceeds token budget "
                f"({source}): {prompt_tokens} + {reserve} > {max_input_tokens}"
            )

    def _load_litellm(self):
        from litellm import get_model_info, token_counter

        return token_counter, get_model_info

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
