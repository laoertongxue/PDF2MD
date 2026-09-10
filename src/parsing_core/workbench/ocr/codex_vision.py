from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from ..secure_codex import (
    MAX_CODEX_EXECUTION_SECONDS,
    SecureCodexError,
    SecureCodexRunner,
    codex_exec_prefix,
    sanitize_codex_exception,
    validate_codex_argv,
)
from .atomic_io import rename_exclusive, sync_file_data
from .page_cache import CacheLimits


class CodexVisionError(RuntimeError):
    pass


class CodexCacheCommitError(CodexVisionError):
    committed = True

    def __init__(
        self,
        target: Path,
        *,
        durability_uncertain: bool,
        identity_uncertain: bool = False,
    ) -> None:
        if identity_uncertain:
            detail = "published identity is uncertain"
        elif durability_uncertain:
            detail = "durability is uncertain"
        else:
            detail = "post-commit cleanup failed"
        super().__init__(f"codex result cache committed but {detail}")
        self.target = target
        self.durability_uncertain = durability_uncertain
        self.identity_uncertain = identity_uncertain


_SCHEMA_DIR = Path(__file__).with_name("schemas")
_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 1024 * 1024
_MAX_RESULT_CACHE_BYTES = _MAX_RESULT_BYTES + 64 * 1024
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_INPUT_JSON_BYTES = 512 * 1024
_MAX_JSON_DEPTH = 24
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_IMAGES = 5
_SAFE_MESSAGES = frozenset(
    {
        "codex cli failed",
        "codex cli input is invalid",
        "codex cli input is too large",
        "codex cli is not available",
        "codex cli cancelled",
        "codex cli output exceeded limit",
        "codex cli result exceeded limit",
        "codex cli returned invalid json",
        "codex cli returned invalid schema",
        "codex cli timed out",
        "codex result cache capacity exceeded",
        "codex result cache scan exceeded limit",
        "codex result cache committed but durability is uncertain",
        "codex result cache committed but post-commit cleanup failed",
        "codex result cache committed but published identity is uncertain",
        "image input is not available",
        "too many crop images",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:openai|baidu|qianfan)?[_-]?(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"),
)
_LOCAL_REFERENCE = re.compile(
    r"(?i)(?:file://|(?<![A-Za-z0-9_.-])/(?:Users|private|tmp|var|etc|home)/|[A-Za-z]:[\\/])"
)


def _public_codex_vision_errors(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except CodexVisionError as exc:
            sanitize_codex_exception(exc, _safe_message(exc))
            raise

    return wrapped


def _load_schema_validator(name: str) -> Draft202012Validator:
    try:
        schema = json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))
        return Draft202012Validator(schema)
    except Exception:
        raise CodexVisionError("codex cli returned invalid schema") from None


_SCHEMA_VALIDATORS = {
    "transcription": _load_schema_validator("page-transcription.json"),
    "adjudication": _load_schema_validator("page-adjudication.json"),
}


@_public_codex_vision_errors
def validate_persisted_payload(
    payload: Any, *, kind: str, page: int, width: int, height: int
) -> None:
    """Re-validate a persisted Codex payload before it can be resumed."""
    expected = "transcription" if kind == "transcription" else "adjudication"
    if kind not in {"transcription", "adjudication"}:
        raise CodexVisionError("codex cli returned invalid schema")
    if not isinstance(payload, dict):
        raise CodexVisionError("codex cli returned invalid schema")
    _validate_result_payload(payload, expected, page, width, height)


@dataclass(frozen=True)
class CodexVisionResult:
    payload: dict[str, Any]
    record: dict[str, Any]


