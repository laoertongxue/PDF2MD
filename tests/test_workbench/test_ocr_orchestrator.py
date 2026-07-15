from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from parsing_core.workbench.ocr.orchestrator import (
    BatchStatus,
    OcrOrchestrator,
    PageStatus,
)


def _observation(engine: str, text: str = "一致文本") -> dict:
    return {
        "id": f"{engine}-observation",
        "engine": engine,
        "input_fingerprint": "image-sha",
        "page": {"number": 1, "width": 1200, "height": 1600},
        "blocks": [
            {
                "id": f"{engine}-block",
                "type": "paragraph",
                "text": text,
                "region": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
                "confidence": 0.99,
                "reading_order": 1,
                "candidates": [],
                "uncertainty_reason": "",
                "table": None,
                "formula": None,
                "source_region": "r1",
            }
        ],
        "uncertain_items": [],
        "reading_order": [f"{engine}-block"],
    }


@dataclass
class FakeEngines:
    codex_failures: int = 0
    apple_text: str = "一致文本"
    codex_text: str = "一致文本"

    def __post_init__(self):
        self.pdf_sha256 = "pdf-sha"
        self.calls: list[str] = []
        self.vision = SimpleNamespace(recognize=self._vision)
        self.codex = SimpleNamespace(
            transcribe_page=self._transcribe,
            adjudicate_page=self._adjudicate,
        )
        self.baidu = SimpleNamespace(recognize=self._baidu)

    def _vision(self, pdf_path, *, page, dpi, languages):
        self.calls.append(f"vision:{page}")
        return SimpleNamespace(
            page=page,
            image_path="/trusted/page.png",
            image_sha256="image-sha",
            width=1200,
            height=1600,
            pdf_sha256=self.pdf_sha256,
            observation=_observation("apple_vision", self.apple_text),
        )

    def _transcribe(self, image_path, *, page_number, width, height, expected_image_sha256):
        self.calls.append(f"codex:{page_number}")
        if self.codex_failures:
            self.codex_failures -= 1
            raise RuntimeError("codex unavailable /Users/private/book.pdf")
        payload = _observation("codex_vision", self.codex_text)
        payload.pop("id")
        payload.pop("engine")
        payload.pop("input_fingerprint")
        return SimpleNamespace(
            payload=payload,
            record={"engine": "codex_vision", "evidence_sha256": "codex-record-sha"},
        )

    def _baidu(self, image, **kwargs):
        self.calls.append(f"baidu:{kwargs['page']}")
        return {
            "engine": "baidu_pp_structure",
            "observations": [_observation("baidu_pp_structure")],
        }

    def _adjudicate(self, image_path, *, page_number, width, height, codex_observation,
                    apple_observation, diff, baidu_observation=None, **kwargs):
        self.calls.append(f"adjudicate:{page_number}")
        return SimpleNamespace(
            payload={
                "page": {"number": page_number, "width": width, "height": height},
                "final_blocks": _observation("codex_vision")["blocks"],
                "resolved_conflicts": [],
                "tables": [],
                "formulas": [],
                "decision_evidence": ["bounded evidence"],
                "confidence": 0.98,
                "status": "accepted",
            },
            record={"engine": "codex_vision", "evidence_sha256": "decision-sha"},
        )


def _orchestrator(tmp_path, engines, **kwargs):
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nfixture textbook\n")
    engines.pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    engines.pdf_path = pdf_path
    return OcrOrchestrator(
        vision=engines.vision,
        codex=engines.codex,
        baidu=engines.baidu,
        state_root=tmp_path / "ocr-state",
        image_loader=lambda _path: b"image-bytes",
        **kwargs,
    )


def _run(orchestrator, engines, **kwargs):
    return orchestrator.run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0, **kwargs
    )


def test_consistent_page_stays_offline_and_runs_final_adjudication(tmp_path):
    engines = FakeEngines()
    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert result.status is BatchStatus.COMPLETED
    assert engines.calls == ["vision:1", "codex:1", "adjudicate:1"]
    assert "baidu:1" not in engines.calls
    assert (tmp_path / "ocr-state" / "batch-final.json").is_file()


def test_conflict_page_uses_bound_one_time_baidu_authorization(tmp_path):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert result.status is BatchStatus.COMPLETED
    assert engines.calls == ["vision:1", "codex:1", "baidu:1", "adjudicate:1"]


def test_missing_page_blocks_batch_and_writes_no_publishable_artifact(tmp_path):
    engines = FakeEngines()
    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path, pages=[1, 3], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert result.status is BatchStatus.BLOCKED
    assert result.pages[3].status is PageStatus.FAILED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()
    state = json.loads((tmp_path / "ocr-state" / "batch-state.json").read_text())
    assert state["status"] == "blocked"
    assert "/books/book.pdf" not in json.dumps(state)


def test_failed_page_is_resumable_without_repeating_completed_vision(tmp_path):
    engines = FakeEngines(codex_failures=1)
    orchestrator = _orchestrator(tmp_path, engines)
    first = orchestrator.run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )
    assert first.status is BatchStatus.FAILED
    assert engines.calls == ["vision:1", "codex:1"]

    second = orchestrator.run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )
    assert second.status is BatchStatus.COMPLETED
    assert engines.calls == ["vision:1", "codex:1", "codex:1", "adjudicate:1"]


