from __future__ import annotations

import base64
import hmac
import http.client
import inspect
import json
import math
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self


class BaiduOcrError(RuntimeError):
    pass


class BaiduEscalationReason(StrEnum):
    CONFLICT = "conflict"
    COMPLEX = "complex"
    SAMPLE = "sample"


@dataclass(frozen=True, slots=True)
class _AuthorizationContext:
    reason: BaiduEscalationReason
    page_hash: str
    input_fingerprint: str
    alignment_status: str
    page: int


class BaiduEscalationAuthorization:
    __slots__ = ("__weakref__",)

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        raise TypeError("Baidu escalation authorization requires a trusted alignment decision")

    @classmethod
    def _from_alignment(
        cls,
        reason: BaiduEscalationReason,
        *,
        page_hash: str,
        input_fingerprint: str,
        alignment_status: str,
        page: int,
    ) -> BaiduEscalationAuthorization:
        caller = sys._getframe(1)
        if (
            caller.f_code.co_name != "authorize_baidu_escalation"
            or caller.f_globals.get("__name__") != "parsing_core.workbench.ocr.alignment"
        ):
            raise TypeError("Baidu escalation authorization requires a trusted alignment decision")
        context = _AuthorizationContext(
            reason, page_hash, input_fingerprint, alignment_status, page
        )
        _validate_authorization_context(context)
        authorization = object.__new__(cls)
        authorization_id = id(authorization)

        def remove_expired(
            _reference: weakref.ReferenceType[BaiduEscalationAuthorization],
            *,
            authorization_id: int = authorization_id,
        ) -> None:
            with _AUTHORIZATION_REGISTRY_LOCK:
                record = _AUTHORIZATION_REGISTRY.get(authorization_id)
                if record is not None and record[0] is _reference:
                    _AUTHORIZATION_REGISTRY.pop(authorization_id, None)

        reference = weakref.ref(authorization, remove_expired)
        with _AUTHORIZATION_REGISTRY_LOCK:
            _AUTHORIZATION_REGISTRY[authorization_id] = (reference, context)
        return authorization

    def __reduce__(self) -> tuple[Any, tuple[()]]:
        return (_unpickled_authorization, ())


_AUTHORIZATION_REGISTRY: dict[
    int, tuple[weakref.ReferenceType[BaiduEscalationAuthorization], _AuthorizationContext]
] = {}
_AUTHORIZATION_REGISTRY_LOCK = threading.Lock()


def _unpickled_authorization() -> BaiduEscalationAuthorization:
    return object.__new__(BaiduEscalationAuthorization)


def _validate_authorization_context(context: _AuthorizationContext) -> None:
    if not isinstance(context.reason, BaiduEscalationReason):
        raise ValueError("Baidu escalation reason is invalid")
    if not all(
        isinstance(value, str) and value for value in (context.page_hash, context.input_fingerprint)
    ):
        raise ValueError("Baidu escalation context is invalid")
    if context.alignment_status not in {"consistent", "conflict", "complex"}:
        raise ValueError("Baidu escalation status is invalid")
    if not isinstance(context.page, int) or context.page < 1:
        raise ValueError("Baidu escalation page is invalid")


@dataclass(frozen=True)
class BaiduRequest:
    url: str
    headers: dict[str, str]
    body: bytes


Transport = Callable[..., tuple[int, bytes]]

_OFFICIAL_ENDPOINT = "https://qianfan.baidubce.com/v2/ocr/paddleocr"
_OFFICIAL_HOST = "qianfan.baidubce.com"
_OFFICIAL_PATH = "/v2/ocr/paddleocr"
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_REQUEST_BODY_BYTES = 4 * ((_MAX_IMAGE_BYTES + 2) // 3) + 4096
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_NORMALIZED_RESPONSE_BYTES = 1024 * 1024
_MAX_BAIDU_BLOCKS = 512
_MAX_BAIDU_BLOCK_TEXT = 8192
_MAX_RETRIES = 2
_TRANSPORT_HEADER_MAX = 64 * 1024
_TRANSPORT_POLL_SECONDS = 0.05
_TRANSPORT_CLEANUP_SECONDS = 1.0
_SENSITIVE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]+|Bearer\s+[^\s,}]+|/Users/[^\s,}]+|/var/[^\s,}]+|\b(?:key|token|access_token|api_key)\s*[:=]\s*[\"']?[^\s,}\"']+)",
    re.IGNORECASE,
)
_JSON_SECRET = re.compile(
    r"(\"?(?:key|token|access_token|api_key)\"?\s*:\s*\")([^\"]*)(\")", re.IGNORECASE
)
_REMOTE_URL = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")


