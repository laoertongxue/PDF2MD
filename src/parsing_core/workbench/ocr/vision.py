from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import OcrObservation
from .page_cache import (
    CachedPagePayload,
    CacheInputs,
    PageCache,
    PageCacheError,
    SourceSnapshot,
    cache_key_for,
    canonical_language_config,
    copy_verified_helper_image,
)


class VisionClientError(RuntimeError):
    pass


_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_JSON_DEPTH = 20
_MAX_PAGE_NUMBER = 10_000
_MAX_DPI = 600
_ALLOWED_HELPER_ENV = frozenset({"HOME", "PATH", "TMPDIR"})
_HELPER_LOCALE_ENV = {"LANG": "C.UTF-8", "LC_CTYPE": "C.UTF-8"}


@dataclass(frozen=True)
class VisionPageResult:
    cache_key: str
    pdf_sha256: str
    page: int
    dpi: int
    helper_version: str
    language_config: tuple[str, ...]
    image_path: str
    image_sha256: str
    width: int
    height: int
    supported_languages: tuple[str, ...]
    observation: OcrObservation


@dataclass(frozen=True)
class _OpenedPdfSource:
    path: Path
    fd: int
    identity: tuple[int, int]

    def sha256(self) -> str:
        return _file_sha256(self.fd)

    def assert_current(self) -> None:
        current = os.stat(self.path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != self.identity:
            raise VisionClientError("PDF source is not registered")


@dataclass(frozen=True)
class _HelperIdentity:
    path: Path
    identity: tuple[int, int]
    uid: int
    mode: int
    sha256: str


class RegisteredPdfSources:
    def __init__(self, paths):
        self._sources: dict[Path, tuple[int, int]] = {}
        for path in paths:
            error = None
            fd = None
            try:
                registered, fd, identity = self._open_pdf(Path(path), expected=None)
                self._sources[registered] = identity
            except Exception:
                error = VisionClientError("PDF source is not registered")
            finally:
                if fd is not None:
                    os.close(fd)
            if error is not None:
                raise error from None

    def validate(self, path: Path | str) -> Path:
        with self.open_validated(path) as source:
            return source.path

    @contextmanager
    def open_validated(self, path: Path | str):
        error = None
        fd = None
        try:
            candidate = Path(path)
            canonical = self._canonical_path(candidate)
            expected = self._sources.get(canonical)
            if expected is None:
                raise VisionClientError("PDF source is not registered")
            canonical, fd, identity = self._open_pdf(candidate, expected=expected)
        except Exception:
            error = VisionClientError("PDF source is not registered")
        if error is not None:
            if fd is not None:
                os.close(fd)
            raise error from None
        source = _OpenedPdfSource(canonical, fd, identity)
        try:
            yield source
        finally:
            os.close(fd)

    @staticmethod
    def _canonical_path(path: Path) -> Path:
        if not path.is_absolute():
            raise VisionClientError("PDF source is not registered")
        link_info = path.lstat()
        if stat.S_ISLNK(link_info.st_mode):
            raise VisionClientError("PDF source is not registered")
        canonical = path.resolve(strict=True)
        if canonical.suffix.lower() != ".pdf":
            raise VisionClientError("PDF source is not registered")
        return canonical

    @classmethod
    def _open_pdf(
        cls, path: Path, *, expected: tuple[int, int] | None
    ) -> tuple[Path, int, tuple[int, int]]:
        canonical = cls._canonical_path(path)
        fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            current = os.stat(canonical, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if not stat.S_ISREG(opened.st_mode):
                raise VisionClientError("PDF source is not registered")
            if identity != (current.st_dev, current.st_ino):
                raise VisionClientError("PDF source is not registered")
            if expected is not None and identity != expected:
                raise VisionClientError("PDF source is not registered")
            if os.pread(fd, 5, 0) != b"%PDF-":
                raise VisionClientError("PDF source is not registered")
            return canonical, fd, identity
        except Exception:
            os.close(fd)
            raise


class VisionClient:
    def __init__(
        self,
        *,
        helper_path,
        cache_root,
        source_validator,
        helper_version,
        timeout=30,
        python_executable=None,
    ):
        helper_candidate = Path(helper_path).expanduser()
        self.helper_path = (
            helper_candidate if helper_candidate.is_absolute() else Path.cwd() / helper_candidate
        )
        self.cache = PageCache(Path(cache_root))
        self.source_validator = source_validator
        self._declared_helper_version = str(helper_version)
        self.timeout = timeout
        self.python_executable = python_executable
        self._helper_identity = self._capture_helper_identity()
        self.helper_version = _stable_helper_version(
            self._declared_helper_version, self._helper_identity
        )
        self._source_snapshots_guard = threading.Lock()
        self._source_snapshots: dict[tuple[int, int], SourceSnapshot] = {}

    def recognize(self, pdf_path, *, page, dpi, languages) -> VisionPageResult:
        error = None
        try:
            with self.source_validator.open_validated(pdf_path) as source:
                _validate_page_number(page)
                _validate_dpi(dpi)
                language_config = canonical_language_config(languages)
                source_snapshot = self._snapshot_for_source(source)
                inputs = CacheInputs(
                    pdf_sha256=source_snapshot.pdf_sha256,
                    page=page,
                    dpi=dpi,
                    helper_version=self.helper_version,
                    language_config=language_config,
                )
                cache_key = cache_key_for(inputs)
                with self.cache.lock(cache_key):
                    cached = self.cache.load_valid(cache_key, inputs)
                    if cached is not None:
                        source.assert_current()
                        return self._result_from_cached(cached)
                    result = self._render_and_publish(source_snapshot.path, inputs, cache_key)
                    source.assert_current()
                    return result
        except VisionClientError as exc:
            error = VisionClientError(_safe_error_message(exc))
        except PageCacheError:
            error = VisionClientError("vision OCR could not complete")
        except Exception:
            error = VisionClientError("vision OCR could not complete")
        raise error from None

    def _snapshot_for_source(self, source: _OpenedPdfSource) -> SourceSnapshot:
        with self._source_snapshots_guard:
            snapshot = self._source_snapshots.get(source.identity)
        if snapshot is not None:
            try:
                return self.cache.validate_source_snapshot(snapshot, verify_hash=True)
            except PageCacheError:
                with self._source_snapshots_guard:
                    if self._source_snapshots.get(source.identity) == snapshot:
                        del self._source_snapshots[source.identity]

        lock_token = hashlib.sha256(
            f"source-snapshot:{self.cache.root}:{source.identity[0]}:{source.identity[1]}".encode()
        ).hexdigest()
        with self.cache.lock(lock_token):
            with self._source_snapshots_guard:
                snapshot = self._source_snapshots.get(source.identity)
            if snapshot is not None:
                try:
                    return self.cache.validate_source_snapshot(snapshot, verify_hash=True)
                except PageCacheError:
                    with self._source_snapshots_guard:
                        if self._source_snapshots.get(source.identity) == snapshot:
                            del self._source_snapshots[source.identity]
            snapshot = self.cache.publish_source_snapshot(source.fd)
            with self._source_snapshots_guard:
                self._source_snapshots[source.identity] = snapshot
            return snapshot

    def _render_and_publish(
        self, source_path: Path, inputs: CacheInputs, cache_key: str
    ) -> VisionPageResult:
        job_relative, job_dir, job_root = self.cache.make_job_dir(cache_key)
        temp_image = self.cache.temporary_image_path(cache_key)
        try:
            response = self._run_helper(source_path, inputs, job_relative, job_root)
            validated = _validate_helper_response(response, inputs.page)
            copy_verified_helper_image(
                jobs_root=job_root,
                job_dir=job_dir,
                relative_image_path=validated["image_path"],
                expected_sha256=validated["image_sha256"],
                destination=temp_image,
            )
            cached = self.cache.publish(
                cache_key=cache_key,
                inputs=inputs,
                image_bytes_path=temp_image,
                image_sha256=validated["image_sha256"],
                width=validated["width"],
                height=validated["height"],
                supported_languages=validated["supported_languages"],
                observations=validated["observations"],
            )
            return self._result_from_cached(cached)
        except Exception:
            try:
                temp_image.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            self.cache.cleanup_job_dir(job_root)

    def _run_helper(
        self, source_path: Path, inputs: CacheInputs, job_relative: str, job_root: Path
    ) -> dict[str, Any]:
        self._verify_helper_identity()
        command = {
            "command": "render_and_recognize",
            "pdf_path": str(source_path),
            "page": inputs.page,
            "dpi": inputs.dpi,
            "languages": list(inputs.language_config),
            "output_dir": job_relative,
        }
        env = _helper_environment(job_root)
        args = [str(self.helper_path)]
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        process_group_id = process.pid
        try:
            stdout = self._communicate_bounded(
                process,
                json.dumps(command, sort_keys=True).encode("utf-8") + b"\n",
                process_group_id,
            )
        finally:
            self._close_process_pipes(process)
        if process.returncode != 0:
            raise VisionClientError("vision helper failed")
        line = _single_stdout_response(stdout)
        if not line:
            raise VisionClientError("vision helper returned no response")
        invalid_json = False
        try:
            response = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
        except Exception:
            invalid_json = True
        if invalid_json:
            raise VisionClientError("vision helper returned invalid response")
        if isinstance(response, dict) and "error" in response:
            raise VisionClientError("vision helper reported an error")
        if not isinstance(response, dict):
            raise VisionClientError("vision helper returned invalid response")
        return response

    def _communicate_bounded(
        self, process: subprocess.Popen[bytes], payload: bytes, process_group_id: int
    ) -> bytes:
        try:
            if process.stdin is not None:
                process.stdin.write(payload)
                process.stdin.close()
        except BrokenPipeError:
            pass

        stdout_chunks: list[bytes] = []
        stdout_size = 0
        stderr_size = 0
        deadline = time.monotonic() + self.timeout
        selector = selectors.DefaultSelector()
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            if pipe is None:
                continue
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe.fileno(), selectors.EVENT_READ, name)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process(process, process_group_id)
                    raise VisionClientError("vision helper timed out")
                for key, _mask in selector.select(timeout=min(0.1, remaining)):
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
                            self._terminate_process(process, process_group_id)
                            raise VisionClientError("vision helper output exceeded limit")
                        stdout_chunks.append(chunk)
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > _MAX_STDERR_BYTES:
                            self._terminate_process(process, process_group_id)
                            raise VisionClientError("vision helper output exceeded limit")
            remaining = deadline - time.monotonic()
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0, remaining))
                except subprocess.TimeoutExpired:
                    self._terminate_process(process, process_group_id)
                    raise VisionClientError("vision helper timed out") from None
        finally:
            selector.close()
        return b"".join(stdout_chunks)

    @staticmethod
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
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=0.5)
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

    def _result_from_cached(self, cached: CachedPagePayload) -> VisionPageResult:
        payload_json = json.dumps(
            {
                "page": cached.page,
                "image_sha256": cached.image_sha256,
                "width": cached.width,
                "height": cached.height,
                "supported_languages": list(cached.supported_languages),
                "observations": list(cached.observations),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        observation = OcrObservation(
            id=f"vision-{cached.cache_key}",
            page_id=f"page-{cached.cache_key}",
            engine="apple_vision",
            input_hash=cached.pdf_sha256,
            engine_config_hash=_engine_config_hash(cached),
            payload_json=payload_json,
            created_at=0,
        )
        return VisionPageResult(
            cache_key=cached.cache_key,
            pdf_sha256=cached.pdf_sha256,
            page=cached.page,
            dpi=cached.dpi,
            helper_version=cached.helper_version,
            language_config=cached.language_config,
            image_path=cached.image_path,
            image_sha256=cached.image_sha256,
            width=cached.width,
            height=cached.height,
            supported_languages=cached.supported_languages,
            observation=observation,
        )

    def _capture_helper_identity(self) -> _HelperIdentity:
        try:
            return _read_helper_identity(self.helper_path)
        except Exception:
            raise VisionClientError("vision helper is not available") from None

    def _verify_helper_identity(self) -> None:
        try:
            current = _read_helper_identity(self.helper_path)
        except Exception:
            raise VisionClientError("vision helper is not available") from None
        if current != self._helper_identity:
            raise VisionClientError("vision helper is not available")


def _validate_helper_response(response: dict[str, Any], expected_page: int) -> dict[str, Any]:
    if _json_depth(response) > _MAX_JSON_DEPTH:
        raise VisionClientError("vision helper returned invalid response")
    encoded_size = len(
        json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )
    if encoded_size > _MAX_RESPONSE_BYTES:
        raise VisionClientError("vision helper returned invalid response")
    expected_fields = {
        "page",
        "image_path",
        "image_sha256",
        "width",
        "height",
        "supported_languages",
        "observations",
    }
    if set(response) != expected_fields:
        raise VisionClientError("vision helper returned invalid response")
    if response.get("page") != expected_page:
        raise VisionClientError("vision helper returned invalid response")
    image_path = response.get("image_path")
    image_sha256 = response.get("image_sha256")
    width = response.get("width")
    height = response.get("height")
    supported_languages = response.get("supported_languages")
    observations = response.get("observations")
    if not isinstance(image_path, str) or not image_path:
        raise VisionClientError("vision helper returned invalid response")
    if not _is_sha256(image_sha256):
        raise VisionClientError("vision helper returned invalid response")
    _validate_positive_int(width)
    _validate_positive_int(height)
    supported = _validate_string_list(supported_languages)
    validated_observations = _validate_observations(observations)
    return {
        "image_path": image_path,
        "image_sha256": image_sha256,
        "width": width,
        "height": height,
        "supported_languages": supported,
        "observations": validated_observations,
    }


def _validate_observations(value: Any) -> tuple[dict[str, Any], ...]:
    from .page_cache import _validate_observations as validate

    try:
        return validate(value)
    except PageCacheError:
        pass
    raise VisionClientError("vision helper returned invalid response")


def _validate_string_list(value: Any) -> tuple[str, ...]:
    from .page_cache import _validate_string_list as validate

    try:
        return validate(value)
    except PageCacheError:
        pass
    raise VisionClientError("vision helper returned invalid response")


def _validate_positive_int(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise VisionClientError("vision helper returned invalid response")


def _validate_page_number(value: Any) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > _MAX_PAGE_NUMBER
    ):
        raise VisionClientError("vision helper returned invalid response")


def _validate_dpi(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > _MAX_DPI:
        raise VisionClientError("vision helper returned invalid response")


def _file_sha256(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _fd_sha256(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _read_helper_identity(path: Path) -> _HelperIdentity:
    link_info = path.lstat()
    if stat.S_ISLNK(link_info.st_mode):
        raise VisionClientError("vision helper is not available")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        mode = stat.S_IMODE(info.st_mode)
        if identity != (current.st_dev, current.st_ino):
            raise VisionClientError("vision helper is not available")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or not mode & stat.S_IXUSR
            or mode & 0o022
        ):
            raise VisionClientError("vision helper is not available")
        return _HelperIdentity(
            path=path,
            identity=identity,
            uid=info.st_uid,
            mode=mode,
            sha256=_fd_sha256(fd),
        )
    finally:
        os.close(fd)


def _stable_helper_version(declared: str, identity: _HelperIdentity) -> str:
    return f"{declared}+helper_sha256={identity.sha256}"


def _helper_environment(job_root: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_HELPER_ENV
    }
    env.update(_HELPER_LOCALE_ENV)
    env["PDF2MD_VISION_OUTPUT_ROOT"] = str(job_root)
    return env


def _single_stdout_response(stdout: bytes) -> bytes:
    if not stdout:
        return b""
    nonempty = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not nonempty:
        return b""
    if len(nonempty) != 1:
        raise VisionClientError("vision helper returned invalid response")
    line = nonempty[0]
    if len(line) > _MAX_RESPONSE_BYTES:
        raise VisionClientError("vision helper output exceeded limit")
    return line


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


_SAFE_ERROR_MESSAGES = frozenset(
    {
        "PDF source is not registered",
        "vision OCR could not complete",
        "vision helper failed",
        "vision helper is not available",
        "vision helper output exceeded limit",
        "vision helper reported an error",
        "vision helper returned invalid response",
        "vision helper returned no response",
        "vision helper timed out",
    }
)


def _safe_error_message(error: VisionClientError) -> str:
    message = str(error)
    if message in _SAFE_ERROR_MESSAGES:
        return message
    return "vision OCR could not complete"


def _engine_config_hash(cached: CachedPagePayload) -> str:
    payload = {
        "dpi": cached.dpi,
        "helper_version": cached.helper_version,
        "language_config": list(cached.language_config),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)
