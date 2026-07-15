from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .alignment import (
    authorize_baidu_escalation,
    classify_page,
    compare_observations,
    needs_baidu,
)


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


class OcrOrchestrator:
    """Run the unattended OCR state machine with a publish gate.

    Dependencies are deliberately injected. The orchestrator owns ordering,
    state durability, escalation authorization, and the final publication gate;
    engine implementations own protocol and schema validation.
    """

    def __init__(
        self,
        *,
        vision: Any,
        codex: Any,
        baidu: Any,
        state_root: str | Path,
        image_loader: Callable[[str], bytes] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        max_page_attempts: int = 2,
    ):
        self.vision = vision
        self.codex = codex
        self.baidu = baidu
        self.state_root = Path(state_root)
        self.image_loader = image_loader or self._load_image
        self.is_cancelled = is_cancelled or (lambda: False)
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
        state = self._load_or_create_state(pdf_path, page_numbers, dpi, languages, sample_rate)
        page_state = state["pages"]
        page_runs = self._page_runs(page_state)

        if not self._is_contiguous(page_numbers):
            for page in page_numbers:
                page_state[str(page)] = {
                    "status": PageStatus.FAILED.value,
                    "error": "page sequence is incomplete",
                }
            return self._finish(
                state, BatchStatus.BLOCKED, "page sequence is incomplete", page_runs
            )

        self._set_status(state, BatchStatus.RUNNING)
        for page in page_numbers:
            current = page_state[str(page)]
            if current.get("status") == PageStatus.COMPLETED.value:
                continue
            if int(current.get("attempts", 0)) >= self.max_page_attempts:
                return self._finish(state, BatchStatus.FAILED, "OCR retry limit reached", page_runs)
            try:
                if self.is_cancelled():
                    current["status"] = PageStatus.CANCELLED.value
                    self._persist(state)
                    return self._finish(state, BatchStatus.CANCELLED, "batch cancelled", page_runs)
                current["attempts"] = int(current.get("attempts", 0)) + 1
                self._check_deadline(deadline)
                self._run_page(
                    state, current, pdf_path, page, dpi, languages, sample_rate, deadline
                )
            except Exception as exc:
                current["status"] = PageStatus.FAILED.value
                current["error"] = _safe_error(exc)
                self._persist(state)
                return self._finish(state, BatchStatus.FAILED, current["error"], page_runs)

        if any(
            page_state[str(page)].get("status") != PageStatus.COMPLETED.value
            for page in page_numbers
        ):
            return self._finish(state, BatchStatus.BLOCKED, "batch is incomplete", page_runs)
        self._set_status(state, BatchStatus.COMPLETED)
        self._publish_atomically(state)
        return BatchRun(BatchStatus.COMPLETED, self._page_runs(page_state))

    def _run_page(self, state, current, pdf_path, page, dpi, languages, sample_rate, deadline):
        self._check_deadline(deadline)
        if "vision" not in current:
            current["status"] = PageStatus.RENDERING.value
            self._persist(state)
            vision_result = self.vision.recognize(pdf_path, page=page, dpi=dpi, languages=languages)
            current["vision"] = _jsonable(vision_result)
        vision = current["vision"]
        image_path = _value(vision, "image_path")
        image_hash = _value(vision, "image_sha256")
        width = _value(vision, "width")
        height = _value(vision, "height")
        apple = _observation_payload(_value(vision, "observation"))
        if not isinstance(apple, dict):
            raise ValueError("Apple Vision evidence is missing")

        if "codex" not in current:
            self._check_deadline(deadline)
            current["status"] = PageStatus.PRIMARY_OCR.value
            self._persist(state)
            result = self.codex.transcribe_page(
                image_path,
                page_number=page,
                width=width,
                height=height,
                expected_image_sha256=image_hash,
            )
            current["codex"] = _jsonable(result)
        codex = _value(current["codex"], "payload")
        if not isinstance(codex, dict):
            raise ValueError("Codex evidence is missing")

        if "alignment" not in current:
            self._check_deadline(deadline)
            current["status"] = PageStatus.DIFFING.value
            self._persist(state)
            alignment = compare_observations(apple, codex)
            classification = classify_page(apple, codex)
            current["alignment"] = _jsonable(
                {
                    "status": classification.value,
                    "conflicts": [asdict(conflict) for conflict in alignment.conflicts],
                    "matched_blocks": alignment.matched_blocks,
                }
            )
        alignment = current["alignment"]
        status = str(alignment["status"])
        page_hash = str(image_hash or _fingerprint(vision))
        input_fingerprint = str(image_hash or _fingerprint({"page": page, "vision": vision}))
        baidu_observation = None
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
                baidu_observation = self.baidu.recognize(
                    self.image_loader(image_path),
                    authorization=authorization,
                    page_hash=page_hash,
                    input_fingerprint=input_fingerprint,
                    page=page,
                    alignment_status=status,
                )
                current["baidu"] = _jsonable(baidu_observation)
            else:
                baidu_observation = current["baidu"]

        if "decision" not in current:
            self._check_deadline(deadline)
            current["status"] = PageStatus.ADJUDICATING.value
            self._persist(state)
            result = self.codex.adjudicate_page(
                image_path,
                page_number=page,
                width=width,
                height=height,
                codex_observation=codex,
                apple_observation=apple,
                diff=alignment,
                baidu_observation=baidu_observation,
                expected_image_sha256=image_hash,
            )
            decision = _value(result, "payload")
            _validate_decision(decision, page, width, height)
            current["decision"] = _jsonable(result)
        else:
            _validate_decision(_value(current["decision"], "payload"), page, width, height)
        current["evidence_fingerprint"] = _fingerprint(
            {"vision": vision, "codex": codex, "alignment": alignment, "baidu": baidu_observation,
             "decision": current["decision"]}
        )
        current["status"] = PageStatus.COMPLETED.value
        current.pop("error", None)
        self._persist(state)

    def _load_or_create_state(self, pdf_path, pages, dpi, languages, sample_rate):
        self.state_root.mkdir(parents=True, exist_ok=True)
        fingerprint = _fingerprint(
            {"pdf": str(pdf_path), "pages": list(pages), "dpi": dpi, "languages": list(languages),
             "sample_rate": sample_rate}
        )
        path = self.state_root / "batch-state.json"
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("input_fingerprint") == fingerprint and set(
                    value.get("pages", {})
                ) == {str(page) for page in pages}:
                    return value
            except Exception:
                pass
        return {
            "schema_version": 1,
            "status": BatchStatus.RUNNING.value,
            "input_fingerprint": fingerprint,
            "pages": {str(page): {"status": PageStatus.PENDING.value} for page in pages},
            "updated_at": int(time.time()),
        }

    def _set_status(self, state, status):
        state["status"] = status.value
        self._persist(state)

    def _finish(self, state, status, error, page_runs):
        state["status"] = status.value
        state["error"] = error
        self._persist(state)
        return BatchRun(status, self._page_runs(state["pages"]), error)

    def _persist(self, state):
        self.state_root.mkdir(parents=True, exist_ok=True)
        target = self.state_root / "batch-state.json"
        fd, name = tempfile.mkstemp(prefix=".batch-state.", dir=self.state_root)
        try:
            encoded = json.dumps(
                state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            os.write(fd, encoded)
            os.fsync(fd)
            os.close(fd)
            os.replace(name, target)
            directory_fd = os.open(self.state_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)

    def _publish_atomically(self, state):
        target = self.state_root / "batch-final.json"
        fd, name = tempfile.mkstemp(prefix=".batch-final.", dir=self.state_root)
        try:
            encoded = json.dumps(
                state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            os.write(fd, encoded)
            os.fsync(fd)
            os.close(fd)
            os.replace(name, target)
            directory_fd = os.open(self.state_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _page_runs(page_state):
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
    def _is_contiguous(pages):
        if not pages or len(set(pages)) != len(pages) or any(page < 1 for page in pages):
            return False
        return (
            list(pages)
            == list(range(pages[0], pages[0] + len(pages)))
        )

    @staticmethod
    def _load_image(path):
        return Path(path).read_bytes()

    @staticmethod
    def _check_deadline(deadline):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("OCR batch timed out")


def _value(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _jsonable(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observation_payload(value: Any):
    if isinstance(value, dict) and set(value) >= {"payload_json"}:
        try:
            value = json.loads(value["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
    return value


def _validate_decision(value: Any, page: int, width: int, height: int) -> None:
    if not isinstance(value, dict):
        raise ValueError("final adjudication is missing")
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
        raise ValueError("final adjudication schema is invalid")
    if not isinstance(value["final_blocks"], list) or not isinstance(
        value["decision_evidence"], list
    ):
        raise ValueError("final adjudication schema is invalid")
    if value["page"] != {"number": page, "width": width, "height": height}:
        raise ValueError("final adjudication schema is invalid")
    if not isinstance(value["confidence"], (int, float)) or not 0 <= value["confidence"] <= 1:
        raise ValueError("final adjudication schema is invalid")
    if value["status"] != "accepted":
        raise ValueError("final adjudication schema is invalid")


def _safe_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "cancel" in message:
        return "batch cancelled"
    if "schema" in message:
        return "OCR schema validation failed"
    if "timeout" in message or "timed out" in message:
        return "OCR engine timed out"
    if "page" in message and "sequence" in message:
        return "page sequence is incomplete"
    return "OCR engine failed"