class BaiduOcrClient:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = _OFFICIAL_ENDPOINT,
        timeout: float = 30.0,
        max_image_bytes: int = _MAX_IMAGE_BYTES,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        max_retries: int = _MAX_RETRIES,
        transport: Transport | None = None,
    ):
        if not api_key or len(api_key) > 512:
            raise ValueError("Baidu API key is required")
        endpoint_parts = _parse_https_endpoint(endpoint)
        if timeout <= 0 or timeout > 120:
            raise ValueError("invalid Baidu timeout")
        if not 0 < max_image_bytes <= _MAX_IMAGE_BYTES:
            raise ValueError("invalid Baidu size limit")
        if not 0 < max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise ValueError("invalid Baidu size limit")
        if max_retries < 0 or max_retries > _MAX_RETRIES:
            raise ValueError("invalid Baidu retry limit")
        self._api_key = api_key
        self.endpoint = _OFFICIAL_ENDPOINT
        self._endpoint_host = endpoint_parts.hostname
        self._endpoint_port = endpoint_parts.port
        self._endpoint_target = endpoint_parts.path or "/"
        if endpoint_parts.query:
            self._endpoint_target += f"?{endpoint_parts.query}"
        self.timeout = timeout
        self.max_image_bytes = max_image_bytes
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.transport = transport
        self._transport_mode = _transport_signature_mode(transport)

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
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        effective_deadline = _effective_deadline(self.timeout, deadline)
        _check_execution_control(effective_deadline, cancel_event)
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
        authorization_id = id(authorization)
        with _AUTHORIZATION_REGISTRY_LOCK:
            record = _AUTHORIZATION_REGISTRY.get(authorization_id)
            if record is None or record[0]() is not authorization:
                raise BaiduOcrError("Baidu OCR authorization is not registered")
            if not _authorization_matches_context(
                record[1],
                page_hash=page_hash,
                input_fingerprint=input_fingerprint,
                page=page,
                alignment_status=alignment_status,
            ):
                raise BaiduOcrError("Baidu OCR authorization context mismatch")
            _AUTHORIZATION_REGISTRY.pop(authorization_id)
        request_payload = {
            "model": "pp-structurev3",
            "file": base64.b64encode(image).decode("ascii"),
            "fileType": 1,
            "layoutNms": True,
            "useDocOrientationClassify": True,
            "useDocUnwarping": True,
            "useTextlineOrientation": True,
            "useRegionDetection": True,
            "useChartRecognition": True,
            "useTableRecognition": True,
            "useFormulaRecognition": True,
            "useOcrResultsWithTableCells": True,
            "useTableOrientationClassify": True,
            "visualize": False,
        }
        request = BaiduRequest(
            url=self.endpoint,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            body=json.dumps(request_payload, separators=(",", ":")).encode("ascii"),
        )
        for attempt in range(self.max_retries + 1):
            _check_execution_control(effective_deadline, cancel_event)
            request_timeout = _remaining_timeout(effective_deadline)
            try:
                status, body = self._send(
                    request,
                    timeout=request_timeout,
                    deadline=effective_deadline,
                    cancel_event=cancel_event,
                )
            except BaiduOcrError:
                raise
            except Exception as exc:
                _check_execution_control(effective_deadline, cancel_event)
                if attempt < self.max_retries:
                    continue
                raise BaiduOcrError(redact_baidu_error(str(exc))) from None
            _check_execution_control(effective_deadline, cancel_event)
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
            return _normalize_pp_structure_response(value)
        raise BaiduOcrError("Baidu OCR request failed")

    def _send(
        self,
        request: BaiduRequest,
        *,
        timeout: float,
        deadline: float,
        cancel_event: Any | None,
    ) -> tuple[int, bytes]:
        if self.transport is not None:
            if self._transport_mode == "legacy":
                return self.transport(request)
            return self.transport(
                request,
                timeout=timeout,
                cancel_event=cancel_event,
            )
        return _send_request_subprocess(
            request,
            timeout=timeout,
            deadline=deadline,
            cancel_event=cancel_event,
            max_response_bytes=self.max_response_bytes,
        )