class CodexVisionExecutor:
    @_public_codex_vision_errors
    def __init__(
        self,
        *,
        codex_path: str | Path,
        temp_root: str | Path,
        trusted_image_root: str | Path,
        timeout: float = 60,
        deadline: float | None = None,
        cancel_event: Any | None = None,
        task_observer: Callable[[Path], None] | None = None,
        cache_limits: CacheLimits | None = None,
    ):
        try:
            self.timeout = _validated_timeout(timeout)
            _validated_deadline(deadline)
        except ValueError:
            raise CodexVisionError("codex cli failed") from None
        try:
            self._runner = SecureCodexRunner(
                codex_path,
                timeout=self.timeout,
                max_stdin_bytes=_MAX_PROMPT_BYTES,
                max_stdout_bytes=_MAX_STDOUT_BYTES,
                max_stderr_bytes=_MAX_STDERR_BYTES,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except SecureCodexError:
            raise CodexVisionError("codex cli is not available") from None
        self.codex_path = self._runner.path
        self.temp_root = _absolute_path(temp_root)
        self.result_cache_root = self.temp_root / "result-cache"
        self.trusted_image_root = _absolute_path(trusted_image_root)
        try:
            root_info = self.trusted_image_root.lstat()
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                raise CodexVisionError("image input is not available")
        except FileNotFoundError:
            raise CodexVisionError("image input is not available") from None
        self._cancel_event = cancel_event
        if task_observer is not None and not callable(task_observer):
            raise CodexVisionError("codex cli failed")
        self._task_observer = task_observer
        if cache_limits is not None and not isinstance(cache_limits, CacheLimits):
            raise CodexVisionError("codex cli failed")
        self._cache_limits = cache_limits or CacheLimits()
        self._identity = self._runner.identity
        self.codex_version = self._runner.codex_version

    @_public_codex_vision_errors
    def transcribe_page(
        self,
        page_image: str | Path,
        *,
        page_number: int,
        width: int,
        height: int,
        expected_image_sha256: str | None = None,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> CodexVisionResult:
        _validate_page(page_number, width, height)
        cancel_event = cancel_event or self._cancel_event
        effective_deadline = _effective_deadline(self.timeout, deadline)
        _check_execution_control(effective_deadline, cancel_event)
        prompt = _transcription_prompt(page_number, width, height)
        return self._run_with_retry(
            kind="transcription",
            schema_name="page-transcription.json",
            page_image=page_image,
            page_number=page_number,
            width=width,
            height=height,
            prompt=prompt,
            expected="transcription",
            crop_images=(),
            expected_image_sha256=expected_image_sha256,
            crop_expected_sha256=(),
            deadline=effective_deadline,
            cancel_event=cancel_event,
        )

    @_public_codex_vision_errors
    def adjudicate_page(
        self,
        page_image: str | Path,
        *,
        page_number: int,
        width: int,
        height: int,
        codex_observation: Any,
        apple_observation: Any,
        diff: Any,
        baidu_observation: Any | None = None,
        crop_images: list[str | Path] | tuple[str | Path, ...] = (),
        expected_image_sha256: str | None = None,
        crop_expected_sha256: list[str | None] | tuple[str | None, ...] = (),
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> CodexVisionResult:
        _validate_page(page_number, width, height)
        cancel_event = cancel_event or self._cancel_event
        effective_deadline = _effective_deadline(self.timeout, deadline)
        _check_execution_control(effective_deadline, cancel_event)
        if len(crop_images) > 4:
            raise CodexVisionError("too many crop images")
        if len(crop_expected_sha256) not in (0, len(crop_images)):
            raise CodexVisionError("codex cli input is invalid")
        payload = {
            "codex_observation": _bounded_json_value(codex_observation),
            "apple_observation": _bounded_json_value(apple_observation),
            "baidu_observation": (
                None if baidu_observation is None else _bounded_json_value(baidu_observation)
            ),
            "diff": _bounded_json_value(diff),
        }
        prompt = _adjudication_prompt(page_number, width, height, payload)
        return self._run_with_retry(
            kind="adjudication",
            schema_name="page-adjudication.json",
            page_image=page_image,
            page_number=page_number,
            width=width,
            height=height,
            prompt=prompt,
            expected="adjudication",
            crop_images=tuple(crop_images),
            expected_image_sha256=expected_image_sha256,
            crop_expected_sha256=tuple(crop_expected_sha256),
            deadline=effective_deadline,
            cancel_event=cancel_event,
        )

    def _run_with_retry(
        self,
        *,
        kind: str,
        schema_name: str,
        page_image: str | Path,
        page_number: int,
        width: int,
        height: int,
        prompt: str,
        expected: str,
        crop_images: tuple[str | Path, ...],
        expected_image_sha256: str | None,
        crop_expected_sha256: tuple[str | None, ...],
        deadline: float,
        cancel_event: Any | None,
    ) -> CodexVisionResult:
        last_error: CodexVisionError | None = None
        for attempt in (1, 2):
            _check_execution_control(deadline, cancel_event)
            try:
                payload, record = self._run_once(
                    kind=kind,
                    schema_name=schema_name,
                    page_image=page_image,
                    page_number=page_number,
                    width=width,
                    height=height,
                    prompt=prompt,
                    expected=expected,
                    crop_images=crop_images,
                    expected_image_sha256=expected_image_sha256,
                    crop_expected_sha256=crop_expected_sha256,
                    attempt=attempt,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                record["attempts"] = attempt
                return CodexVisionResult(payload=payload, record=record)
            except CodexCacheCommitError:
                raise
            except CodexVisionError as exc:
                if str(exc) != "codex cli returned invalid schema" or attempt == 2:
                    raise CodexVisionError(_safe_message(exc)) from None
                last_error = exc
        raise CodexVisionError(_safe_message(last_error or CodexVisionError("codex cli failed")))

    def _run_once(
        self,
        *,
        kind: str,
        schema_name: str,
        page_image: str | Path,
        page_number: int,
        width: int,
        height: int,
        prompt: str,
        expected: str,
        crop_images: tuple[str | Path, ...],
        attempt: int,
        expected_image_sha256: str | None,
        crop_expected_sha256: tuple[str | None, ...],
        deadline: float,
        cancel_event: Any | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        _check_execution_control(deadline, cancel_event)
        self._verify_identity()
        _ensure_prompt_bounded(prompt)
        _ensure_safe_cache_parent(self.temp_root)
        try:
            with self._runner.private_task() as job_dir:
                if self._task_observer is not None:
                    try:
                        self._task_observer(job_dir)
                    except Exception:
                        raise CodexVisionError("codex cli failed") from None
                _copy_verified_image(
                    page_image,
                    job_dir / "page.png",
                    trusted_root=self.trusted_image_root,
                    expected_sha256=expected_image_sha256,
                    expected_width=width,
                    expected_height=height,
                )
                _check_execution_control(deadline, cancel_event)
                for index, crop in enumerate(crop_images, start=1):
                    expected_hash = (
                        crop_expected_sha256[index - 1] if crop_expected_sha256 else None
                    )
                    _copy_verified_image(
                        crop,
                        job_dir / f"crop-{index}.png",
                        trusted_root=self.trusted_image_root,
                        expected_sha256=expected_hash,
                    )
                    _check_execution_control(deadline, cancel_event)
                schema_path = job_dir / schema_name
                shutil.copyfile(_SCHEMA_DIR / schema_name, schema_path)
                page_hash = _path_sha256(job_dir / "page.png")
                crop_hashes = [
                    _path_sha256(job_dir / f"crop-{index}.png")
                    for index in range(1, len(crop_images) + 1)
                ]
                cache_key = _input_cache_key(
                    kind=kind,
                    codex_version=self.codex_version,
                    codex_sha256=self._identity.sha256,
                    schema_name=schema_name,
                    schema_sha256=_path_sha256(schema_path),
                    page_number=page_number,
                    width=width,
                    height=height,
                    page_image_sha256=page_hash,
                    crop_image_sha256=crop_hashes,
                    prompt=prompt,
                )
                with _locked_result_cache(
                    self.result_cache_root,
                    cache_key,
                    deadline=deadline,
                    cancel_event=cancel_event,
                ) as cache_fd:
                    cached = _read_cached_result(
                        cache_fd,
                        cache_key=cache_key,
                        kind=kind,
                        expected=expected,
                        page=page_number,
                        width=width,
                        height=height,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        max_bytes=self._cache_limits.max_codex_result_bytes,
                    )
                    if cached is not None:
                        return cached, _codex_record(
                            kind=kind,
                            attempt=attempt,
                            page=page_number,
                            version=self.codex_version,
                            executable_sha256=self._identity.sha256,
                            schema_name=schema_name,
                            cache_key=cache_key,
                            cache_hit=True,
                        )
                    argv = _codex_argv(self.codex_path, schema_name, 1 + len(crop_images))
                    validate_codex_exec_argv([str(part) for part in argv])
                    stdout = self._communicate(
                        argv,
                        prompt,
                        job_dir,
                        deadline=deadline,
                        cancel_event=cancel_event,
                    )
                    if stdout.strip():
                        raise CodexVisionError("codex cli returned invalid json")
                    result_path = job_dir / "result.json"
                    payload = _read_result_json(
                        result_path, deadline=deadline, cancel_event=cancel_event
                    )
                    _validate_result_payload(payload, expected, page_number, width, height)
                    _publish_cached_result(
                        cache_fd,
                        cache_key=cache_key,
                        kind=kind,
                        payload=payload,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        limits=self._cache_limits,
                    )
                    return payload, _codex_record(
                        kind=kind,
                        attempt=attempt,
                        page=page_number,
                        version=self.codex_version,
                        executable_sha256=self._identity.sha256,
                        schema_name=schema_name,
                        cache_key=cache_key,
                        cache_hit=False,
                    )
        except SecureCodexError as exc:
            raise CodexVisionError(_safe_secure_runner_message(exc)) from None

    @_public_codex_vision_errors
    def _communicate(
        self,
        argv: list[str],
        prompt: str,
        cwd: Path,
        *,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> bytes:
        if not isinstance(prompt, str):
            raise CodexVisionError("codex cli input is invalid")
        _ensure_prompt_bounded(prompt)
        deadline = _effective_deadline(self.timeout, deadline)
        _check_execution_control(deadline, cancel_event)
        try:
            return cast(
                bytes,
                self._runner.run(
                    argv,
                    prompt.encode("utf-8"),
                    cwd=cwd,
                    deadline=deadline,
                    cancel_event=cancel_event,
                ),
            )
        except SecureCodexError as exc:
            raise CodexVisionError(_safe_secure_runner_message(exc)) from None

    @_public_codex_vision_errors
    def _verify_identity(self) -> None:
        try:
            self._runner.assert_current()
        except SecureCodexError:
            raise CodexVisionError("codex cli is not available") from None


def _safe_secure_runner_message(error: SecureCodexError) -> str:
    message = str(error)
    return message if message in _SAFE_MESSAGES else "codex cli failed"


def _effective_deadline(timeout: float, deadline: float | None) -> float:
    try:
        bounded_timeout = _validated_timeout(timeout)
        bounded_deadline = _validated_deadline(deadline)
    except ValueError:
        raise CodexVisionError("codex cli failed") from None
    local_deadline = time.monotonic() + bounded_timeout
    if not math.isfinite(local_deadline):
        raise CodexVisionError("codex cli failed")
    return local_deadline if bounded_deadline is None else min(local_deadline, bounded_deadline)


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid timeout")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_CODEX_EXECUTION_SECONDS:
        raise ValueError("invalid timeout")
    return timeout


def _validated_deadline(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid deadline")
    deadline = float(value)
    if not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("invalid deadline")
    now = time.monotonic()
    if not math.isfinite(now) or deadline - now > MAX_CODEX_EXECUTION_SECONDS:
        raise ValueError("invalid deadline")
    return deadline


def _check_execution_control(deadline: float, cancel_event: Any | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CodexVisionError("codex cli cancelled")
    if time.monotonic() >= deadline:
        raise CodexVisionError("codex cli timed out")


def validate_codex_exec_argv(argv: list[str]) -> None:
    if not isinstance(argv, list) or not argv:
        raise CodexVisionError("codex cli failed")
    try:
        validate_codex_argv(argv, Path(argv[0]))
    except (SecureCodexError, TypeError, ValueError):
        raise CodexVisionError("codex cli failed") from None


def _codex_argv(codex_path: Path, schema_name: str, image_count: int) -> list[str]:
    argv = codex_exec_prefix(codex_path) + ["--image", "page.png"]
    for index in range(1, image_count):
        argv.extend(["--image", f"crop-{index}.png"])
    argv.extend(["--output-schema", schema_name, "--output-last-message", "result.json", "-"])
    return argv


def _read_result_json(
    path: Path,
    *,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> dict[str, Any]:
    if deadline is None:
        deadline = time.monotonic() + 60
    _check_execution_control(deadline, cancel_event)
    directory_fd = None
    result_fd = None
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_result_file_stat(before)
        result_fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(result_fd)
        _validate_result_file_stat(opened)
        if _result_file_fingerprint(opened) != _result_file_fingerprint(before):
            raise CodexVisionError("codex cli returned invalid json")
        if opened.st_size > _MAX_RESULT_BYTES:
            raise CodexVisionError("codex cli result exceeded limit")

        chunks: list[bytes] = []
        size = 0
        while True:
            _check_execution_control(deadline, cancel_event)
            chunk = _read_result_chunk(result_fd, _MAX_RESULT_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_RESULT_BYTES:
                raise CodexVisionError("codex cli result exceeded limit")

        after = os.fstat(result_fd)
        path_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_result_file_stat(after)
        if (
            _result_file_fingerprint(after) != _result_file_fingerprint(opened)
            or _result_file_fingerprint(path_after) != _result_file_fingerprint(opened)
            or after.st_size != size
        ):
            raise CodexVisionError("codex cli returned invalid json")
        try:
            payload = json.loads(
                b"".join(chunks).decode("utf-8"), parse_constant=_reject_json_constant
            )
        except Exception:
            raise CodexVisionError("codex cli returned invalid json") from None
        if not isinstance(payload, dict):
            raise CodexVisionError("codex cli returned invalid schema")
        return payload
    except CodexVisionError:
        raise
    except (FileNotFoundError, OSError, ValueError, TypeError):
        raise CodexVisionError("codex cli returned invalid json") from None
    finally:
        if result_fd is not None:
            os.close(result_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _validate_result_file_stat(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CodexVisionError("codex cli returned invalid json")


def _result_file_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_result_chunk(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _validate_result_payload(
    payload: dict[str, Any], expected: str, page: int, width: int, height: int
) -> None:
    try:
        if _json_depth(payload) > _MAX_JSON_DEPTH:
            raise CodexVisionError("codex cli returned invalid schema")
        encoded = _json_bytes(payload)
        if len(encoded) > _MAX_RESULT_BYTES:
            raise CodexVisionError("codex cli result exceeded limit")
        validator = _SCHEMA_VALIDATORS[expected]
        errors = list(validator.iter_errors(payload))
        if errors:
            raise CodexVisionError("codex cli returned invalid schema")
        _validate_page_object(payload.get("page"), page, width, height)
        if expected == "transcription":
            _validate_blocks(payload["blocks"])
        else:
            _validate_blocks(payload["final_blocks"])
            _validate_conflicts(payload["resolved_conflicts"])
    except CodexVisionError:
        raise
    except Exception:
        raise CodexVisionError("codex cli returned invalid schema") from None


def _validate_page_object(value: Any, page: int, width: int, height: int) -> None:
    if not isinstance(value, dict) or set(value) != {"number", "width", "height"}:
        raise CodexVisionError("codex cli returned invalid schema")
    if value["number"] != page or value["width"] != width or value["height"] != height:
        raise CodexVisionError("codex cli returned invalid schema")


def _validate_blocks(value: Any) -> None:
    if not isinstance(value, list):
        raise CodexVisionError("codex cli returned invalid schema")
    for block in value:
        if not isinstance(block, dict):
            raise CodexVisionError("codex cli returned invalid schema")
        required = {
            "id",
            "type",
            "text",
            "region",
            "bounding_box",
            "candidates",
            "uncertainty_reason",
            "reading_order",
            "table",
            "formula",
            "source_region",
            "confidence",
        }
        if set(block) != required:
            raise CodexVisionError("codex cli returned invalid schema")
        if block["type"] not in {
            "title",
            "heading",
            "paragraph",
            "footnote",
            "page_number",
            "table",
            "formula",
            "caption",
            "image",
            "list",
        }:
            raise CodexVisionError("codex cli returned invalid schema")
        _validate_bbox(block["region"])
        _validate_bbox(block["bounding_box"])
        _validate_confidence(block["confidence"])
        if not isinstance(block["reading_order"], int) or block["reading_order"] < 0:
            raise CodexVisionError("codex cli returned invalid schema")


def _validate_conflicts(value: Any) -> None:
    if not isinstance(value, list):
        raise CodexVisionError("codex cli returned invalid schema")
    for conflict in value:
        if not isinstance(conflict, dict):
            raise CodexVisionError("codex cli returned invalid schema")
        if set(conflict) != {"id", "region", "evidence", "decision", "confidence"}:
            raise CodexVisionError("codex cli returned invalid schema")
        _validate_bbox(conflict["region"])
        if not isinstance(conflict["evidence"], list) or not conflict["evidence"]:
            raise CodexVisionError("codex cli returned invalid schema")
        _validate_confidence(conflict["confidence"])


def _validate_bbox(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise CodexVisionError("codex cli returned invalid schema")
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(number)
        ):
            raise CodexVisionError("codex cli returned invalid schema")
        if number < 0 or number > 1:
            raise CodexVisionError("codex cli returned invalid schema")
    if value["x"] + value["width"] > 1 or value["y"] + value["height"] > 1:
        raise CodexVisionError("codex cli returned invalid schema")


def _validate_confidence(value: Any) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise CodexVisionError("codex cli returned invalid schema")
    if value < 0 or value > 1:
        raise CodexVisionError("codex cli returned invalid schema")


def _transcription_prompt(page_number: int, width: int, height: int) -> str:
    return (
        "Machine OCR task for a single page image.\n"
        f"Page number: {page_number}. Image pixels: {width}x{height}.\n"
        "only transcribe visible content from the supplied image.\n"
        "Preserve reading order, tables, formulas, captions, footnotes, and page numbers.\n"
        "Do not infer context from earlier or later pages.\n"
        "mark invisible or inferred content as uncertain.\n"
        "Return only JSON matching the supplied schema."
    )


def _adjudication_prompt(page_number: int, width: int, height: int, payload: dict[str, Any]) -> str:
    evidence_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        "Machine OCR adjudication task for a single page image and optional crop images.\n"
        f"Page number: {page_number}. Image pixels: {width}x{height}.\n"
        "resolve conflicts using region-specific evidence from the visible images.\n"
        "do not decide by majority vote alone.\n"
        "Use Apple, Codex, optional Baidu observations, and machine diff only as bounded "
        "evidence.\n"
        "Return accepted, needs_review, or rejected; include evidence for every resolved "
        "conflict.\n"
        f"Bounded JSON evidence: {evidence_json}"
    )


def _copy_verified_image(
    source: str | Path,
    destination: Path,
    *,
    trusted_root: Path,
    expected_sha256: str | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> None:
    source_path = _absolute_path(source)
    try:
        relative = source_path.relative_to(trusted_root)
        if ".." in relative.parts:
            raise CodexVisionError("image input is not available")
        current_path = trusted_root
        for component in relative.parts:
            current_path = current_path / component
            if stat.S_ISLNK(current_path.lstat().st_mode):
                raise CodexVisionError("image input is not available")
    except (ValueError, FileNotFoundError):
        raise CodexVisionError("image input is not available") from None
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_sha256)
    ):
        raise CodexVisionError("image input is not available")
    fd = None
    try:
        link_info = source_path.lstat()
        if stat.S_ISLNK(link_info.st_mode) or not stat.S_ISREG(link_info.st_mode):
            raise CodexVisionError("image input is not available")
        fd = os.open(
            source_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(fd)
        current = os.stat(source_path, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise CodexVisionError("image input is not available")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CodexVisionError("image input is not available")
        if info.st_size <= 0 or info.st_size > _MAX_IMAGE_BYTES:
            raise CodexVisionError("image input is not available")
        image_format, image_width, image_height = _image_dimensions(fd)
        if image_format not in {"png", "jpeg"}:
            raise CodexVisionError("image input is not available")
        if expected_width is not None and image_width != expected_width:
            raise CodexVisionError("image input is not available")
        if expected_height is not None and image_height != expected_height:
            raise CodexVisionError("image input is not available")
        source_sha256 = _fd_sha256(fd)
        if expected_sha256 is not None and source_sha256 != expected_sha256.lower():
            raise CodexVisionError("image input is not available")
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as reader, destination.open("wb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
        destination.chmod(0o400)
        if destination.stat().st_size != info.st_size or _path_sha256(destination) != source_sha256:
            raise CodexVisionError("image input is not available")
    except CodexVisionError:
        raise
    except Exception:
        raise CodexVisionError("image input is not available") from None
    finally:
        if fd is not None:
            os.close(fd)


def _image_dimensions(fd: int) -> tuple[str, int, int]:
    os.lseek(fd, 0, os.SEEK_SET)
    header = os.read(fd, 32)
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24 and header[12:16] == b"IHDR":
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width > 0 and height > 0:
            return "png", width, height
    if header.startswith(b"\xff\xd8"):
        os.lseek(fd, 2, os.SEEK_SET)
        while True:
            marker = os.read(fd, 2)
            if len(marker) != 2:
                break
            if marker[0] != 0xFF:
                break
            while marker[1] == 0xFF:
                marker = bytes((marker[0], os.read(fd, 1)[0]))
            size_bytes = os.read(fd, 2)
            if len(size_bytes) != 2:
                break
            segment_size = int.from_bytes(size_bytes, "big")
            if marker[1] in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                segment = os.read(fd, 5)
                if len(segment) == 5:
                    return (
                        "jpeg",
                        int.from_bytes(segment[1:3], "big"),
                        int.from_bytes(segment[3:5], "big"),
                    )
                break
            if segment_size < 2:
                break
            os.lseek(fd, segment_size - 2, os.SEEK_CUR)
    raise CodexVisionError("image input is not available")


def _fd_sha256(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_json_value(value: Any) -> Any:
    if _json_depth(value) > _MAX_JSON_DEPTH:
        raise CodexVisionError("codex cli input is invalid")
    _reject_sensitive_json_strings(value)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except Exception:
        raise CodexVisionError("codex cli input is invalid") from None
    if len(encoded) > _MAX_INPUT_JSON_BYTES:
        raise CodexVisionError("codex cli input is too large")
    return json.loads(encoded.decode("utf-8"))


def _reject_sensitive_json_strings(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _sensitive_key(key):
                raise CodexVisionError("codex cli input is invalid")
            _reject_sensitive_json_strings(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_sensitive_json_strings(item)
        return
    if isinstance(value, str):
        stripped = value.strip()
        if (
            any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
            or _LOCAL_REFERENCE.search(value)
            or stripped.startswith(("../", "./", "~/"))
        ):
            raise CodexVisionError("codex cli input is invalid")


def _sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    exact_names = {
        "filename",
        "file_uri",
        "course_dir",
        "book_dir",
        "original_text",
        "raw_document",
        "api_key",
        "keychain",
    }
    if lowered in exact_names:
        return True
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold()
    tokens = {token for token in re.split(r"[^a-z0-9]+", separated) if token}
    return bool(
        tokens
        & {
            "key",
            "path",
            "token",
            "secret",
            "password",
            "authorization",
            "uri",
            "url",
        }
    )


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        return depth
    if isinstance(value, dict):
        if not value:
            return depth + 1
        return max(_json_depth(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        if not value:
            return depth + 1
        return max(_json_depth(item, depth + 1) for item in value)
    return depth + 1


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except Exception:
        raise CodexVisionError("codex cli returned invalid schema") from None


def _ensure_prompt_bounded(prompt: str) -> None:
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise CodexVisionError("codex cli input is too large")


def _validate_page(page_number: int, width: int, height: int) -> None:
    for value in (page_number, width, height):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise CodexVisionError("codex cli input is invalid")


def _ensure_safe_cache_parent(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
    except OSError:
        raise CodexVisionError("codex cli failed") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CodexVisionError("codex cli failed")


_RESULT_CACHE_ENTRY = re.compile(r"(?P<key>[0-9a-f]{64})\.json\Z")
_RESULT_CACHE_TEMPORARY = re.compile(r"\.(?P<key>[0-9a-f]{64})\.[0-9]+\.[0-9]+\.tmp\Z")
_RESULT_CACHE_AUX_TEMPORARY = re.compile(r"\.(?:cleanup|evict)-[0-9a-f]+\.tmp\Z")
_RESULT_CACHE_LOCK_OBJECT = re.compile(r"\.entry-(?P<slot>[0-9]{2})\.lock\Z")
_RESULT_QUOTA_STATE_NAME = ".quota-state"
_RESULT_QUOTA_STATE_MAGIC = b"PDF2Q002"
_RESULT_QUOTA_STATE = struct.Struct(">8sQQQQQQ")
_RESULT_RESERVATION_MAGIC = b"PDF2R002"
_RESULT_RESERVATION_RECORD = struct.Struct(">8sQQqQ32s16s")
_RESULT_LOCK_SLOTS = 64


@dataclass(frozen=True)
class _ResultCacheEntry:
    name: str
    cache_key: str | None
    size: int
    accessed_ns: int
    identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _ResultCacheScan:
    total_bytes: int
    total_entries: int
    candidates: tuple[_ResultCacheEntry, ...]
    payload_bytes: int
    payload_entries: int


@dataclass(frozen=True)
class _ResultQuotaState:
    committed_bytes: int
    committed_entries: int
    reserved_bytes: int
    reserved_entries: int
    generation: int
    directory_mtime_ns: int


@dataclass(frozen=True)
class _PersistedResultReservation:
    generation: int
    reserved_bytes: int
    delta_bytes: int
    entry_delta: int
    cache_key: str
    reservation_id: bytes


@dataclass
class _ResultCacheReservation:
    generation: int
    reserved_bytes: int
    delta_bytes: int
    entry_delta: int
    slot: int
    reservation_id: bytes
    active: bool = True


@contextmanager
def _locked_result_quota(
    directory_fd: int,
    *,
    deadline: float,
    cancel_event: Any | None,
) -> Iterator[int]:
    state_fd = -1
    primary_pending = False
    try:
        state_fd = os.open(
            _RESULT_QUOTA_STATE_NAME,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        info = os.fstat(state_fd)
        path_info = os.stat(
            _RESULT_QUOTA_STATE_NAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or _result_identity(info) != _result_identity(path_info)
        ):
            raise OSError
        while True:
            _check_execution_control(deadline, cancel_event)
            try:
                fcntl.flock(state_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        yield state_fd
    except CodexVisionError:
        primary_pending = True
        raise
    except OSError:
        primary_pending = True
        raise CodexVisionError("codex cli failed") from None
    finally:
        if state_fd >= 0:
            closing_fd = state_fd
            state_fd = -1
            try:
                fcntl.flock(closing_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(closing_fd)
            except OSError:
                if not primary_pending:
                    raise CodexVisionError("codex cli failed") from None


def _scan_result_cache(
    directory_fd: int,
    limits: CacheLimits,
    *,
    collect_candidates: bool = True,
) -> _ResultCacheScan:
    return _inspect_result_cache(
        directory_fd,
        limits,
        collect_candidates=collect_candidates,
    )


def _measure_result_cache(directory_fd: int, limits: CacheLimits) -> _ResultCacheScan:
    return _inspect_result_cache(directory_fd, limits, collect_candidates=False)


def _inspect_result_cache(
    directory_fd: int,
    limits: CacheLimits,
    *,
    collect_candidates: bool,
) -> _ResultCacheScan:
    total_bytes = 0
    entries_seen = 0
    payload_bytes = 0
    payload_entries = 0
    candidates: list[_ResultCacheEntry] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > limits.max_scan_entries:
                    raise CodexVisionError("codex result cache scan exceeded limit")
                match = _RESULT_CACHE_ENTRY.fullmatch(entry.name)
                if match is None:
                    match = _RESULT_CACHE_TEMPORARY.fullmatch(entry.name)
                auxiliary = _RESULT_CACHE_AUX_TEMPORARY.fullmatch(entry.name)
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                total_bytes += max(0, info.st_size)
                lock_match = _RESULT_CACHE_LOCK_OBJECT.fullmatch(entry.name)
                control_object = entry.name == _RESULT_QUOTA_STATE_NAME or (
                    lock_match is not None and int(lock_match.group("slot")) < _RESULT_LOCK_SLOTS
                )
                if not control_object:
                    payload_bytes += max(0, info.st_size)
                    payload_entries += 1
                if (
                    collect_candidates
                    and (match is not None or auxiliary is not None)
                    and stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1
                    and info.st_uid == os.geteuid()
                    and not stat.S_IMODE(info.st_mode) & 0o077
                ):
                    candidates.append(
                        _ResultCacheEntry(
                            name=entry.name,
                            cache_key=match.group("key") if match is not None else None,
                            size=info.st_size,
                            accessed_ns=info.st_mtime_ns,
                            identity=_result_identity(info),
                        )
                    )
    except CodexVisionError:
        raise
    except OSError:
        raise CodexVisionError("codex cli failed") from None
    return _ResultCacheScan(
        total_bytes,
        entries_seen,
        tuple(candidates),
        payload_bytes,
        payload_entries,
    )


def _read_result_quota_state(state_fd: int) -> _ResultQuotaState | None:
    try:
        info = os.fstat(state_fd)
        if info.st_size != _RESULT_QUOTA_STATE.size:
            return None
        encoded = os.pread(state_fd, _RESULT_QUOTA_STATE.size, 0)
        if len(encoded) != _RESULT_QUOTA_STATE.size:
            return None
        (
            magic,
            committed_bytes,
            committed_entries,
            reserved_bytes,
            reserved_entries,
            generation,
            directory_mtime_ns,
        ) = _RESULT_QUOTA_STATE.unpack(encoded)
        if magic != _RESULT_QUOTA_STATE_MAGIC or generation < 1:
            return None
        return _ResultQuotaState(
            committed_bytes,
            committed_entries,
            reserved_bytes,
            reserved_entries,
            generation,
            directory_mtime_ns,
        )
    except (OSError, struct.error):
        return None


def _write_result_quota_state(
    state_fd: int,
    state: _ResultQuotaState,
) -> None:
    values = (
        state.committed_bytes,
        state.committed_entries,
        state.reserved_bytes,
        state.reserved_entries,
        state.generation,
        state.directory_mtime_ns,
    )
    if any(value < 0 for value in values) or state.generation < 1:
        raise CodexVisionError("codex cli failed")
    try:
        encoded = _RESULT_QUOTA_STATE.pack(_RESULT_QUOTA_STATE_MAGIC, *values)
        os.ftruncate(state_fd, 0)
        os.lseek(state_fd, 0, os.SEEK_SET)
        offset = 0
        while offset < len(encoded):
            written = os.write(state_fd, encoded[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(state_fd)
    except (OSError, struct.error, OverflowError):
        raise CodexVisionError("codex cli failed") from None


def _read_result_reservation(slot_fd: int) -> _PersistedResultReservation | None:
    try:
        info = os.fstat(slot_fd)
        if info.st_size == 0:
            return None
        if info.st_size != _RESULT_RESERVATION_RECORD.size:
            raise CodexVisionError("codex cli failed")
        encoded = os.pread(slot_fd, _RESULT_RESERVATION_RECORD.size, 0)
        if len(encoded) != _RESULT_RESERVATION_RECORD.size:
            raise CodexVisionError("codex cli failed")
        magic, generation, reserved, delta, entries, key, reservation_id = (
            _RESULT_RESERVATION_RECORD.unpack(encoded)
        )
        if (
            magic != _RESULT_RESERVATION_MAGIC
            or generation < 1
            or entries > 1
            or len(reservation_id) != 16
        ):
            raise CodexVisionError("codex cli failed")
        return _PersistedResultReservation(
            generation,
            reserved,
            delta,
            entries,
            key.hex(),
            reservation_id,
        )
    except CodexVisionError:
        raise
    except (OSError, struct.error, OverflowError):
        raise CodexVisionError("codex cli failed") from None


def _write_result_reservation(
    slot_fd: int,
    reservation: _PersistedResultReservation | None,
) -> None:
    try:
        encoded = b""
        if reservation is not None:
            encoded = _RESULT_RESERVATION_RECORD.pack(
                _RESULT_RESERVATION_MAGIC,
                reservation.generation,
                reservation.reserved_bytes,
                reservation.delta_bytes,
                reservation.entry_delta,
                bytes.fromhex(reservation.cache_key),
                reservation.reservation_id,
            )
        os.ftruncate(slot_fd, 0)
        if encoded:
            offset = 0
            while offset < len(encoded):
                written = os.pwrite(slot_fd, encoded[offset:], offset)
                if written <= 0:
                    raise OSError
                offset += written
        os.fsync(slot_fd)
    except (OSError, ValueError, struct.error, OverflowError):
        raise CodexVisionError("codex cli failed") from None


def _open_result_slot(directory_fd: int, slot: int) -> int:
    if not 0 <= slot < _RESULT_LOCK_SLOTS:
        raise CodexVisionError("codex cli failed")
    fd = -1
    try:
        fd = os.open(
            f".entry-{slot:02d}.lock",
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size not in {0, _RESULT_RESERVATION_RECORD.size}
        ):
            raise OSError
        result = fd
        fd = -1
        return result
    except OSError:
        raise CodexVisionError("codex cli failed") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _recover_result_reservations(
    directory_fd: int,
    *,
    current_slot: int | None,
) -> tuple[_PersistedResultReservation, ...]:
    active: list[_PersistedResultReservation] = []
    for slot in range(_RESULT_LOCK_SLOTS):
        slot_fd = _open_result_slot(directory_fd, slot)
        locked_here = False
        try:
            record = _read_result_reservation(slot_fd)
            if record is None:
                continue
            if current_slot is not None and slot == current_slot:
                _write_result_reservation(slot_fd, None)
                continue
            try:
                fcntl.flock(slot_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked_here = True
            except BlockingIOError:
                active.append(record)
                continue
            _write_result_reservation(slot_fd, None)
        finally:
            if locked_here:
                try:
                    fcntl.flock(slot_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(slot_fd)
    return tuple(active)


def _rebuild_result_quota_state(
    directory_fd: int,
    state: _ResultQuotaState | None,
    *,
    current_slot: int | None,
    limits: CacheLimits,
) -> tuple[_ResultQuotaState, _ResultCacheScan]:
    active = _recover_result_reservations(directory_fd, current_slot=current_slot)
    scan = _scan_result_cache(directory_fd, limits)
    committed_bytes = scan.payload_bytes
    # The quota state is one fixed control entry; the bounded lock slots are
    # coordination infrastructure and do not consume user payload quota.
    committed_entries = scan.payload_entries + 1
    active_keys = {reservation.cache_key for reservation in active}
    for candidate in scan.candidates:
        temporary = _RESULT_CACHE_TEMPORARY.fullmatch(candidate.name)
        if temporary is not None and temporary.group("key") in active_keys:
            committed_bytes = max(0, committed_bytes - candidate.size)
            committed_entries = max(0, committed_entries - 1)
    return (
        _ResultQuotaState(
            committed_bytes=committed_bytes,
            committed_entries=committed_entries,
            reserved_bytes=sum(item.reserved_bytes for item in active),
            reserved_entries=sum(item.entry_delta for item in active),
            generation=(state.generation + 1) if state is not None else 1,
            directory_mtime_ns=os.fstat(directory_fd).st_mtime_ns,
        ),
        scan,
    )


def _result_quota_exceeded(state: _ResultQuotaState, limits: CacheLimits) -> bool:
    return (
        state.committed_bytes + state.reserved_bytes > limits.max_codex_total_bytes
        or state.committed_entries + state.reserved_entries > limits.max_codex_cache_entries
    )


def _current_result_size(directory_fd: int, cache_key: str) -> int:
    try:
        info = os.stat(
            f"{cache_key}.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return 0
    except OSError:
        raise CodexVisionError("codex cli failed") from None
    return max(0, info.st_size) if stat.S_ISREG(info.st_mode) else 0


def _reserve_result_cache_capacity(
    directory_fd: int,
    *,
    cache_key: str,
    incoming_bytes: int,
    limits: CacheLimits,
    deadline: float,
    cancel_event: Any | None,
) -> _ResultCacheReservation:
    if (
        not isinstance(incoming_bytes, int)
        or isinstance(incoming_bytes, bool)
        or incoming_bytes < 0
        or incoming_bytes > limits.max_codex_result_bytes
    ):
        raise CodexVisionError("codex result cache capacity exceeded")
    _prepare_result_lock_slots(directory_fd)
    slot = _result_lock_slot(cache_key)
    with _locked_result_quota(
        directory_fd,
        deadline=deadline,
        cancel_event=cancel_event,
    ) as state_fd:
        state = _read_result_quota_state(state_fd)
        active = _recover_result_reservations(directory_fd, current_slot=slot)
        active_bytes = sum(item.reserved_bytes for item in active)
        active_entries = sum(item.entry_delta for item in active)
        directory_mtime_ns = os.fstat(directory_fd).st_mtime_ns
        scan: _ResultCacheScan | None = None
        if state is None or state.directory_mtime_ns != directory_mtime_ns:
            state, scan = _rebuild_result_quota_state(
                directory_fd,
                state,
                current_slot=slot,
                limits=limits,
            )
        elif state.reserved_bytes != active_bytes or state.reserved_entries != active_entries:
            state = _ResultQuotaState(
                state.committed_bytes,
                state.committed_entries,
                active_bytes,
                active_entries,
                state.generation + 1,
                directory_mtime_ns,
            )
        previous_size = _current_result_size(directory_fd, cache_key)
        delta_bytes = incoming_bytes - previous_size
        reserved_bytes = max(0, delta_bytes)
        entry_delta = 0 if previous_size else 1
        projected = _ResultQuotaState(
            state.committed_bytes,
            state.committed_entries,
            state.reserved_bytes + reserved_bytes,
            state.reserved_entries + entry_delta,
            state.generation + 1,
            directory_mtime_ns,
        )
        if _result_quota_exceeded(projected, limits):
            state, scan = _rebuild_result_quota_state(
                directory_fd,
                state,
                current_slot=slot,
                limits=limits,
            )
            previous_size = _current_result_size(directory_fd, cache_key)
            delta_bytes = incoming_bytes - previous_size
            reserved_bytes = max(0, delta_bytes)
            entry_delta = 0 if previous_size else 1
            projected = _ResultQuotaState(
                state.committed_bytes,
                state.committed_entries,
                state.reserved_bytes + reserved_bytes,
                state.reserved_entries + entry_delta,
                state.generation + 1,
                state.directory_mtime_ns,
            )
        removed_any = False
        if _result_quota_exceeded(projected, limits):
            assert scan is not None
            for candidate in sorted(
                scan.candidates,
                key=lambda item: (item.accessed_ns, item.name),
            ):
                if candidate.cache_key is not None and candidate.cache_key == cache_key:
                    continue
                removed = _evict_result_cache_entry(
                    directory_fd,
                    candidate,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                if removed:
                    removed_any = True
                    state = _ResultQuotaState(
                        max(0, state.committed_bytes - removed),
                        max(0, state.committed_entries - 1),
                        state.reserved_bytes,
                        state.reserved_entries,
                        state.generation,
                        state.directory_mtime_ns,
                    )
                    projected = _ResultQuotaState(
                        state.committed_bytes,
                        state.committed_entries,
                        state.reserved_bytes + reserved_bytes,
                        state.reserved_entries + entry_delta,
                        state.generation + 1,
                        state.directory_mtime_ns,
                    )
                    if not _result_quota_exceeded(projected, limits):
                        break
        if removed_any:
            try:
                os.fsync(directory_fd)
            except OSError:
                raise CodexVisionError("codex cli failed") from None
        if _result_quota_exceeded(projected, limits):
            _write_result_quota_state(
                state_fd,
                _ResultQuotaState(
                    state.committed_bytes,
                    state.committed_entries,
                    state.reserved_bytes,
                    state.reserved_entries,
                    state.generation + 1,
                    os.fstat(directory_fd).st_mtime_ns,
                ),
            )
            raise CodexVisionError("codex result cache capacity exceeded")
        generation = projected.generation
        reservation_id = secrets.token_bytes(16)
        persisted = _PersistedResultReservation(
            generation,
            reserved_bytes,
            delta_bytes,
            entry_delta,
            cache_key,
            reservation_id,
        )
        slot_fd = _open_result_slot(directory_fd, slot)
        try:
            _write_result_quota_state(state_fd, projected)
            try:
                _write_result_reservation(slot_fd, persisted)
            except BaseException:
                _write_result_quota_state(
                    state_fd,
                    _ResultQuotaState(
                        state.committed_bytes,
                        state.committed_entries,
                        state.reserved_bytes,
                        state.reserved_entries,
                        generation + 1,
                        os.fstat(directory_fd).st_mtime_ns,
                    ),
                )
                raise
        finally:
            os.close(slot_fd)
        return _ResultCacheReservation(
            generation,
            reserved_bytes,
            delta_bytes,
            entry_delta,
            slot,
            reservation_id,
        )


def _release_result_cache_capacity(
    directory_fd: int,
    reservation: _ResultCacheReservation,
    *,
    limits: CacheLimits,
    deadline: float,
    cancel_event: Any | None,
) -> None:
    if not reservation.active:
        return
    _finish_result_cache_capacity(
        directory_fd,
        reservation,
        commit=False,
        limits=limits,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def _commit_result_cache_capacity(
    directory_fd: int,
    reservation: _ResultCacheReservation,
    *,
    limits: CacheLimits,
    deadline: float,
    cancel_event: Any | None,
) -> None:
    if not reservation.active:
        return
    _finish_result_cache_capacity(
        directory_fd,
        reservation,
        commit=True,
        limits=limits,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def _finish_result_cache_capacity(
    directory_fd: int,
    reservation: _ResultCacheReservation,
    *,
    commit: bool,
    limits: CacheLimits,
    deadline: float,
    cancel_event: Any | None,
) -> None:
    with _locked_result_quota(
        directory_fd,
        deadline=deadline,
        cancel_event=cancel_event,
    ) as state_fd:
        state = _read_result_quota_state(state_fd)
        if state is None:
            state, _scan = _rebuild_result_quota_state(
                directory_fd,
                state,
                current_slot=None,
                limits=limits,
            )
        slot_fd = _open_result_slot(directory_fd, reservation.slot)
        try:
            persisted = _read_result_reservation(slot_fd)
            if (
                persisted is None
                or persisted.generation != reservation.generation
                or persisted.reserved_bytes != reservation.reserved_bytes
                or persisted.delta_bytes != reservation.delta_bytes
                or persisted.entry_delta != reservation.entry_delta
                or persisted.reservation_id != reservation.reservation_id
            ):
                raise CodexVisionError("codex cli failed")
            _write_result_reservation(slot_fd, None)
            committed_bytes = state.committed_bytes
            committed_entries = state.committed_entries
            if commit:
                committed_bytes = max(0, committed_bytes + reservation.delta_bytes)
                committed_entries += reservation.entry_delta
            updated = _ResultQuotaState(
                committed_bytes,
                committed_entries,
                max(0, state.reserved_bytes - reservation.reserved_bytes),
                max(0, state.reserved_entries - reservation.entry_delta),
                state.generation + 1,
                os.fstat(directory_fd).st_mtime_ns,
            )
            _write_result_quota_state(state_fd, updated)
        finally:
            os.close(slot_fd)
    reservation.active = False


def _open_result_entry_lock(
    directory_fd: int,
    cache_key: str,
) -> int | None:
    fd = -1
    try:
        fd = os.open(
            _result_lock_name(cache_key),
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise OSError
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = fd
        fd = -1
        return result
    except (BlockingIOError, OSError):
        return None
    finally:
        if fd >= 0:
            closing_fd = fd
            fd = -1
            try:
                os.close(closing_fd)
            except OSError:
                pass


def _evict_result_cache_entry(
    directory_fd: int,
    candidate: _ResultCacheEntry,
    *,
    deadline: float,
    cancel_event: Any | None,
) -> int:
    _check_execution_control(deadline, cancel_event)
    lock_fd = -1
    if candidate.cache_key is not None:
        acquired = _open_result_entry_lock(directory_fd, candidate.cache_key)
        if acquired is None:
            return 0
        lock_fd = acquired
    candidate_fd = -1
    try:
        try:
            candidate_fd = os.open(
                candidate.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            opened = os.fstat(candidate_fd)
            current = os.stat(
                candidate.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError:
            return 0
        if (
            _result_identity(opened) != candidate.identity
            or _result_identity(current) != candidate.identity
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            return 0
        quarantine = f".evict-{secrets.token_hex(16)}.tmp"
        try:
            rename_exclusive(Path(candidate.name), Path(quarantine), directory_fd)
        except OSError:
            return 0
        try:
            moved = os.stat(
                quarantine,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError:
            return 0
        if _result_bound_identity(moved) != candidate.identity[:4]:
            try:
                rename_exclusive(Path(quarantine), Path(candidate.name), directory_fd)
            except OSError:
                pass
            return 0
        try:
            os.unlink(quarantine, dir_fd=directory_fd)
        except OSError:
            return 0
        return candidate.size
    finally:
        if candidate_fd >= 0:
            closing_fd = candidate_fd
            candidate_fd = -1
            try:
                os.close(closing_fd)
            except OSError:
                pass
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            closing_lock_fd = lock_fd
            lock_fd = -1
            try:
                os.close(closing_lock_fd)
            except OSError:
                pass


def _result_lock_name(cache_key: str) -> str:
    return f".entry-{_result_lock_slot(cache_key):02d}.lock"


def _result_lock_slot(cache_key: str) -> int:
    digest = hashlib.sha256(cache_key.encode("ascii", errors="strict")).digest()
    return int.from_bytes(digest[:8], "big") % _RESULT_LOCK_SLOTS


def _prepare_result_lock_slots(directory_fd: int) -> None:
    for slot in range(_RESULT_LOCK_SLOTS):
        fd = -1
        try:
            fd = os.open(
                f".entry-{slot:02d}.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size not in {0, _RESULT_RESERVATION_RECORD.size}
            ):
                raise OSError
        except OSError:
            raise CodexVisionError("codex cli failed") from None
        finally:
            if fd >= 0:
                os.close(fd)


@contextmanager
def _locked_result_cache(
    root: Path,
    cache_key: str,
    *,
    deadline: float,
    cancel_event: Any | None,
) -> Iterator[int]:
    _ensure_safe_cache_parent(root.parent)
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        path_info = root.lstat()
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or path_info.st_uid != os.geteuid()
            or stat.S_IMODE(path_info.st_mode) & 0o077
        ):
            raise OSError
        directory_fd = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(directory_fd)
        if (path_info.st_dev, path_info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError
        with _locked_result_quota(
            directory_fd,
            deadline=deadline,
            cancel_event=cancel_event,
        ):
            _prepare_result_lock_slots(directory_fd)
    except OSError:
        raise CodexVisionError("codex cli failed") from None

    lock_fd = -1
    try:
        lock_fd = os.open(
            _result_lock_name(cache_key),
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        lock_info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_nlink != 1
            or lock_info.st_uid != os.geteuid()
            or stat.S_IMODE(lock_info.st_mode) & 0o077
        ):
            raise OSError
        while True:
            _check_execution_control(deadline, cancel_event)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        yield directory_fd
    except CodexVisionError:
        raise
    except OSError:
        raise CodexVisionError("codex cli failed") from None
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        os.close(directory_fd)


def _read_cached_result(
    directory_fd: int,
    *,
    cache_key: str,
    kind: str,
    expected: str,
    page: int,
    width: int,
    height: int,
    deadline: float,
    cancel_event: Any | None,
    max_bytes: int = _MAX_RESULT_CACHE_BYTES,
) -> dict[str, Any] | None:
    name = f"{cache_key}.json"
    try:
        fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 0 < info.st_size <= max_bytes
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while total < info.st_size:
            _check_execution_control(deadline, cancel_event)
            chunk = os.read(fd, min(64 * 1024, info.st_size - total))
            if not chunk:
                return None
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or total != info.st_size:
            return None
        value = json.loads(b"".join(chunks).decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "cache_key", "kind", "payload"}
            or value.get("schema_version") != 1
            or value.get("cache_key") != cache_key
            or value.get("kind") != kind
            or not isinstance(value.get("payload"), dict)
        ):
            return None
        payload = value["payload"]
        _validate_result_payload(payload, expected, page, width, height)
        os.utime(fd, None)
        return dict(payload)
    except (CodexVisionError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(fd)


def _publish_cached_result(
    directory_fd: int,
    *,
    cache_key: str,
    kind: str,
    payload: dict[str, Any],
    deadline: float,
    cancel_event: Any | None,
    limits: CacheLimits | None = None,
) -> None:
    wrapper = {
        "schema_version": 1,
        "cache_key": cache_key,
        "kind": kind,
        "payload": payload,
    }
    try:
        encoded = json.dumps(
            wrapper,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise CodexVisionError("codex cli returned invalid schema") from None
    max_bytes = limits.max_codex_result_bytes if limits else _MAX_RESULT_CACHE_BYTES
    if len(encoded) > max_bytes:
        if limits is not None:
            raise CodexVisionError("codex result cache capacity exceeded")
        raise CodexVisionError("codex cli result exceeded limit")
    temporary = f".{cache_key}.{os.getpid()}.{time.time_ns()}.tmp"
    fd = -1
    temporary_identity: tuple[int, int, int, int, int] | None = None
    reservation: _ResultCacheReservation | None = None
    committed = False
    primary_pending = False
    try:
        if limits is not None:
            reservation = _reserve_result_cache_capacity(
                directory_fd,
                cache_key=cache_key,
                incoming_bytes=len(encoded),
                limits=limits,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(encoded):
            _check_execution_control(deadline, cancel_event)
            written = os.write(fd, encoded[offset : offset + 64 * 1024])
            if written <= 0:
                raise OSError
            offset += written
        sync_file_data(fd)
        opened = os.fstat(fd)
        current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
            or _result_identity(opened) != _result_identity(current)
        ):
            raise OSError
        temporary_identity = _result_identity(opened)
        closing_fd = fd
        fd = -1
        os.close(closing_fd)
        _check_execution_control(deadline, cancel_event)
        os.replace(
            temporary,
            f"{cache_key}.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        committed = True
        target = Path(f"{cache_key}.json")
        try:
            published = os.stat(
                target.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CodexCacheCommitError(
                target,
                durability_uncertain=False,
                identity_uncertain=True,
            ) from exc
        if _result_bound_identity(published) != temporary_identity[:4]:
            raise CodexCacheCommitError(
                target,
                durability_uncertain=False,
                identity_uncertain=True,
            )
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise CodexCacheCommitError(
                target,
                durability_uncertain=True,
            ) from exc
        if reservation is not None and limits is not None:
            _commit_result_cache_capacity(
                directory_fd,
                reservation,
                limits=limits,
                deadline=deadline,
                cancel_event=cancel_event,
            )
    except CodexCacheCommitError:
        primary_pending = True
        if reservation is not None and committed and limits is not None:
            try:
                _commit_result_cache_capacity(
                    directory_fd,
                    reservation,
                    limits=limits,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            except CodexVisionError:
                pass
        raise
    except CodexVisionError:
        primary_pending = True
        if reservation is not None and reservation.active and limits is not None:
            try:
                _release_result_cache_capacity(
                    directory_fd,
                    reservation,
                    limits=limits,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            except CodexVisionError:
                pass
        raise
    except OSError:
        primary_pending = True
        if reservation is not None and reservation.active and limits is not None:
            try:
                _release_result_cache_capacity(
                    directory_fd,
                    reservation,
                    limits=limits,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            except CodexVisionError:
                pass
        raise CodexVisionError("codex cli failed") from None
    finally:
        if fd >= 0:
            closing_fd = fd
            fd = -1
            try:
                os.close(closing_fd)
            except OSError:
                if not primary_pending:
                    raise CodexVisionError("codex cli failed") from None
        if temporary_identity is not None:
            _cleanup_result_temporary(
                directory_fd,
                temporary,
                expected=temporary_identity,
            )


def _result_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _result_bound_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _cleanup_result_temporary(
    directory_fd: int,
    name: str,
    *,
    expected: tuple[int, int, int, int, int],
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return
    if _result_identity(current) != expected:
        return
    quarantine = f".cleanup-{secrets.token_hex(16)}.tmp"
    try:
        rename_exclusive(Path(name), Path(quarantine), directory_fd)
    except OSError:
        return
    try:
        moved = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return
    if _result_bound_identity(moved) != expected[:4]:
        try:
            rename_exclusive(Path(quarantine), Path(name), directory_fd)
        except OSError:
            pass
        return
    try:
        os.unlink(quarantine, dir_fd=directory_fd)
    except OSError:
        pass


def _input_cache_key(
    *,
    kind: str,
    codex_version: str,
    codex_sha256: str,
    schema_name: str,
    schema_sha256: str,
    page_number: int,
    width: int,
    height: int,
    page_image_sha256: str,
    crop_image_sha256: list[str],
    prompt: str,
) -> str:
    material = {
        "kind": kind,
        "codex_version": codex_version,
        "codex_sha256": codex_sha256,
        "schema": schema_name,
        "schema_sha256": schema_sha256,
        "page": {"number": page_number, "width": width, "height": height},
        "page_image_sha256": page_image_sha256,
        "crop_image_sha256": crop_image_sha256,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _codex_record(
    *,
    kind: str,
    attempt: int,
    page: int,
    version: str,
    executable_sha256: str,
    schema_name: str,
    cache_key: str,
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "attempt": attempt,
        "page": page,
        "codex_version": version,
        "codex_sha256": executable_sha256,
        "schema": schema_name,
        "cache_key": cache_key,
        "cache_hit": cache_hit,
    }


def _absolute_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _safe_message(error: CodexVisionError) -> str:
    message = str(error)
    if message in _SAFE_MESSAGES:
        return message
    return "codex cli failed"


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)
