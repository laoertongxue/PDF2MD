from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class CodexVisionError(RuntimeError):
    pass


_SCHEMA_DIR = Path(__file__).with_name("schemas")
_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 1024 * 1024
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_INPUT_JSON_BYTES = 512 * 1024
_MAX_JSON_DEPTH = 24
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_IMAGES = 5
_ALLOWED_ENV = frozenset({"HOME", "PATH", "TMPDIR"})
_LOCALE_ENV = {"LANG": "C.UTF-8", "LC_CTYPE": "C.UTF-8"}
_SAFE_MESSAGES = frozenset(
    {
        "codex cli failed",
        "codex cli input is invalid",
        "codex cli input is too large",
        "codex cli is not available",
        "codex cli output exceeded limit",
        "codex cli result exceeded limit",
        "codex cli returned invalid json",
        "codex cli returned invalid schema",
        "codex cli timed out",
        "image input is not available",
        "too many crop images",
    }
)


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


@dataclass(frozen=True)
class CodexVisionResult:
    payload: dict[str, Any]
    record: dict[str, Any]


@dataclass(frozen=True)
class _ExecutableIdentity:
    path: Path
    identity: tuple[int, int]
    uid: int
    mode: int
    sha256: str


class CodexVisionExecutor:
    def __init__(
        self,
        *,
        codex_path: str | Path,
        temp_root: str | Path,
        trusted_image_root: str | Path,
        timeout: float = 60,
    ):
        self.codex_path = _absolute_path(codex_path)
        self.temp_root = _absolute_path(temp_root)
        self.trusted_image_root = _absolute_path(trusted_image_root)
        try:
            root_info = self.trusted_image_root.lstat()
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                raise CodexVisionError("image input is not available")
        except FileNotFoundError:
            raise CodexVisionError("image input is not available") from None
        self.timeout = float(timeout)
        self._identity = _read_executable_identity(self.codex_path)
        self.codex_version = self._read_codex_version()

    def transcribe_page(
        self,
        page_image: str | Path,
        *,
        page_number: int,
        width: int,
        height: int,
        expected_image_sha256: str | None = None,
    ) -> CodexVisionResult:
        _validate_page(page_number, width, height)
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
        )

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
    ) -> CodexVisionResult:
        _validate_page(page_number, width, height)
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
    ) -> CodexVisionResult:
        last_error: CodexVisionError | None = None
        for attempt in (1, 2):
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
                )
                record["attempts"] = attempt
                return CodexVisionResult(payload=payload, record=record)
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
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._verify_identity()
        _ensure_prompt_bounded(prompt)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="codex-vision-", dir=self.temp_root))
        try:
            _copy_verified_image(
                page_image,
                job_dir / "page.png",
                trusted_root=self.trusted_image_root,
                expected_sha256=expected_image_sha256,
                expected_width=width,
                expected_height=height,
            )
            for index, crop in enumerate(crop_images, start=1):
                expected_hash = crop_expected_sha256[index - 1] if crop_expected_sha256 else None
                _copy_verified_image(
                    crop,
                    job_dir / f"crop-{index}.png",
                    trusted_root=self.trusted_image_root,
                    expected_sha256=expected_hash,
                )
            shutil.copyfile(_SCHEMA_DIR / schema_name, job_dir / schema_name)
            argv = _codex_argv(self.codex_path, schema_name, 1 + len(crop_images))
            validate_codex_exec_argv([str(part) for part in argv])
            deadline = time.monotonic() + self.timeout
            stdout = self._communicate(argv, prompt, job_dir, deadline=deadline)
            if stdout.strip():
                raise CodexVisionError("codex cli returned invalid json")
            result_path = job_dir / "result.json"
            payload = _read_result_json(result_path, deadline=deadline)
            _validate_result_payload(payload, expected, page_number, width, height)
            record = {
                "kind": kind,
                "attempt": attempt,
                "page": page_number,
                "codex_path": str(self.codex_path),
                "codex_version": self.codex_version,
                "codex_sha256": self._identity.sha256,
                "schema": schema_name,
                "cache_key": _cache_key(kind, self.codex_version, self._identity.sha256, payload),
            }
            return payload, record
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _communicate(
        self, argv: list[str], prompt: str, cwd: Path, *, deadline: float | None = None
    ) -> bytes:
        env = _codex_environment()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        process_group_id = process.pid
        try:
            return _communicate_bounded(
                process,
                prompt.encode("utf-8"),
                timeout=self.timeout,
                process_group_id=process_group_id,
                deadline=deadline,
            )
        finally:
            _close_process_pipes(process)

    def _read_codex_version(self) -> str:
        self._verify_identity()
        try:
            result = subprocess.run(
                [str(self.codex_path), "--version"],
                text=True,
                capture_output=True,
                timeout=min(max(self.timeout, 0.1), 5),
                check=False,
                env=_codex_environment(),
            )
        except Exception:
            raise CodexVisionError("codex cli is not available") from None
        if result.returncode != 0:
            raise CodexVisionError("codex cli is not available")
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
        return _sanitize_version(version)

    def _verify_identity(self) -> None:
        try:
            current = _read_executable_identity(self.codex_path)
        except Exception:
            raise CodexVisionError("codex cli is not available") from None
        if current != self._identity:
            raise CodexVisionError("codex cli is not available")