def _transport_signature_mode(transport: Transport | None) -> str | None:
    if transport is None:
        return None
    try:
        signature = inspect.signature(transport)
    except (TypeError, ValueError):
        raise ValueError("Baidu transport signature is invalid") from None
    request = object()
    try:
        signature.bind(request, timeout=1.0, cancel_event=None)
    except TypeError:
        try:
            signature.bind(request)
        except TypeError:
            raise ValueError("Baidu transport signature is invalid") from None
        return "legacy"
    return "modern"


def _normalize_pp_structure_response(value: dict[str, Any]) -> dict[str, Any]:
    try:
        if "error" in value or "error_code" in value:
            raise ValueError
        request_id = value.get("id")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", request_id)
        ):
            raise ValueError
        result = value.get("result")
        if not isinstance(result, dict):
            raise ValueError
        data_info = result.get("dataInfo")
        if not isinstance(data_info, dict) or set(data_info) != {"type", "width", "height"}:
            raise ValueError
        width = data_info.get("width")
        height = data_info.get("height")
        if (
            data_info.get("type") != "image"
            or not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or not 1 <= width <= 100_000
            or not 1 <= height <= 100_000
        ):
            raise ValueError
        layout_results = result.get("layoutParsingResults")
        if not isinstance(layout_results, list) or len(layout_results) != 1:
            raise ValueError
        layout = layout_results[0]
        if not isinstance(layout, dict):
            raise ValueError
        pruned = layout.get("prunedResult")
        if not isinstance(pruned, dict):
            raise ValueError
        raw_blocks = pruned.get("parsing_res_list")
        if not isinstance(raw_blocks, list) or not 1 <= len(raw_blocks) <= _MAX_BAIDU_BLOCKS:
            raise ValueError

        blocks = [
            _normalize_baidu_block(raw, pruned, index, width, height)
            for index, raw in enumerate(raw_blocks, start=1)
        ]
        normalized = {
            "engine": "baidu_pp_structure",
            "request_id": request_id,
            "data_info": {"type": "image", "width": width, "height": height},
            "blocks": blocks,
        }
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_NORMALIZED_RESPONSE_BYTES:
            raise ValueError
        return normalized
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BaiduOcrError("Baidu OCR response is invalid") from None


def _normalize_baidu_block(
    value: object,
    pruned: dict[str, Any],
    index: int,
    page_width: int,
    page_height: int,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "block_label",
        "block_content",
        "block_bbox",
        "block_id",
        "block_order",
    }:
        raise ValueError
    label = value["block_label"]
    content = value["block_content"]
    block_id = value["block_id"]
    block_order = value["block_order"]
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= 64
        or not isinstance(content, str)
        or len(content) > _MAX_BAIDU_BLOCK_TEXT
        or not isinstance(block_id, int)
        or isinstance(block_id, bool)
        or not 0 <= block_id <= 1_000_000
        or (block_order is not None and (not isinstance(block_order, int) or block_order < 0))
    ):
        raise ValueError
    text = _REMOTE_URL.sub("", content).strip()
    if not text:
        raise ValueError
    x1, y1, x2, y2 = _validated_baidu_bbox(value["block_bbox"], page_width, page_height)
    confidence = _baidu_block_confidence(pruned, index - 1)
    return {
        "id": f"baidu-{block_id}",
        "type": _baidu_block_type(label),
        "text": text,
        "bounding_box": {
            "x": x1 / page_width,
            "y": y1 / page_height,
            "width": (x2 - x1) / page_width,
            "height": (y2 - y1) / page_height,
        },
        "reading_order": block_order if block_order is not None else index,
        "confidence": confidence,
        "source_label": label,
    }


def _validated_baidu_bbox(
    value: object, page_width: int, page_height: int
) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError
    if any(
        not isinstance(item, int | float)
        or isinstance(item, bool)
        or not math.isfinite(float(item))
        for item in value
    ):
        raise ValueError
    x1, y1, x2, y2 = (float(item) for item in value)
    if not (0 <= x1 < x2 <= page_width and 0 <= y1 < y2 <= page_height):
        raise ValueError
    return x1, y1, x2, y2


