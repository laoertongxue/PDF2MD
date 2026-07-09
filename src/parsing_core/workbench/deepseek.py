import json
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.request import Request


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

    def complete(self, prompt: str, timeout: int = 120) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的 MBA 课程精读助教。"},
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
                data = json.loads(res.read().decode())
        except (HTTPError, URLError, TimeoutError) as exc:
            raise DeepSeekError(str(exc)) from exc
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content.strip():
            raise DeepSeekError("deepseek returned empty content")
        return content


class DeepSeekExecutor:
    def __init__(self, client: DeepSeekClient):
        self.client = client

    def run(self, round_key: str, task_package: str) -> str:
        return self.client.complete(task_package)