def validate_codex_exec_argv(argv: list[str]) -> None:
    if not isinstance(argv, list) or len(argv) < 2:
        raise CodexVisionError("codex cli failed")
    forbidden = {
        "--resume",
        "resume",
        "--session",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-sandbox",
    }
    if any(part in forbidden for part in argv):
        raise CodexVisionError("codex cli failed")
    if "--sandbox" not in argv:
        raise CodexVisionError("codex cli failed")
    sandbox_index = argv.index("--sandbox")
    if sandbox_index + 1 >= len(argv) or argv[sandbox_index + 1] != "read-only":
        raise CodexVisionError("codex cli failed")
    if "--ephemeral" not in argv or "--ignore-user-config" not in argv:
        raise CodexVisionError("codex cli failed")
    if argv[-1] != "-":
        raise CodexVisionError("codex cli failed")


def _codex_argv(codex_path: Path, schema_name: str, image_count: int) -> list[str]:
    argv = [
        str(codex_path),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--image",
        "page.png",
    ]
    for index in range(1, image_count):
        argv.extend(["--image", f"crop-{index}.png"])
    argv.extend(["--output-schema", schema_name, "--output-last-message", "result.json", "-"])
    return argv


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    payload: bytes,
    *,
    timeout: float,
    process_group_id: int,
    deadline: float | None = None,
) -> bytes:
    deadline = time.monotonic() + timeout if deadline is None else deadline
    stdout_chunks: list[bytes] = []
    stdout_size = 0
    stderr_size = 0
    payload_offset = 0
    selector = selectors.DefaultSelector()
    if process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin.fileno(), selectors.EVENT_WRITE, "stdin")
    for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
        if pipe is None:
            continue
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe.fileno(), selectors.EVENT_READ, name)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process, process_group_id)
                raise CodexVisionError("codex cli timed out")
            for key, _mask in selector.select(timeout=min(0.1, remaining)):
                if key.data == "stdin":
                    if payload_offset == len(payload):
                        selector.unregister(key.fd)
                        process.stdin.close()
                        continue
                    try:
                        written = os.write(key.fd, payload[payload_offset : payload_offset + 65536])
                    except BlockingIOError:
                        written = 0
                    except BrokenPipeError:
                        selector.unregister(key.fd)
                        process.stdin.close()
                        payload_offset = len(payload)
                        continue
                    payload_offset += written
                    if payload_offset == len(payload):
                        selector.unregister(key.fd)
                        process.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                if key.data == "stdout":
                    stdout_size += len(chunk)
                    if stdout_size > _MAX_STDOUT_BYTES:
                        _terminate_process(process, process_group_id)
                        raise CodexVisionError("codex cli output exceeded limit")
                    stdout_chunks.append(chunk)
                else:
                    stderr_size += len(chunk)
                    if stderr_size > _MAX_STDERR_BYTES:
                        _terminate_process(process, process_group_id)
                        raise CodexVisionError("codex cli output exceeded limit")
        remaining = deadline - time.monotonic()
        if process.poll() is None:
            try:
                process.wait(timeout=max(0, remaining))
            except subprocess.TimeoutExpired:
                _terminate_process(process, process_group_id)
                raise CodexVisionError("codex cli timed out") from None
    finally:
        selector.close()
    if process.returncode != 0:
        raise CodexVisionError("codex cli failed")
    return b"".join(stdout_chunks)