def _baidu_block_confidence(pruned: dict[str, Any], index: int) -> float | None:
    detection = pruned.get("layout_det_res")
    if not isinstance(detection, dict):
        return None
    boxes = detection.get("boxes")
    if not isinstance(boxes, list) or index >= len(boxes):
        return None
    box = boxes[index]
    if not isinstance(box, dict):
        raise ValueError
    score = box.get("score")
    if (
        not isinstance(score, int | float)
        or isinstance(score, bool)
        or not math.isfinite(float(score))
        or not 0 <= score <= 1
    ):
        raise ValueError
    return float(score)


def _baidu_block_type(label: str) -> str:
    lowered = label.casefold()
    return {
        "title": "title",
        "heading": "heading",
        "table": "table",
        "formula": "formula",
        "image": "image",
        "figure": "image",
        "list": "list",
        "footnote": "footnote",
        "page_number": "page_number",
    }.get(lowered, "paragraph")


def _send_https_request_inline(
    request: BaiduRequest,
    *,
    timeout: float,
    deadline: float,
    cancel_event: Any | None,
    max_response_bytes: int,
) -> tuple[int, bytes]:
    endpoint = _parse_https_endpoint(request.url)
    host = endpoint.hostname
    if host is None:
        raise BaiduOcrError("Baidu OCR endpoint is invalid")
    target = endpoint.path or "/"
    if endpoint.query:
        target += f"?{endpoint.query}"
    _check_execution_control(deadline, cancel_event)
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(
        host,
        endpoint.port,
        timeout=min(timeout, _remaining_timeout(deadline)),
        context=context,
    )
    control_stop, control_thread = _watch_connection_control(
        connection,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    try:
        _check_execution_control(deadline, cancel_event)
        connection.connect()
        _set_remaining_socket_timeout(connection, deadline, cancel_event)
        connection.request(
            "POST",
            target,
            body=request.body,
            headers=request.headers,
        )
        _check_execution_control(deadline, cancel_event)
        _set_remaining_socket_timeout(connection, deadline, cancel_event)
        response = connection.getresponse()
        _check_execution_control(deadline, cancel_event)
        body = _read_bounded(
            response,
            max_response_bytes,
            connection=connection,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        return int(response.status), body
    finally:
        control_stop.set()
        try:
            connection.close()
        finally:
            control_thread.join()


def _send_request_subprocess(
    request: BaiduRequest,
    *,
    timeout: float,
    deadline: float,
    cancel_event: Any | None,
    max_response_bytes: int,
    command: tuple[str, ...] | None = None,
) -> tuple[int, bytes]:
    _check_execution_control(deadline, cancel_event)
    header = json.dumps(
        {
            "url": request.url,
            "headers": request.headers,
            "body_length": len(request.body),
            "timeout": min(timeout, _remaining_timeout(deadline)),
            "max_response_bytes": max_response_bytes,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header) > _TRANSPORT_HEADER_MAX:
        raise BaiduOcrError("Baidu OCR transport request is invalid")
    payload = len(header).to_bytes(4, "big") + header + request.body
    worker_command = command or (
        sys.executable,
        "-s",
        "-m",
        "parsing_core.workbench.ocr.baidu_transport",
    )
    process = subprocess.Popen(
        worker_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        env=_transport_environment(),
    )
    process_group_id = _verify_transport_process_group(process)
    send_payload: bytes | None = payload
    try:
        while True:
            _check_execution_control(deadline, cancel_event)
            wait_seconds = min(_TRANSPORT_POLL_SECONDS, _remaining_timeout(deadline))
            try:
                stdout, _stderr = process.communicate(
                    input=send_payload,
                    timeout=wait_seconds,
                )
                break
            except subprocess.TimeoutExpired:
                send_payload = None
        if process.returncode != 0:
            raise BaiduOcrError("Baidu OCR transport failed")
        if len(stdout) > max_response_bytes + 32:
            raise BaiduOcrError("Baidu OCR response is too large")
        status_line, separator, body = stdout.partition(b"\n")
        if separator != b"\n" or not status_line.startswith(b"OK "):
            raise BaiduOcrError("Baidu OCR transport returned invalid response")
        try:
            status = int(status_line[3:].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            raise BaiduOcrError("Baidu OCR transport returned invalid response") from None
        if not 100 <= status <= 599:
            raise BaiduOcrError("Baidu OCR transport returned invalid response")
        return status, body
    except BaseException:
        _terminate_transport_process(process, process_group_id)
        raise
    finally:
        _close_transport_pipes(process)


def _transport_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _verify_transport_process_group(process: subprocess.Popen[bytes]) -> int:
    try:
        process_group_id = os.getpgid(process.pid)
    except OSError:
        process.wait(timeout=_TRANSPORT_CLEANUP_SECONDS)
        raise BaiduOcrError("Baidu OCR transport failed") from None
    if process_group_id != process.pid or process_group_id == os.getpgrp():
        process.terminate()
        process.wait(timeout=_TRANSPORT_CLEANUP_SECONDS)
        raise BaiduOcrError("Baidu OCR transport isolation failed")
    return process_group_id


def _terminate_transport_process(
    process: subprocess.Popen[bytes],
    process_group_id: int,
) -> None:
    if process.poll() is None:
        try:
            if os.getpgid(process.pid) == process_group_id:
                os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()
    try:
        process.wait(timeout=_TRANSPORT_CLEANUP_SECONDS / 2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()
        try:
            process.wait(timeout=_TRANSPORT_CLEANUP_SECONDS / 2)
        except subprocess.TimeoutExpired:
            raise BaiduOcrError("Baidu OCR transport cleanup failed") from None
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return
    except OSError:
        return
    raise BaiduOcrError("Baidu OCR transport cleanup failed")


def _close_transport_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _effective_deadline(timeout: float, deadline: float | None) -> float:
    local_deadline = time.monotonic() + timeout
    return local_deadline if deadline is None else min(local_deadline, deadline)


def _parse_https_endpoint(endpoint: str) -> urllib.parse.SplitResult:
    if not isinstance(endpoint, str) or not endpoint or any(ord(char) < 32 for char in endpoint):
        raise ValueError("Baidu endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError("Baidu endpoint is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _OFFICIAL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.path != _OFFICIAL_PATH
    ):
        raise ValueError("Baidu endpoint is invalid")
    if port not in (None, 443):
        raise ValueError("Baidu endpoint is invalid")
    return parsed


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BaiduOcrError("Baidu OCR timed out")
    return remaining


def _check_execution_control(deadline: float, cancel_event: Any | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise BaiduOcrError("Baidu OCR cancelled")
    if time.monotonic() >= deadline:
        raise BaiduOcrError("Baidu OCR timed out")


def _authorization_matches_context(
    context: _AuthorizationContext,
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
        hmac.compare_digest(context.page_hash, page_hash)
        and hmac.compare_digest(context.input_fingerprint, input_fingerprint)
        and context.page == page
        and context.alignment_status == alignment_status
        and (
            context.reason is BaiduEscalationReason.CONFLICT
            and alignment_status == "conflict"
            or context.reason is BaiduEscalationReason.COMPLEX
            and alignment_status == "complex"
            or context.reason is BaiduEscalationReason.SAMPLE
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


def _set_remaining_socket_timeout(
    connection: http.client.HTTPSConnection,
    deadline: float,
    cancel_event: Any | None,
) -> None:
    _check_execution_control(deadline, cancel_event)
    if connection.sock is not None:
        connection.sock.settimeout(_remaining_timeout(deadline))


def _watch_connection_control(
    connection: http.client.HTTPSConnection,
    *,
    deadline: float,
    cancel_event: Any | None,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            cancelled = cancel_event is not None and cancel_event.is_set()
            remaining = deadline - time.monotonic()
            if cancelled or remaining <= 0:
                try:
                    connection.close()
                except Exception:
                    pass
                return
            stop.wait(min(0.05, remaining))

    thread = threading.Thread(
        target=watch,
        name="baidu-transport-control",
        daemon=False,
    )
    thread.start()
    return stop, thread


def _read_bounded(
    stream: http.client.HTTPResponse,
    limit: int,
    *,
    connection: http.client.HTTPSConnection,
    deadline: float,
    cancel_event: Any | None,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        _check_execution_control(deadline, cancel_event)
        _set_remaining_socket_timeout(connection, deadline, cancel_event)
        chunk = stream.read(min(64 * 1024, limit + 1 - size))
        _check_execution_control(deadline, cancel_event)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise BaiduOcrError("Baidu OCR response is too large")
    return b"".join(chunks)


def _response_detail(body: bytes) -> str:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "remote error"
    if not isinstance(value, dict):
        return "remote error"
    detail = value.get("error_msg") or value.get("message") or "remote error"
    return str(detail)[:160]