def test_cancel_stops_before_final_publish(tmp_path):
    engines = FakeEngines()
    def cancelled():
        return True
    result = _orchestrator(tmp_path, engines, is_cancelled=cancelled).run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert result.status is BatchStatus.CANCELLED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_invalid_final_schema_blocks_publication(tmp_path):
    engines = FakeEngines()

    def invalid_adjudication(*args, **kwargs):
        engines.calls.append("adjudicate:1")
        return SimpleNamespace(payload={"status": "accepted"}, record={})

    engines.codex.adjudicate_page = invalid_adjudication
    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert result.status is BatchStatus.FAILED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_retry_limit_is_explicit_and_bounded(tmp_path):
    engines = FakeEngines(codex_failures=3)
    orchestrator = _orchestrator(tmp_path, engines, max_page_attempts=2)
    first = orchestrator.run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )
    second = orchestrator.run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )
    third = orchestrator.run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert first.status is BatchStatus.FAILED
    assert second.status is BatchStatus.FAILED
    assert third.status is BatchStatus.FAILED
    assert engines.calls == ["vision:1", "codex:1", "codex:1"]


def test_blocking_engine_isolated_by_batch_timeout_and_cannot_publish_late(tmp_path):
    engines = FakeEngines()
    started = threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        time.sleep(1)
        engines.calls.append("late-vision")
        return engines._vision(*args, **kwargs)

    engines.vision.recognize = blocked
    orchestrator = _orchestrator(tmp_path, engines)
    began = time.monotonic()
    result = _run(orchestrator, engines, timeout=0.05)
    elapsed = time.monotonic() - began

    assert started.is_set()
    assert elapsed < 0.5
    assert result.status is BatchStatus.FAILED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()
    time.sleep(1.1)
    state = json.loads((tmp_path / "ocr-state" / "batch-state.json").read_text())
    assert state["status"] == "failed"
    assert state["pages"]["1"]["status"] != "completed"


def test_blocking_engine_isolated_by_batch_cancel(tmp_path):
    engines = FakeEngines()
    started = threading.Event()
    cancel = threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        time.sleep(1)
        return engines._vision(*args, **kwargs)

    engines.vision.recognize = blocked
    orchestrator = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    result_holder = []
    worker = threading.Thread(
        target=lambda: result_holder.append(_run(orchestrator, engines, timeout=2)),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=0.5)
    cancel.set()
    worker.join(timeout=0.5)

    assert not worker.is_alive()
    assert result_holder[0].status is BatchStatus.CANCELLED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_same_path_with_replaced_pdf_content_does_not_reuse_page_results(tmp_path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    first = _run(orchestrator, engines)
    assert first.status is BatchStatus.COMPLETED
    first_calls = list(engines.calls)
    engines.pdf_path.write_bytes(b"%PDF-1.7\nreplacement textbook\n")
    engines.pdf_sha256 = hashlib.sha256(engines.pdf_path.read_bytes()).hexdigest()

    second = _run(orchestrator, engines)

    assert second.status is BatchStatus.COMPLETED
    assert engines.calls[len(first_calls):] == ["vision:1", "codex:1", "adjudicate:1"]


def test_resume_rechecks_complete_evidence_and_reruns_when_decision_is_missing(tmp_path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    assert _run(orchestrator, engines).status is BatchStatus.COMPLETED
    state_path = tmp_path / "ocr-state" / "batch-state.json"
    state = json.loads(state_path.read_text())
    del state["pages"]["1"]["decision"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    resumed = _run(orchestrator, engines)

    assert resumed.status is BatchStatus.COMPLETED
    assert engines.calls[-1] == "adjudicate:1"
    repaired = json.loads(state_path.read_text())
    assert "decision" in repaired["pages"]["1"]
    assert (tmp_path / "ocr-state" / "batch-final.json").is_file()


@pytest.mark.parametrize("evidence_key, forged", [
    ("codex", {"record": {"forged": True}, "payload": {"forged": True}}),
    ("alignment", {"status": "consistent"}),
    ("decision", {"record": {}, "payload": {"status": "accepted"}}),
])
def test_resume_rejects_forged_evidence_and_rebuilds_page(
    tmp_path, evidence_key, forged
):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    orchestrator = _orchestrator(tmp_path, engines)
    assert _run(orchestrator, engines).status is BatchStatus.COMPLETED
    state_path = tmp_path / "ocr-state" / "batch-state.json"
    state = json.loads(state_path.read_text())
    state["pages"]["1"][evidence_key] = forged
    state_path.write_text(json.dumps(state), encoding="utf-8")

    resumed = _run(orchestrator, engines)

    assert resumed.status is BatchStatus.COMPLETED
    assert engines.calls[-4:] == ["vision:1", "codex:1", "baidu:1", "adjudicate:1"]
    assert (tmp_path / "ocr-state" / "batch-final.json").is_file()


def test_resume_rejects_forged_baidu_envelope(tmp_path):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    orchestrator = _orchestrator(tmp_path, engines)
    assert _run(orchestrator, engines).status is BatchStatus.COMPLETED
    state_path = tmp_path / "ocr-state" / "batch-state.json"
    state = json.loads(state_path.read_text())
    state["pages"]["1"]["baidu"]["response"] = {"forged": True}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    resumed = _run(orchestrator, engines)

    assert resumed.status is BatchStatus.COMPLETED
    assert engines.calls[-4:] == ["vision:1", "codex:1", "baidu:1", "adjudicate:1"]


@pytest.mark.parametrize("pdf_sha256", ["wrong", None])
def test_first_vision_result_must_match_pdf_snapshot(tmp_path, pdf_sha256):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    engines.pdf_sha256 = pdf_sha256

    result = _run(orchestrator, engines)

    assert result.status is BatchStatus.FAILED
    assert engines.calls == ["vision:1"]
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()
