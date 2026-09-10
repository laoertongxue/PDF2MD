from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, TypeGuard

from .alignment import (
    authorize_baidu_escalation,
    classify_page,
    compare_observations,
    needs_baidu,
    primary_block_uncertainty_reason,
)
from .atomic_io import AtomicCommitError, atomic_write_bytes
from .baidu import BaiduEscalationAuthorization
from .codex_vision import CodexVisionError, validate_persisted_payload
from .vision import canonicalize_vision_payload

# Accepted output is publishable only at this unattended high-confidence floor.
MIN_FINAL_ADJUDICATION_CONFIDENCE = 0.95
_BATCH_STATE_SCHEMA_VERSION = 2
_MAX_BATCH_STATE_BYTES = 16 * 1024 * 1024
_STATE_READ_CHUNK_BYTES = 64 * 1024


def _write_file(fd: int, data: memoryview) -> int:
    return os.write(fd, data)


def _sync_file(fd: int) -> None:
    os.fsync(fd)


def _close_file(fd: int) -> None:
    os.close(fd)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _replace_file(source: Path, target: Path, directory_fd: int) -> None:
    if source.parent != target.parent:
        raise ValueError("atomic source and target must share a directory")
    os.replace(
        source.name,
        target.name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _sync_directory(fd: int) -> None:
    os.fsync(fd)


class BatchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class PageStatus(StrEnum):
    PENDING = "pending"
    RENDERING = "rendering"
    PRIMARY_OCR = "primary_ocr"
    DIFFING = "diffing"
    BAIDU_PENDING = "baidu_pending"
    ADJUDICATING = "adjudicating"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PageRun:
    page: int
    status: PageStatus
    error: str | None = None
    evidence_fingerprint: str | None = None


@dataclass(frozen=True)
class BatchRun:
    status: BatchStatus
    pages: dict[int, PageRun]
    error: str | None = None


class _BatchCancelled(Exception):
    pass


class _EvidenceBlocked(ValueError):
    code = "ocr_evidence_uncertain"


class _StateInvalid(ValueError):
    code = "ocr_state_invalid"


class CancellationEvent(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...


class _VisionEngine(Protocol):
    def recognize(
        self,
        pdf_path: str | Path,
        *,
        page: int,
        dpi: int,
        languages: list[str] | tuple[str, ...],
        deadline: float | None,
        cancel_event: CancellationEvent,
    ) -> object: ...


class _CodexEngine(Protocol):
    def transcribe_page(
        self,
        page_image: str | Path,
        *,
        page_number: int,
        width: int,
        height: int,
        expected_image_sha256: str | None,
        deadline: float | None,
        cancel_event: CancellationEvent,
    ) -> object: ...

    def adjudicate_page(
        self,
        page_image: str | Path,
        *,
        page_number: int,
        width: int,
        height: int,
        codex_observation: object,
        apple_observation: object,
        diff: object,
        baidu_observation: object | None,
        expected_image_sha256: str | None,
        deadline: float | None,
        cancel_event: CancellationEvent,
    ) -> object: ...


class _BaiduEngine(Protocol):
    def recognize(
        self,
        image: bytes,
        *,
        authorization: BaiduEscalationAuthorization | None,
        page_hash: str | None,
        input_fingerprint: str | None,
        page: int | None,
        alignment_status: str | None,
        deadline: float | None,
        cancel_event: CancellationEvent,
    ) -> object: ...


class _PdfSnapshot(TypedDict):
    sha256: str
    size: int


class _RunConfig(TypedDict):
    pages: list[int]
    dpi: int
    languages: list[str]
    sample_rate: float


type _JsonValue = None | bool | int | float | str | list[_JsonValue] | dict[str, _JsonValue]
type _JsonObject = dict[str, _JsonValue]


class _PageState(TypedDict, total=False):
    status: str
    error: str
    attempts: int
    vision: _JsonObject
    codex: _JsonObject
    alignment: _JsonObject
    baidu: _JsonObject
    decision: _JsonObject
    page_input_fingerprint: str
    evidence_fingerprint: str


class _BatchState(TypedDict):
    schema_version: int
    status: str
    input_fingerprint: str
    pdf_snapshot: _PdfSnapshot
    run_config: _RunConfig
    pages: dict[str, _PageState]
    updated_at: int
    error: NotRequired[str | None]


class CancellationSignal:
    """Latch a workflow cancellation callback behind an Event-like API."""

    def __init__(
        self,
        check: Callable[[], bool] | None = None,
        event: CancellationEvent | None = None,
    ) -> None:
        self._check = check
        self._event = event or threading.Event()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self._check is not None and self._check():
            self._event.set()
            return True
        return False

    def set(self) -> None:
        self._event.set()


class OcrOrchestrator:
    """Run the unattended OCR state machine with a publish gate.

    Dependencies are deliberately injected. The orchestrator owns ordering,
    state durability, escalation authorization, and the final publication gate;
    engine implementations own protocol and schema validation.
    """

    def __init__(
        self,
        *,
        vision: _VisionEngine | None,
        codex: _CodexEngine | None,
        baidu: _BaiduEngine | None,
        state_root: str | Path,
        image_loader: Callable[..., bytes] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_event: CancellationEvent | None = None,
        max_page_attempts: int = 2,
    ) -> None:
        self.vision = vision
        self.codex = codex
        self.baidu = baidu
        self.state_root = Path(state_root)
        self.image_loader = image_loader or self._load_image
        self.is_cancelled = is_cancelled or (lambda: False)
        self.cancel_event = CancellationSignal(self.is_cancelled, cancel_event)
        self._publication_local = threading.local()
        if not 1 <= max_page_attempts <= 3:
            raise ValueError("max page attempts must be between 1 and 3")
        self.max_page_attempts = max_page_attempts

    def run_batch(
        self,
        pdf_path: str | Path,
        *,
        pages: list[int] | tuple[int, ...],
        dpi: int,
        languages: list[str] | tuple[str, ...],
        sample_rate: float = 0.05,
        timeout: float | None = None,
    ) -> BatchRun:
        if timeout is not None and timeout <= 0:
            raise ValueError("batch timeout must be positive")
        deadline = None if timeout is None else time.monotonic() + timeout
        page_numbers = tuple(pages)
        state: _BatchState | None = None
        page_runs: dict[int, PageRun] = {}
        current: _PageState | None = None

        try:
            completed = self._completed_final_for_request(
                pdf_path,
                page_numbers,
                dpi,
                languages,
                sample_rate,
                deadline=deadline,
            )
            if completed is not None:
                return BatchRun(
                    BatchStatus.COMPLETED,
                    self._page_runs(completed["pages"]),
                )
            with self._publication_transaction(deadline=deadline):
                completed = self._completed_final_for_request(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
                if completed is not None:
                    return BatchRun(
                        BatchStatus.COMPLETED,
                        self._page_runs(completed["pages"]),
                    )
                self._discard_final_artifact()
                self._check_control(deadline)
                state = self._load_or_create_state(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
            page_state = state["pages"]
            page_runs = self._page_runs(page_state)
            self._check_control(deadline)
            if not self._is_contiguous(page_numbers):
                for page in page_numbers:
                    page_state[str(page)] = {
                        "status": PageStatus.FAILED.value,
                        "error": "ocr_page_sequence_incomplete",
                    }
                return self._finish(
                    state,
                    BatchStatus.BLOCKED,
                    "ocr_page_sequence_incomplete",
                    page_runs,
                )

            self._set_status(state, BatchStatus.RUNNING)
            self._check_control(deadline)
            for page in page_numbers:
                current = page_state[str(page)]
                self._check_control(deadline)
                if current.get("status") == PageStatus.COMPLETED.value:
                    valid = self._completed_evidence_is_valid(state, current, page, sample_rate)
                    self._check_control(deadline)
                    if valid:
                        continue
                    self._reset_page(current)
                if int(current.get("attempts", 0)) >= self.max_page_attempts:
                    return self._finish(
                        state, BatchStatus.FAILED, "ocr_retry_limit_reached", page_runs
                    )
                current["attempts"] = int(current.get("attempts", 0)) + 1
                self._check_control(deadline)
                self._run_page(
                    state, current, pdf_path, page, dpi, languages, sample_rate, deadline
                )
                self._check_control(deadline)

            self._check_control(deadline)
            if any(
                page_state[str(page)].get("status") != PageStatus.COMPLETED.value
                for page in page_numbers
            ):
                return self._finish(state, BatchStatus.BLOCKED, "ocr_batch_incomplete", page_runs)
            self._check_control(deadline)
            with self._publication_transaction(deadline=deadline):
                completed = self._completed_final_for_request(
                    pdf_path,
                    page_numbers,
                    dpi,
                    languages,
                    sample_rate,
                    deadline=deadline,
                )
                if completed is not None:
                    return BatchRun(
                        BatchStatus.COMPLETED,
                        self._page_runs(completed["pages"]),
                    )
                if self._completed_transaction_exists_unlocked():
                    raise _StateInvalid("completed OCR publication changed")
                self._check_control(deadline)
                state["error"] = None
                self._set_status(state, BatchStatus.COMPLETED)
                self._check_control(deadline)
                self._publish_atomically_unlocked(state, deadline=deadline)
            return BatchRun(BatchStatus.COMPLETED, self._page_runs(page_state))
        except AtomicCommitError:
            raise
        except _BatchCancelled:
            if state is None:
                return BatchRun(
                    BatchStatus.CANCELLED,
                    {
                        page: PageRun(page, PageStatus.CANCELLED, "ocr_cancelled")
                        for page in page_numbers
                    },
                    "ocr_cancelled",
                )
            if current is not None and current.get("status") != PageStatus.COMPLETED.value:
                current["status"] = PageStatus.CANCELLED.value
                current["error"] = "ocr_cancelled"
            return self._finish(state, BatchStatus.CANCELLED, "ocr_cancelled", page_runs)
        except _EvidenceBlocked as exc:
            error = exc.code
            if state is None:
                return BatchRun(
                    BatchStatus.BLOCKED,
                    {page: PageRun(page, PageStatus.FAILED, error) for page in page_numbers},
                    error,
                )
            if current is not None and current.get("status") != PageStatus.COMPLETED.value:
                current["status"] = PageStatus.FAILED.value
                current["error"] = error
            return self._finish(state, BatchStatus.BLOCKED, error, page_runs)
        except Exception as exc:
            if state is None:
                error = _safe_error(exc)
                return BatchRun(
                    BatchStatus.FAILED,
                    {page: PageRun(page, PageStatus.FAILED, error) for page in page_numbers},
                    error,
                )
            if current is not None and current.get("status") != PageStatus.COMPLETED.value:
                current["status"] = PageStatus.FAILED.value
                current["error"] = _safe_error(exc)
            return self._finish(state, BatchStatus.FAILED, _safe_error(exc), page_runs)

    def _run_page(
        self,
        state: _BatchState,
        current: _PageState,
        pdf_path: str | Path,
        page: int,
        dpi: int,
        languages: list[str] | tuple[str, ...],
        sample_rate: float,
        deadline: float | None,
    ) -> None:
        self._check_control(deadline)
        if self.vision is None or self.codex is None:
            raise ValueError("OCR engine is unavailable")
        if "vision" not in current:
            current["status"] = PageStatus.RENDERING.value
            self._persist(state)
            vision_result = self._call_engine(
                self.vision.recognize,
                pdf_path,
                page=page,
                dpi=dpi,
                languages=languages,
                deadline=deadline,
            )
            current["vision"] = _jsonable_object(vision_result)
            _validate_vision_pdf_snapshot(current["vision"], state["pdf_snapshot"])
        vision = current["vision"]
        _validate_vision_pdf_snapshot(vision, state["pdf_snapshot"])
        image_path = _required_str(vision, "image_path")
        image_hash = _required_str(vision, "image_sha256")
        width = _required_int(vision, "width")
        height = _required_int(vision, "height")
        apple_payload = _observation_payload(_value(vision, "observation"))
        if not isinstance(apple_payload, dict):
            raise ValueError("Apple Vision evidence is missing")
        apple = canonicalize_vision_payload(
            apple_payload,
            page=page,
            width=width,
            height=height,
            image_sha256=image_hash,
        )
        _validate_apple_observation(apple, page, width, height, image_hash)

        if "codex" not in current:
            self._check_deadline(deadline)
            current["status"] = PageStatus.PRIMARY_OCR.value
            self._persist(state)
            result = self._call_engine(
                self.codex.transcribe_page,
                image_path,
                page_number=page,
                width=width,
                height=height,
                expected_image_sha256=image_hash,
                deadline=deadline,
            )
            current["codex"] = _jsonable_object(result)
        codex_payload = _value(current["codex"], "payload")
        if not isinstance(codex_payload, dict):
            raise ValueError("Codex evidence is missing")
        try:
            validate_persisted_payload(
                codex_payload,
                kind="transcription",
                page=page,
                width=width,
                height=height,
            )
        except CodexVisionError:
            raise ValueError("Codex evidence schema is invalid") from None
        codex = _codex_observation(codex_payload, image_hash, page, width, height)
        page_hash = str(image_hash or _fingerprint(vision))
        input_fingerprint = _fingerprint(
            {"batch": state["input_fingerprint"], "page": page, "image_sha256": image_hash}
        )

        if "alignment" not in current:
            self._check_deadline(deadline)
            current["status"] = PageStatus.DIFFING.value
            self._persist(state)
            current["alignment"] = _alignment_payload(
                apple,
                codex,
                page=page,
                page_hash=page_hash,
                input_fingerprint=input_fingerprint,
                sample_rate=sample_rate,
            )
        alignment = current["alignment"]
        status = _required_str(alignment, "status")
        baidu_observation: object | None = None
        if needs_baidu(page_hash, page, status, sample_rate=sample_rate):
            if "baidu" not in current:
                self._check_deadline(deadline)
                current["status"] = PageStatus.BAIDU_PENDING.value
                self._persist(state)
                authorization = authorize_baidu_escalation(
                    page_hash,
                    page,
                    status,
                    input_fingerprint=input_fingerprint,
                    sample_rate=sample_rate,
                )
                if authorization is None:
                    raise ValueError("Baidu escalation authorization is missing")
                if self.baidu is None:
                    raise ValueError("Baidu OCR engine is unavailable")
                image = self._call_engine(self.image_loader, image_path, deadline=deadline)
                if hashlib.sha256(image).hexdigest() != image_hash.lower():
                    raise ValueError("Baidu OCR image snapshot mismatch")
                baidu_observation = self._call_engine(
                    self.baidu.recognize,
                    image,
                    authorization=authorization,
                    page_hash=page_hash,
                    input_fingerprint=input_fingerprint,
                    page=page,
                    alignment_status=status,
                    deadline=deadline,
                )
                current["baidu"] = _baidu_envelope(
                    baidu_observation,
                    page_hash=page_hash,
                    input_fingerprint=input_fingerprint,
                    page=page,
                    alignment_status=status,
                )
            else:
                baidu_observation = _validate_baidu_envelope(
                    current["baidu"],
                    page_hash=page_hash,
                    input_fingerprint=input_fingerprint,
                    page=page,
                    alignment_status=status,
                )

        expected_conflicts = _expected_adjudication_conflicts(
            alignment,
            apple=apple,
            codex=codex,
            baidu=baidu_observation,
            page=page,
            width=width,
            height=height,
            image_hash=image_hash,
        )
        adjudication_diff = _jsonable_object(alignment)
        adjudication_diff["conflicts"] = _jsonable(expected_conflicts)

        if "decision" not in current:
            self._check_deadline(deadline)
            current["status"] = PageStatus.ADJUDICATING.value
            self._persist(state)
            result = self._call_engine(
                self.codex.adjudicate_page,
                image_path,
                page_number=page,
                width=width,
                height=height,
                codex_observation=codex,
                apple_observation=apple,
                diff=adjudication_diff,
                baidu_observation=baidu_observation,
                expected_image_sha256=image_hash,
                deadline=deadline,
            )
            decision = _value(result, "payload")
            try:
                validate_persisted_payload(
                    decision,
                    kind="adjudication",
                    page=page,
                    width=width,
                    height=height,
                )
            except CodexVisionError:
                raise _EvidenceBlocked("final adjudication schema is invalid") from None
            _validate_decision(
                decision,
                page,
                width,
                height,
                expected_conflict_ids=_conflict_ids(expected_conflicts),
            )
            current["decision"] = _jsonable_object(result)
        else:
            try:
                validate_persisted_payload(
                    _value(current["decision"], "payload"),
                    kind="adjudication",
                    page=page,
                    width=width,
                    height=height,
                )
            except CodexVisionError:
                raise _EvidenceBlocked("final adjudication schema is invalid") from None
            _validate_decision(
                _value(current["decision"], "payload"),
                page,
                width,
                height,
                expected_conflict_ids=_conflict_ids(expected_conflicts),
            )
        current["page_input_fingerprint"] = input_fingerprint
        current["evidence_fingerprint"] = _fingerprint(
            {
                "vision": vision,
                "codex": codex,
                "alignment": alignment,
                "baidu": baidu_observation,
                "decision": current["decision"],
            }
        )
        self._check_control(deadline)
        current["status"] = PageStatus.COMPLETED.value
        current.pop("error", None)
        self._persist(state)
        self._check_control(deadline)

    def _load_or_create_state(
        self,
        pdf_path: str | Path,
        pages: Sequence[int],
        dpi: int,
        languages: Sequence[str],
        sample_rate: float,
        *,
        deadline: float | None,
    ) -> _BatchState:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._check_control(deadline)
        snapshot = _snapshot_pdf(
            pdf_path,
            deadline=deadline,
            cancel_event=self.cancel_event,
        )
        self._check_control(deadline)
        fingerprint = _fingerprint(
            {
                "pdf_snapshot": snapshot,
                "pages": list(pages),
                "dpi": dpi,
                "languages": list(languages),
                "sample_rate": sample_rate,
            }
        )
        run_config: _RunConfig = {
            "pages": list(pages),
            "dpi": dpi,
            "languages": list(languages),
            "sample_rate": sample_rate,
        }
        path = self.state_root / "batch-state.json"
        try:
            value = _read_batch_state(path, deadline=deadline, cancel_event=self.cancel_event)
        except FileNotFoundError:
            pass
        except InterruptedError:
            raise _BatchCancelled() from None
        else:
            if value["input_fingerprint"] == fingerprint and value["run_config"] == run_config:
                return value
        return {
            "schema_version": _BATCH_STATE_SCHEMA_VERSION,
            "status": BatchStatus.RUNNING.value,
            "input_fingerprint": fingerprint,
            "pdf_snapshot": snapshot,
            "run_config": run_config,
            "pages": {str(page): {"status": PageStatus.PENDING.value} for page in pages},
            "updated_at": int(time.time()),
        }

    def _completed_final_for_request(
        self,
        pdf_path: str | Path,
        pages: Sequence[int],
        dpi: int,
        languages: Sequence[str],
        sample_rate: float,
        *,
        deadline: float | None,
    ) -> _BatchState | None:
        try:
            state = _read_batch_state(
                self.state_root / "batch-state.json",
                deadline=deadline,
            )
            final = _read_batch_state(
                self.state_root / "batch-final.json",
                deadline=deadline,
            )
        except (FileNotFoundError, _StateInvalid):
            return None

        snapshot = _snapshot_pdf(pdf_path, deadline=deadline)
        run_config: _RunConfig = {
            "pages": list(pages),
            "dpi": dpi,
            "languages": list(languages),
            "sample_rate": sample_rate,
        }
        fingerprint = _fingerprint(
            {
                "pdf_snapshot": snapshot,
                "pages": list(pages),
                "dpi": dpi,
                "languages": list(languages),
                "sample_rate": sample_rate,
            }
        )
        if (
            state != final
            or final["status"] != BatchStatus.COMPLETED.value
            or final["pdf_snapshot"] != snapshot
            or final["run_config"] != run_config
            or final["input_fingerprint"] != fingerprint
        ):
            return None
        for page in pages:
            current = final["pages"][str(page)]
            if current.get("status") != PageStatus.COMPLETED.value:
                return None
            if not self._completed_evidence_is_valid(final, current, page, sample_rate):
                return None
        return final

    @staticmethod
    def _reset_page(current: _PageState) -> None:
        _clear_mapping(current)
        current.update({"status": PageStatus.PENDING.value, "attempts": 0})

    def _completed_evidence_is_valid(
        self,
        state: object,
        current: object,
        page: int,
        sample_rate: float,
    ) -> bool:
        if not _is_batch_state(state) or not _is_page_state(current):
            return False
        try:
            vision = current["vision"]
            codex_record = current["codex"]
            alignment = current["alignment"]
            decision_record = current["decision"]
            snapshot = state["pdf_snapshot"]
            if not isinstance(vision, dict) or not isinstance(codex_record, dict):
                return False
            if not isinstance(decision_record, dict):
                return False
            if not isinstance(codex_record.get("record"), dict):
                return False
            if not isinstance(decision_record.get("record"), dict):
                return False
            if _value(vision, "pdf_sha256") != snapshot["sha256"]:
                return False
            if _value(vision, "page") != page:
                return False
            image_hash = _value(vision, "image_sha256")
            if not isinstance(image_hash, str) or not image_hash:
                return False
            width = _required_int(vision, "width")
            height = _required_int(vision, "height")
            apple_payload = _observation_payload(_value(vision, "observation"))
            codex_payload = _value(codex_record, "payload")
            if not isinstance(apple_payload, dict) or not isinstance(codex_payload, dict):
                return False
            apple = canonicalize_vision_payload(
                apple_payload,
                page=page,
                width=width,
                height=height,
                image_sha256=image_hash,
            )
            _validate_apple_observation(apple, page, width, height, image_hash)
            validate_persisted_payload(
                codex_payload,
                kind="transcription",
                page=page,
                width=width,
                height=height,
            )
            if apple.get("input_fingerprint") != image_hash:
                return False
            codex = _codex_observation(codex_payload, image_hash, page, width, height)
            page_input = _fingerprint(
                {"batch": state["input_fingerprint"], "page": page, "image_sha256": image_hash}
            )
            expected_alignment = _alignment_payload(
                apple,
                codex,
                page=page,
                page_hash=image_hash,
                input_fingerprint=page_input,
                sample_rate=sample_rate,
            )
            if alignment != expected_alignment:
                return False
            if current.get("page_input_fingerprint") != page_input:
                return False
            status = _required_str(alignment, "status")
            baidu = current.get("baidu")
            if needs_baidu(image_hash, page, status, sample_rate=sample_rate):
                if baidu is None:
                    return False
                baidu_response = _validate_baidu_envelope(
                    baidu,
                    page_hash=image_hash,
                    input_fingerprint=page_input,
                    page=page,
                    alignment_status=status,
                )
            elif baidu is not None:
                return False
            else:
                baidu_response = None
            expected_conflicts = _expected_adjudication_conflicts(
                alignment,
                apple=apple,
                codex=codex,
                baidu=baidu_response,
                page=page,
                width=width,
                height=height,
                image_hash=image_hash,
            )
            validate_persisted_payload(
                _value(decision_record, "payload"),
                kind="adjudication",
                page=page,
                width=width,
                height=height,
            )
            _validate_decision(
                _value(decision_record, "payload"),
                page,
                width,
                height,
                expected_conflict_ids=_conflict_ids(expected_conflicts),
            )
            expected = _fingerprint(
                {
                    "vision": vision,
                    "codex": codex,
                    "alignment": alignment,
                    "baidu": baidu_response,
                    "decision": decision_record,
                }
            )
            return current.get("evidence_fingerprint") == expected
        except (KeyError, TypeError, ValueError, CodexVisionError):
            return False

    def _discard_final_artifact(self) -> None:
        target = self.state_root / "batch-final.json"
        try:
            info = target.lstat()
            if info.st_nlink == 1 and stat.S_ISREG(info.st_mode):
                target.unlink()
        except FileNotFoundError:
            pass

    def _call_engine[Result](
        self,
        function: Callable[..., Result],
        *args: object,
        deadline: float | None,
        **kwargs: object,
    ) -> Result:
        self._check_control(deadline)
        try:
            result = function(
                *args,
                deadline=deadline,
                cancel_event=self.cancel_event,
                **kwargs,
            )
        except BaseException:
            self._check_control(deadline)
            raise
        self._check_control(deadline)
        return result

    def _set_status(self, state: _BatchState, status: BatchStatus) -> None:
        state["status"] = status.value
        self._persist(state)

    def _finish(
        self,
        state: _BatchState,
        status: BatchStatus,
        error: str | None,
        page_runs: dict[int, PageRun],
    ) -> BatchRun:
        with self._publication_transaction(deadline=None):
            if self._completed_transaction_exists_unlocked():
                return BatchRun(status, self._page_runs(state["pages"]), error)
            if status is not BatchStatus.COMPLETED:
                self._discard_final_artifact()
            state["status"] = status.value
            state["error"] = error
            self._persist_unlocked(state)
        return BatchRun(status, self._page_runs(state["pages"]), error)

    def _persist(self, state: _BatchState) -> None:
        with self._publication_transaction(deadline=None):
            if (
                state.get("status") != BatchStatus.COMPLETED.value
                and self._completed_transaction_exists_unlocked()
            ):
                return
            self._persist_unlocked(state)

    def _persist_unlocked(self, state: _BatchState) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        target = self.state_root / "batch-state.json"
        encoded = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        atomic_write_bytes(
            target=target,
            data=encoded,
            temporary_prefix=".batch-state.",
            writer=_write_file,
            sync_file=_sync_file,
            close_file=_close_file,
            open_directory=_open_directory,
            replace_file=_replace_file,
            sync_directory=_sync_directory,
        )

    def _publish_atomically(self, state: _BatchState, *, deadline: float | None) -> None:
        with self._publication_transaction(deadline=deadline):
            self._publish_atomically_unlocked(state, deadline=deadline)

    def _publish_atomically_unlocked(
        self,
        state: _BatchState,
        *,
        deadline: float | None,
    ) -> None:
        target = self.state_root / "batch-final.json"
        self._check_control(deadline)
        encoded = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

        def replace_at_commit_point(source: Path, destination: Path, directory_fd: int) -> None:
            self._check_control(deadline)
            _replace_file(source, destination, directory_fd)

        atomic_write_bytes(
            target=target,
            data=encoded,
            temporary_prefix=".batch-final.",
            writer=_write_file,
            sync_file=_sync_file,
            close_file=_close_file,
            open_directory=_open_directory,
            replace_file=replace_at_commit_point,
            sync_directory=_sync_directory,
        )

    @contextmanager
    def _publication_transaction(self, *, deadline: float | None) -> Iterator[None]:
        from .workflow import _publication_transaction_lock, workflow_paths

        depth = int(getattr(self._publication_local, "depth", 0))
        if depth:
            self._publication_local.depth = depth + 1
            try:
                yield
            finally:
                self._publication_local.depth = depth
            return
        with _publication_transaction_lock(
            workflow_paths(self.state_root),
            deadline=deadline,
            cancel_event=self.cancel_event if deadline is not None else None,
        ):
            self._publication_local.depth = 1
            try:
                yield
            finally:
                self._publication_local.depth = 0

    def _completed_transaction_exists_unlocked(self) -> bool:
        try:
            state = _read_batch_state(self.state_root / "batch-state.json")
            final = _read_batch_state(self.state_root / "batch-final.json")
            if state != final or final["status"] != BatchStatus.COMPLETED.value:
                return False
            run_config = final["run_config"]
            sample_rate = run_config["sample_rate"]
            pages = run_config["pages"]
            return all(
                final["pages"].get(str(page), {}).get("status") == PageStatus.COMPLETED.value
                and self._completed_evidence_is_valid(
                    final,
                    final["pages"][str(page)],
                    page,
                    sample_rate,
                )
                for page in pages
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, _StateInvalid):
            return False

    @staticmethod
    def _page_runs(page_state: Mapping[str, _PageState]) -> dict[int, PageRun]:
        return {
            int(page): PageRun(
                int(page),
                PageStatus(value.get("status", PageStatus.PENDING.value)),
                value.get("error"),
                value.get("evidence_fingerprint"),
            )
            for page, value in page_state.items()
        }

    @staticmethod
    def _is_contiguous(pages: Sequence[int]) -> bool:
        if not pages or len(set(pages)) != len(pages) or any(page < 1 for page in pages):
            return False
        return list(pages) == list(range(pages[0], pages[0] + len(pages)))

    @staticmethod
    def _load_image(
        path: str | Path,
        *,
        deadline: float | None = None,
        cancel_event: CancellationEvent | None = None,
    ) -> bytes:
        _check_external_control(deadline, cancel_event)
        candidate = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(candidate, flags)
        except OSError:
            raise ValueError("OCR image is unavailable") from None
        chunks: list[bytes] = []
        size = 0
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("OCR image is unavailable")
            while True:
                _check_external_control(deadline, cancel_event)
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise ValueError("OCR image is too large")
                chunks.append(chunk)
        finally:
            os.close(fd)
        _check_external_control(deadline, cancel_event)
        return b"".join(chunks)

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("OCR batch timed out")

    def _check_control(self, deadline: float | None) -> None:
        if self.cancel_event.is_set():
            raise _BatchCancelled()
        self._check_deadline(deadline)


def _check_external_control(deadline: float | None, cancel_event: CancellationEvent | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("OCR image loading cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("OCR batch timed out")


def _value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    result: object = getattr(value, name, default)
    return result


def _jsonable(value: object) -> _JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _jsonable_object(value: object) -> _JsonObject:
    result = _jsonable(value)
    if not isinstance(result, dict):
        raise ValueError("OCR engine result is invalid")
    return result


def _required_str(value: Mapping[str, object], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ValueError("OCR evidence is invalid")
    return result


def _required_int(value: Mapping[str, object], name: str) -> int:
    result = value.get(name)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError("OCR evidence is invalid")
    return result


def _clear_mapping(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("OCR page state is invalid")
    value.clear()


def _is_json_value(value: object) -> TypeGuard[_JsonValue]:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _is_json_object(value: object) -> TypeGuard[_JsonObject]:
    return isinstance(value, dict) and all(
        isinstance(key, str) and _is_json_value(item) for key, item in value.items()
    )


def _read_batch_state(
    path: str | Path,
    *,
    deadline: float | None = None,
    cancel_event: CancellationEvent | None = None,
) -> _BatchState:
    candidate = Path(path)
    _check_external_control(deadline, cancel_event)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(candidate, flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise _StateInvalid("persisted OCR state is unsafe") from None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size <= 0
            or before.st_size > _MAX_BATCH_STATE_BYTES
        ):
            raise _StateInvalid("persisted OCR state is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            _check_external_control(deadline, cancel_event)
            chunk = os.read(fd, min(remaining, _STATE_READ_CHUNK_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if remaining or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _StateInvalid("persisted OCR state changed while reading")
    finally:
        os.close(fd)
    _check_external_control(deadline, cancel_event)
    try:
        value: object = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _StateInvalid("persisted OCR state is invalid") from None
    if not _is_batch_state(value):
        raise _StateInvalid("persisted OCR state is invalid")
    return value


def _interrupted_run_config(state_path: str | Path, source_path: str | Path) -> _RunConfig | None:
    state = _read_batch_state(state_path)
    if state["status"] != BatchStatus.RUNNING.value:
        return None
    if state["pdf_snapshot"] != _snapshot_pdf(source_path):
        return None
    return {
        "pages": list(state["run_config"]["pages"]),
        "dpi": state["run_config"]["dpi"],
        "languages": list(state["run_config"]["languages"]),
        "sample_rate": state["run_config"]["sample_rate"],
    }


def _is_batch_state(value: object) -> TypeGuard[_BatchState]:
    if not isinstance(value, dict):
        return False
    allowed_fields = {
        "schema_version",
        "status",
        "input_fingerprint",
        "pdf_snapshot",
        "run_config",
        "pages",
        "updated_at",
        "error",
    }
    if set(value) - allowed_fields or value.get("schema_version") != _BATCH_STATE_SCHEMA_VERSION:
        return False
    if value.get("status") not in {status.value for status in BatchStatus}:
        return False
    input_fingerprint = value.get("input_fingerprint")
    if (
        not isinstance(input_fingerprint, str)
        or len(input_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in input_fingerprint)
    ):
        return False
    if not isinstance(value.get("updated_at"), int):
        return False
    snapshot = value.get("pdf_snapshot")
    if not isinstance(snapshot, dict):
        return False
    if set(snapshot) != {"sha256", "size"}:
        return False
    if (
        not isinstance(snapshot.get("sha256"), str)
        or len(snapshot["sha256"]) != 64
        or not isinstance(snapshot.get("size"), int)
        or isinstance(snapshot["size"], bool)
        or snapshot["size"] <= 0
    ):
        return False
    run_config = value.get("run_config")
    if not isinstance(run_config, dict) or set(run_config) != {
        "pages",
        "dpi",
        "languages",
        "sample_rate",
    }:
        return False
    configured_pages = run_config.get("pages")
    dpi = run_config.get("dpi")
    languages = run_config.get("languages")
    sample_rate = run_config.get("sample_rate")
    if (
        not isinstance(configured_pages, list)
        or not configured_pages
        or any(
            not isinstance(page, int) or isinstance(page, bool) or page <= 0
            for page in configured_pages
        )
        or len(set(configured_pages)) != len(configured_pages)
        or not isinstance(dpi, int)
        or isinstance(dpi, bool)
        or not 72 <= dpi <= 1200
        or not isinstance(languages, list)
        or not languages
        or any(not isinstance(language, str) or not language for language in languages)
        or not isinstance(sample_rate, int | float)
        or isinstance(sample_rate, bool)
        or not math.isfinite(float(sample_rate))
        or not 0 <= sample_rate <= 1
    ):
        return False
    pages = value.get("pages")
    if not isinstance(pages, dict):
        return False
    if set(pages) != {str(page) for page in configured_pages}:
        return False
    for page_number, page_state in pages.items():
        if not isinstance(page_number, str) or not _is_page_state(page_state):
            return False
    error = value.get("error")
    if error is not None and (
        not isinstance(error, str) or re.fullmatch(r"ocr_[a-z0-9_]+", error) is None
    ):
        return False
    expected_fingerprint = _fingerprint(
        {
            "pdf_snapshot": snapshot,
            "pages": configured_pages,
            "dpi": dpi,
            "languages": languages,
            "sample_rate": sample_rate,
        }
    )
    return input_fingerprint == expected_fingerprint


def _is_page_state(value: object) -> TypeGuard[_PageState]:
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return False
    attempts = value.get("attempts")
    if attempts is not None and not isinstance(attempts, int):
        return False
    for field in ("error", "page_input_fingerprint", "evidence_fingerprint"):
        field_value = value.get(field)
        if field_value is not None and not isinstance(field_value, str):
            return False
    for field in ("vision", "codex", "alignment", "baidu", "decision"):
        if field in value and not _is_json_object(value[field]):
            return False
    return True


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_pdf(
    path: str | Path,
    *,
    deadline: float | None = None,
    cancel_event: CancellationEvent | None = None,
) -> _PdfSnapshot:
    candidate = Path(path)
    try:
        _check_external_control(deadline, cancel_event)
        link_info = candidate.lstat()
        if stat.S_ISLNK(link_info.st_mode):
            raise ValueError
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError
            if before.st_size <= 0 or before.st_size > 2 * 1024 * 1024 * 1024:
                raise ValueError
            if os.pread(fd, 5, 0) != b"%PDF-":
                raise ValueError
            digest = hashlib.sha256()
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                _check_external_control(deadline, cancel_event)
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                _check_external_control(deadline, cancel_event)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise ValueError
            _check_external_control(deadline, cancel_event)
            return {"sha256": digest.hexdigest(), "size": before.st_size}
        finally:
            os.close(fd)
    except (InterruptedError, TimeoutError):
        raise
    except (OSError, ValueError):
        raise ValueError("PDF source is invalid") from None


def _observation_payload(value: object) -> object:
    if isinstance(value, Mapping) and set(value) >= {"payload_json"}:
        try:
            value = json.loads(value["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
    return value


def _validate_vision_pdf_snapshot(vision: object, snapshot: object) -> None:
    if not isinstance(vision, Mapping) or not isinstance(snapshot, Mapping):
        raise ValueError("Apple Vision evidence is missing")
    if vision.get("pdf_sha256") != snapshot.get("sha256"):
        raise ValueError("Apple Vision PDF snapshot mismatch")


def _validate_apple_observation(
    apple: object, page: int, width: int, height: int, image_hash: str
) -> None:
    if not isinstance(apple, dict) or apple.get("input_fingerprint") != image_hash:
        raise ValueError("Apple Vision evidence is invalid")
    if apple.get("page") != {"number": page, "width": width, "height": height}:
        raise ValueError("Apple Vision evidence is invalid")
    if not isinstance(apple.get("blocks"), list):
        raise ValueError("Apple Vision evidence is invalid")
    # compare_observations performs the detailed block/region checks used by the
    # production alignment path; this guard prevents a metadata-only forgery.
    for block in apple["blocks"]:
        if not isinstance(block, dict) or not isinstance(block.get("id"), str):
            raise ValueError("Apple Vision evidence is invalid")


def _codex_observation(
    payload: Mapping[str, object], image_hash: str, page: int, width: int, height: int
) -> _JsonObject:
    observation = {str(key): _jsonable(value) for key, value in payload.items()}
    observation.update(
        {
            "id": f"codex-{_fingerprint(payload)[:24]}",
            "engine": "codex_vision",
            "input_fingerprint": image_hash,
            "page": {"number": page, "width": width, "height": height},
        }
    )
    return observation


def _alignment_payload(
    apple: object,
    codex: object,
    *,
    page: int,
    page_hash: str,
    input_fingerprint: str,
    sample_rate: float,
) -> _JsonObject:
    comparison = compare_observations(apple, codex)
    classification = classify_page(apple, codex)
    status = classification.value
    return _jsonable_object(
        {
            "page": page,
            "page_hash": page_hash,
            "input_fingerprint": input_fingerprint,
            "status": status,
            "baidu_required": needs_baidu(page_hash, page, status, sample_rate=sample_rate),
            "conflicts": [
                _conflict_record(asdict(conflict), source="apple-codex", page=page)
                for conflict in comparison.conflicts
            ],
            "matched_blocks": comparison.matched_blocks,
        }
    )


def _quantized_coordinate(value: object) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        return 0.0
    return round(float(value), 6)


def _conflict_record(conflict: Mapping[str, object], *, source: str, page: int) -> _JsonObject:
    record = _jsonable_object(conflict)
    region = record.get("region")
    quantized_region = (
        {key: _quantized_coordinate(region.get(key, 0.0)) for key in ("x", "y", "width", "height")}
        if isinstance(region, Mapping)
        else region
    )
    identity = {
        "source": source,
        "page": page,
        "reason": record.get("reason"),
        "region": quantized_region,
        "apple_text": record.get("apple_text"),
        "codex_text": record.get("codex_text"),
        "apple_block_id": record.get("apple_block_id"),
        "codex_block_id": record.get("codex_block_id"),
    }
    return {
        "id": f"{source}-{_fingerprint(identity)[:24]}",
        "source": source,
        **record,
    }


def _expected_adjudication_conflicts(
    alignment: object,
    *,
    apple: object,
    codex: object,
    baidu: object | None,
    page: int,
    width: int,
    height: int,
    image_hash: str,
) -> list[_JsonObject]:
    if not isinstance(alignment, Mapping) or not isinstance(alignment.get("conflicts"), list):
        raise _EvidenceBlocked("alignment conflicts are invalid")
    conflicts = []
    for conflict in alignment["conflicts"]:
        if not isinstance(conflict, dict):
            raise _EvidenceBlocked("alignment conflicts are invalid")
        conflicts.append(_jsonable_object(conflict))
    if baidu is not None:
        baidu_observation = _baidu_comparison_observation(
            baidu,
            page=page,
            width=width,
            height=height,
            image_hash=image_hash,
        )
        for source, primary in (("apple-baidu", apple), ("codex-baidu", codex)):
            comparison = compare_observations(primary, baidu_observation)
            conflicts.extend(
                _conflict_record(asdict(conflict), source=source, page=page)
                for conflict in comparison.conflicts
            )
    _conflict_ids(conflicts)
    return conflicts


def _baidu_comparison_observation(
    response: object,
    *,
    page: int,
    width: int,
    height: int,
    image_hash: str,
) -> _JsonObject:
    if not isinstance(response, Mapping):
        raise _EvidenceBlocked("Baidu OCR evidence is invalid")
    data_info = response.get("data_info")
    blocks = response.get("blocks")
    if (
        response.get("engine") != "baidu_pp_structure"
        or not isinstance(response.get("request_id"), str)
        or not isinstance(data_info, Mapping)
        or data_info.get("type") != "image"
        or data_info.get("width") != width
        or data_info.get("height") != height
        or not isinstance(blocks, list)
        or not blocks
        or any(not isinstance(block, dict) for block in blocks)
    ):
        raise _EvidenceBlocked("Baidu OCR evidence is invalid")
    return {
        "id": f"baidu-{response['request_id']}",
        "engine": "baidu_pp_structure",
        "input_fingerprint": image_hash,
        "page": {"number": page, "width": width, "height": height},
        "blocks": _jsonable(blocks),
    }


def _conflict_ids(conflicts: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    identifiers: list[str] = []
    for conflict in conflicts:
        identifier = conflict.get("id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise _EvidenceBlocked("expected conflict id is invalid")
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise _EvidenceBlocked("expected conflict ids are not unique")
    return tuple(identifiers)


def _baidu_envelope(
    response: object,
    *,
    page_hash: str,
    input_fingerprint: str,
    page: int,
    alignment_status: str,
) -> _JsonObject:
    if not isinstance(response, dict):
        raise ValueError("Baidu OCR evidence is invalid")
    normalized_response = _jsonable_object(response)
    return {
        "page_hash": page_hash,
        "input_fingerprint": input_fingerprint,
        "page": page,
        "alignment_status": alignment_status,
        "observation_ref": _fingerprint(normalized_response),
        "response": normalized_response,
    }


def _validate_baidu_envelope(
    envelope: object,
    *,
    page_hash: str,
    input_fingerprint: str,
    page: int,
    alignment_status: str,
) -> _JsonObject:
    if not isinstance(envelope, dict):
        raise ValueError("Baidu OCR evidence is invalid")
    expected = {
        "page_hash": page_hash,
        "input_fingerprint": input_fingerprint,
        "page": page,
        "alignment_status": alignment_status,
    }
    if any(envelope.get(key) != value for key, value in expected.items()):
        raise ValueError("Baidu OCR evidence context mismatch")
    response = envelope.get("response")
    if not isinstance(response, dict) or envelope.get("observation_ref") != _fingerprint(response):
        raise ValueError("Baidu OCR evidence reference is invalid")
    return response


def _validate_decision(
    value: object,
    page: int,
    width: int,
    height: int,
    *,
    expected_conflict_ids: Sequence[str],
) -> None:
    if not isinstance(value, dict):
        raise _EvidenceBlocked("final adjudication is missing")
    required = {
        "page",
        "final_blocks",
        "resolved_conflicts",
        "tables",
        "formulas",
        "decision_evidence",
        "confidence",
        "status",
    }
    if set(value) != required:
        raise _EvidenceBlocked("final adjudication schema is invalid")
    if (
        not isinstance(value["final_blocks"], list)
        or not isinstance(value["resolved_conflicts"], list)
        or not isinstance(value["decision_evidence"], list)
    ):
        raise _EvidenceBlocked("final adjudication schema is invalid")
    if value["page"] != {"number": page, "width": width, "height": height}:
        raise _EvidenceBlocked("final adjudication schema is invalid")
    confidence = value["confidence"]
    if (
        not isinstance(confidence, int | float)
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or confidence < MIN_FINAL_ADJUDICATION_CONFIDENCE
        or confidence > 1
    ):
        raise _EvidenceBlocked("final adjudication confidence is insufficient")
    if value["status"] != "accepted":
        raise _EvidenceBlocked("final adjudication is unresolved")
    if not value["final_blocks"]:
        raise _EvidenceBlocked("final adjudication blocks are missing")
    if not value["decision_evidence"] or any(
        not isinstance(item, str) or not 1 <= len(item.strip()) <= 4096
        for item in value["decision_evidence"]
    ):
        raise _EvidenceBlocked("final adjudication evidence is missing")
    expected_ids = tuple(expected_conflict_ids)
    if len(expected_ids) != len(set(expected_ids)):
        raise _EvidenceBlocked("expected conflict ids are not unique")
    resolved_ids: list[str] = []
    for conflict in value["resolved_conflicts"]:
        if not isinstance(conflict, dict):
            raise _EvidenceBlocked("resolved conflict is invalid")
        identifier = conflict.get("id")
        decision = conflict.get("decision")
        evidence = conflict.get("evidence")
        conflict_confidence = conflict.get("confidence")
        if (
            not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 128
            or not isinstance(decision, str)
            or not 1 <= len(decision.strip()) <= 8192
            or not isinstance(evidence, list)
            or not 1 <= len(evidence) <= 32
            or any(
                not isinstance(item, str) or not 1 <= len(item.strip()) <= 4096 for item in evidence
            )
            or not isinstance(conflict_confidence, int | float)
            or isinstance(conflict_confidence, bool)
            or not math.isfinite(float(conflict_confidence))
            or not MIN_FINAL_ADJUDICATION_CONFIDENCE <= conflict_confidence <= 1
        ):
            raise _EvidenceBlocked("resolved conflict is uncertain")
        resolved_ids.append(identifier)
    if len(resolved_ids) != len(set(resolved_ids)):
        raise _EvidenceBlocked("resolved conflict ids are duplicated")
    if set(resolved_ids) != set(expected_ids) or len(resolved_ids) != len(expected_ids):
        raise _EvidenceBlocked("resolved conflict ids do not match evidence")
    for block in value["final_blocks"]:
        if not isinstance(block, dict):
            raise _EvidenceBlocked("final adjudication block is invalid")
        block_confidence = block.get("confidence")
        if (
            not isinstance(block_confidence, int | float)
            or isinstance(block_confidence, bool)
            or not math.isfinite(float(block_confidence))
            or block_confidence < MIN_FINAL_ADJUDICATION_CONFIDENCE
        ):
            raise _EvidenceBlocked("final adjudication block confidence is insufficient")
        if primary_block_uncertainty_reason(block) is not None:
            raise _EvidenceBlocked("final adjudication block is unresolved")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, _StateInvalid):
        return exc.code
    message = str(exc).lower()
    if "schema" in message:
        return "ocr_schema_invalid"
    if "timeout" in message or "timed out" in message:
        return "ocr_timeout"
    if "page" in message and "sequence" in message:
        return "ocr_page_sequence_incomplete"
    return "ocr_engine_failed"
