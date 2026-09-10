from __future__ import annotations

import json
import sys
import time
from typing import BinaryIO, TextIO

from parsing_core.workbench.ocr.baidu import (
    _MAX_REQUEST_BODY_BYTES,
    _MAX_RESPONSE_BYTES,
    _TRANSPORT_HEADER_MAX,
    BaiduRequest,
    _parse_https_endpoint,
    _send_https_request_inline,
)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ValueError("truncated transport request")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_request(stream: BinaryIO) -> tuple[BaiduRequest, float, int]:
    header_size = int.from_bytes(_read_exact(stream, 4), "big")
    if not 0 < header_size <= _TRANSPORT_HEADER_MAX:
        raise ValueError("invalid transport header")
    header = json.loads(_read_exact(stream, header_size).decode("utf-8"))
    if not isinstance(header, dict) or set(header) != {
        "url",
        "headers",
        "body_length",
        "timeout",
        "max_response_bytes",
    }:
        raise ValueError("invalid transport header")
    url = header["url"]
    headers = header["headers"]
    body_length = header["body_length"]
    timeout = header["timeout"]
    max_response_bytes = header["max_response_bytes"]
    _parse_https_endpoint(url)
    if (
        not isinstance(headers, dict)
        or set(headers) != {"Authorization", "Content-Type"}
        or not isinstance(headers["Authorization"], str)
        or not headers["Authorization"].startswith("Bearer ")
        or len(headers["Authorization"]) > 520
        or headers["Content-Type"] != "application/json"
        or not isinstance(body_length, int)
        or not 0 <= body_length <= _MAX_REQUEST_BODY_BYTES
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 120
        or not isinstance(max_response_bytes, int)
        or not 0 < max_response_bytes <= _MAX_RESPONSE_BYTES
    ):
        raise ValueError("invalid transport request")
    body = _read_exact(stream, body_length)
    return BaiduRequest(url=url, headers=headers, body=body), float(timeout), max_response_bytes


def main(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    error_stream = stderr or sys.stderr
    try:
        request, timeout, max_response_bytes = _read_request(input_stream)
        deadline = time.monotonic() + timeout
        status, body = _send_https_request_inline(
            request,
            timeout=timeout,
            deadline=deadline,
            cancel_event=None,
            max_response_bytes=max_response_bytes,
        )
        if len(body) > max_response_bytes:
            raise ValueError("response too large")
        output_stream.write(f"OK {status}\n".encode("ascii"))
        output_stream.write(body)
        output_stream.flush()
        return 0
    except BaseException:
        error_stream.write("Baidu OCR transport failed\n")
        error_stream.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
