from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass


class BaiduOcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class BaiduRequest:
    url: str
    headers: dict[str, str]
    body: bytes


Transport = Callable[[BaiduRequest], tuple[int, bytes]]

_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RETRIES = 2
_SENSITIVE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]+|Bearer\s+[^\s,}]+|/Users/[^\s,}]+|/var/[^\s,}]+|\b(?:key|token|access_token|api_key)\s*[:=]\s*[\"']?[^\s,}\"']+)",
    re.IGNORECASE,
)
_JSON_SECRET = re.compile(
    r"(\"?(?:key|token|access_token|api_key)\"?\s*:\s*\")([^\"]*)(\")", re.IGNORECASE
)


class BaiduOcrClient:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://qianfan.baidubce.com/v2/ocr/structure",
        timeout: float = 30.0,
        max_image_bytes: int = _MAX_IMAGE_BYTES,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        max_retries: int = _MAX_RETRIES,
        transport: Transport | None = None,
    ):
        if not api_key or len(api_key) > 512:
            raise ValueError("Baidu API key is required")
        if not endpoint.startswith("https://"):
            raise ValueError("Baidu endpoint must use HTTPS")
        if timeout <= 0 or timeout > 120:
            raise ValueError("invalid Baidu timeout")
        if max_image_bytes <= 0 or max_response_bytes <= 0:
            raise ValueError("invalid Baidu size limit")
        if max_retries < 0 or max_retries > _MAX_RETRIES:
            raise ValueError("invalid Baidu retry limit")
        self._api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_image_bytes = max_image_bytes
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.transport = transport

    def recognize(self, image: bytes, *, allow_network: bool = False) -> dict:
        if not isinstance(image, bytes):
            raise BaiduOcrError("Baidu OCR image is invalid")
        if len(image) > self.max_image_bytes:
            raise BaiduOcrError("Baidu OCR image is too large")
        if not allow_network:
            raise BaiduOcrError("Baidu OCR network disabled")
        request = BaiduRequest(
            url=self.endpoint,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/octet-stream",
            },
            body=image,
        )
        for attempt in range(self.max_retries + 1):
            try:
                status, body = self._send(request)
            except BaiduOcrError:
                raise
            except Exception as exc:
                if attempt < self.max_retries:
                    continue
                raise BaiduOcrError(redact_baidu_error(str(exc))) from None
            if len(body) > self.max_response_bytes:
                raise BaiduOcrError("Baidu OCR response is too large")
            if status in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                continue
            if status < 200 or status >= 300:
                detail = _response_detail(body)
                raise BaiduOcrError(
                    redact_baidu_error(f"Baidu OCR request failed: {status} {detail}")
                )
            try:
                value = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise BaiduOcrError("Baidu OCR returned invalid JSON") from None
            if not isinstance(value, dict):
                raise BaiduOcrError("Baidu OCR returned invalid JSON")
            return value
        raise BaiduOcrError("Baidu OCR request failed")

    def _send(self, request: BaiduRequest) -> tuple[int, bytes]:
        if self.transport is not None:
            return self.transport(request)
        req = urllib.request.Request(
            request.url, data=request.body, headers=request.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return int(response.status), _read_bounded(response, self.max_response_bytes)
        except urllib.error.HTTPError as exc:
            return int(exc.code), _read_bounded(exc, self.max_response_bytes)


def redact_baidu_error(message: str) -> str:
    if not isinstance(message, str):
        return "Baidu OCR request failed"
    value = _JSON_SECRET.sub(r"\1[REDACTED]\3", message)
    value = _SENSITIVE.sub("[REDACTED]", value)
    if not value.startswith("Baidu OCR request failed"):
        value = f"Baidu OCR request failed: {value}"
    return value[:512]


def _read_bounded(stream, limit: int) -> bytes:
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise BaiduOcrError("Baidu OCR response is too large")
    return body


def _response_detail(body: bytes) -> str:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "remote error"
    if not isinstance(value, dict):
        return "remote error"
    detail = value.get("error_msg") or value.get("message") or "remote error"
    return str(detail)[:160]
