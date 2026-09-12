from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def api_error(status_code: int, code: str, **params: Any) -> HTTPException:
    return HTTPException(status_code, {"code": code, "params": params})
