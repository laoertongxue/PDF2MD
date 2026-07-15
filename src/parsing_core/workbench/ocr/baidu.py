from __future__ import annotations

import hmac
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class BaiduOcrError(RuntimeError):
    pass


class BaiduEscalationReason(StrEnum):
    CONFLICT = "conflict"
    COMPLEX = "complex"
    SAMPLE = "sample"


_AUTHORIZATION_CAPABILITY = object()


@dataclass(frozen=True, slots=True, init=False)
class BaiduEscalationAuthorization:
    reason: BaiduEscalationReason
    page_hash: str
    input_fingerprint: str
    alignment_status: str
    page: int

    def __init__(
        self,
        reason: BaiduEscalationReason,
        page_hash: str | None = None,
        input_fingerprint: str | None = None,
        alignment_status: str | None = None,
        page: int | None = None,
        *,
        _capability: object | None = None,
    ) -> None:
        if _capability is not _AUTHORIZATION_CAPABILITY:
            raise TypeError("Baidu escalation authorization requires a trusted factory")
        if not isinstance(reason, BaiduEscalationReason):
            raise ValueError("Baidu escalation reason is invalid")
        if not all(isinstance(value, str) and value for value in (page_hash, input_fingerprint)):
            raise ValueError("Baidu escalation context is invalid")
        if alignment_status not in {"consistent", "conflict", "complex"}:
            raise ValueError("Baidu escalation status is invalid")
        if not isinstance(page, int) or page < 1:
            raise ValueError("Baidu escalation page is invalid")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "page_hash", page_hash)
        object.__setattr__(self, "input_fingerprint", input_fingerprint)
        object.__setattr__(self, "alignment_status", alignment_status)
        object.__setattr__(self, "page", page)


def _issue_baidu_escalation_authorization(
    reason: BaiduEscalationReason,
    *,
    page_hash: str,
    input_fingerprint: str,
    alignment_status: str,
    page: int,
) -> BaiduEscalationAuthorization:
    return BaiduEscalationAuthorization(
        reason,
        page_hash,
        input_fingerprint,
        alignment_status,
        page,
        _capability=_AUTHORIZATION_CAPABILITY,
    )


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

    def recognize(
        self,
        image: bytes,
        *,
        authorization: BaiduEscalationAuthorization | None = None,
        allow_network: bool = False,
        page_hash: str | None = None,
        input_fingerprint: str | None = None,
        page: int | None = None,
        alignment_status: str | None = None,
    ) -> dict:
        if not isinstance(image, bytes):
            raise BaiduOcrError("Baidu OCR image is invalid")
        if len(image) > self.max_image_bytes:
            raise BaiduOcrError("Baidu OCR image is too large")
        if authorization is not None and not isinstance(
            authorization, BaiduEscalationAuthorization
        ):
            raise BaiduOcrError("Baidu OCR unsupported escalation reason")
        if not isinstance(authorization, BaiduEscalationAuthorization):
            if allow_network:
                raise BaiduOcrError("Baidu OCR requires typed escalation authorization")
            raise BaiduOcrError("Baidu OCR network disabled")
        if not isinstance(authorization.reason, BaiduEscalationReason):
            raise BaiduOcrError("Baidu OCR unsupported escalation reason")
        if not _authorization_matches_context(
            authorization,
            page_hash=page_hash,
            input_fingerprint=input_fingerprint,
            page=page,
            alignment_status=alignment_status,
        ):
            raise BaiduOcrError("Baidu OCR authorization context mismatch")
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


def _authorization_matches_context(
    authorization: BaiduEscalationAuthorization,
    *,
    page_hash: str | None,
    input_fingerprint: str | None,
    page: int | None,
    alignment_status: str | None,
) -> bool:
    if not isinstance(page_hash, str) or not isinstance(input_fingerprint, str):
        return False
    if not isinstance(page, int) or not isinstance(alignment_status, str):
        return False
    return (
        hmac.compare_digest(authorization.page_hash, page_hash)
        and hmac.compare_digest(authorization.input_fingerprint, input_fingerprint)
        and authorization.page == page
        and authorization.alignment_status == alignment_status
        and (
            authorization.reason is BaiduEscalationReason.CONFLICT
            and alignment_status == "conflict"
            or authorization.reason is BaiduEscalationReason.COMPLEX
            and alignment_status == "complex"
            or authorization.reason is BaiduEscalationReason.SAMPLE
            and alignment_status == "consistent"
        )
    )


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
