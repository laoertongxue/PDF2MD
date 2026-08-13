import re
import secrets
from collections.abc import Collection
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from parsing_core.serving.scheduler import Scheduler

SESSION_HEADER = "X-PDF2MD-Session"
SESSION_ENV = "PDF2MD_SESSION_TOKEN"
MIN_SESSION_TOKEN_BYTES = 32
WS_SESSION_PROTOCOL = "pdf2md-session-v1"
WS_SESSION_TOKEN_PREFIX = "pdf2md-session-token."
_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

_scheduler_singleton: Scheduler | None = None


def set_scheduler(sch: Scheduler) -> None:
    global _scheduler_singleton
    _scheduler_singleton = sch


def get_scheduler() -> Scheduler:
    assert _scheduler_singleton is not None, "Scheduler not initialized"
    return _scheduler_singleton


def validate_session_token(token: str) -> str:
    if len(token) < MIN_SESSION_TOKEN_BYTES or not _SESSION_TOKEN_RE.fullmatch(token):
        raise ValueError("session token must contain at least 32 bytes")
    return token


def session_token_matches(supplied: str, expected: str) -> bool:
    if not supplied:
        return False
    try:
        return secrets.compare_digest(supplied, expected)
    except TypeError:
        return False


def _request_header_values(request: Request, name: str) -> list[str]:
    expected = name.lower().encode("latin-1")
    return [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", [])
        if key.lower() == expected
    ]


def require_local_session(request: Request) -> None:
    expected = request.app.state.session_token
    values = _request_header_values(request, SESSION_HEADER)
    if len(values) > 1:
        raise HTTPException(status_code=400, detail={"code": "invalid_request"})
    supplied = values[0] if values else ""
    if not session_token_matches(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "session_required"})


def require_health_session(request: Request) -> None:
    require_local_session(request)
    origins = _request_header_values(request, "origin")
    if len(origins) > 1:
        raise HTTPException(status_code=400, detail={"code": "invalid_request"})
    origin = origins[0] if origins else None
    if origin is not None and origin not in request.app.state.allowed_origins:
        raise HTTPException(status_code=403, detail={"code": "origin_forbidden"})


def origin_is_allowed(origin: str | None, allowed_origins: Collection[str]) -> bool:
    return origin is not None and origin in allowed_origins


def websocket_session_from_protocols(header: str | None) -> str:
    protocols = [item.strip() for item in (header or "").split(",") if item.strip()]
    if WS_SESSION_PROTOCOL not in protocols:
        return ""
    tokens = [
        item.removeprefix(WS_SESSION_TOKEN_PREFIX)
        for item in protocols
        if item.startswith(WS_SESSION_TOKEN_PREFIX)
    ]
    if len(tokens) != 1:
        return ""
    return tokens[0]


SchedulerDep = Annotated[Scheduler, Depends(get_scheduler)]