def _terminate_process(process: subprocess.Popen[bytes], process_group_id: int) -> None:
    caller_group_id = os.getpgrp()
    group_is_safe = process_group_id != caller_group_id
    try:
        if group_is_safe:
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except (PermissionError, ProcessLookupError):
                process.terminate()
        else:
            process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    try:
        if group_is_safe:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                process.kill()
        else:
            process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=0.5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=0.5)
        except Exception:
            pass


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _read_result_json(path: Path, *, deadline: float | None = None) -> dict[str, Any]:
    if deadline is None:
        deadline = time.monotonic() + 60
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
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CodexVisionError("codex cli returned invalid json")
        if opened.st_size > _MAX_RESULT_BYTES:
            raise CodexVisionError("codex cli result exceeded limit")

        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise CodexVisionError("codex cli timed out")
            chunk = os.read(result_fd, _MAX_RESULT_BYTES + 1 - size)
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
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or (path_after.st_dev, path_after.st_ino) != (opened.st_dev, opened.st_ino)
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
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CodexVisionError("codex cli returned invalid json")


def _validate_result_payload(
    payload: dict[str, Any], expected: str, page: int, width: int, height: int
):
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
            not isinstance(number, int | float)
            or isinstance(number, bool)
            or not math.isfinite(number)
        ):
            raise CodexVisionError("codex cli returned invalid schema")
        if number < 0 or number > 1:
            raise CodexVisionError("codex cli returned invalid schema")
    if value["x"] + value["width"] > 1 or value["y"] + value["height"] > 1:
        raise CodexVisionError("codex cli returned invalid schema")


def _validate_confidence(value: Any) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
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
        if stat.S_ISLNK(link_info.st_mode):
            raise CodexVisionError("image input is not available")
        fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_executable_identity(path: Path) -> _ExecutableIdentity:
    link_info = path.lstat()
    if stat.S_ISLNK(link_info.st_mode):
        raise CodexVisionError("codex cli is not available")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        mode = stat.S_IMODE(info.st_mode)
        if identity != (current.st_dev, current.st_ino):
            raise CodexVisionError("codex cli is not available")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or not mode & stat.S_IXUSR
            or mode & 0o022
        ):
            raise CodexVisionError("codex cli is not available")
        return _ExecutableIdentity(
            path=path,
            identity=identity,
            uid=info.st_uid,
            mode=mode,
            sha256=_fd_sha256(fd),
        )
    finally:
        os.close(fd)


def _fd_sha256(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _codex_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _ALLOWED_ENV}
    env.update(_LOCALE_ENV)
    return env


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
    if isinstance(value, str) and (
        "/Users/" in value
        or "PDF2MD" in value
        or "OPENAI_API_KEY" in value
        or "教材" in value
        or value.startswith("/tmp/")
        or value.startswith("file://")
        or value.startswith("/")
        or value.startswith("../")
        or value.startswith("./")
        or value.startswith("~/")
    ):
        raise CodexVisionError("codex cli input is invalid")


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in (
            "path",
            "filename",
            "file_uri",
            "course_dir",
            "book_dir",
            "original_text",
            "raw_document",
            "api_key",
            "keychain",
        )
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


def _cache_key(kind: str, codex_version: str, codex_sha256: str, payload: dict[str, Any]) -> str:
    material = {
        "kind": kind,
        "codex_version": codex_version,
        "codex_sha256": codex_sha256,
        "payload_sha256": hashlib.sha256(_json_bytes(payload)).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _absolute_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _sanitize_version(value: str) -> str:
    safe = "".join(character for character in value if character.isprintable())
    return safe[:128] or "unknown"


def _safe_message(error: CodexVisionError) -> str:
    message = str(error)
    if message in _SAFE_MESSAGES:
        return message
    return "codex cli failed"


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)
