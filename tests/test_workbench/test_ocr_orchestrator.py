from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from parsing_core.serving.api import routes_workbench
from parsing_core.workbench.ocr import orchestrator as orchestrator_module
from parsing_core.workbench.ocr import workflow as workflow_module
from parsing_core.workbench.ocr.baidu import BaiduOcrClient
from parsing_core.workbench.ocr.codex_vision import CodexVisionError
from parsing_core.workbench.ocr.models import OcrObservation
from parsing_core.workbench.ocr.orchestrator import (
    BatchStatus,
    OcrOrchestrator,
    PageStatus,
)
from parsing_core.workbench.ocr.vision import VisionPageResult
from parsing_core.workbench.ocr.workflow import WorkflowStatus

_IMAGE_BYTES = b"image-bytes"
_IMAGE_SHA256 = hashlib.sha256(_IMAGE_BYTES).hexdigest()


def _observation(engine: str, text: str = "一致文本", *, block_type: str = "paragraph") -> dict:
    return {
        "id": f"{engine}-observation",
        "engine": engine,
        "input_fingerprint": _IMAGE_SHA256,
        "page": {"number": 1, "width": 1200, "height": 1600},
        "blocks": [
            {
                "id": f"{engine}-block",
                "type": block_type,
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
    baidu_text: str = "一致文本"
    apple_type: str = "paragraph"
    codex_type: str = "paragraph"
    baidu_type: str = "paragraph"

    def __post_init__(self):
        self.pdf_sha256 = "pdf-sha"
        self.calls: list[str] = []
        self.vision = SimpleNamespace(recognize=self._vision)
        self.codex = SimpleNamespace(
            transcribe_page=self._transcribe,
            adjudicate_page=self._adjudicate,
        )
        self.baidu = SimpleNamespace(recognize=self._baidu)

    def _vision(self, pdf_path, *, page, dpi, languages, **_control):
        self.calls.append(f"vision:{page}")
        return SimpleNamespace(
            page=page,
            dpi=dpi,
            language_config=tuple(languages),
            image_path="/trusted/page.png",
            image_sha256=_IMAGE_SHA256,
            width=1200,
            height=1600,
            pdf_sha256=self.pdf_sha256,
            observation=_observation("apple_vision", self.apple_text, block_type=self.apple_type),
        )

    def _transcribe(
        self, image_path, *, page_number, width, height, expected_image_sha256, **_control
    ):
        self.calls.append(f"codex:{page_number}")
        if self.codex_failures:
            self.codex_failures -= 1
            raise RuntimeError("codex unavailable /Users/private/book.pdf")
        payload = _observation("codex_vision", self.codex_text, block_type=self.codex_type)
        payload.pop("id")
        payload.pop("engine")
        payload.pop("input_fingerprint")
        return SimpleNamespace(
            payload=payload,
            record={"engine": "codex_vision", "evidence_sha256": "codex-record-sha"},
        )

    def _baidu(self, image, **kwargs):
        self.calls.append(f"baidu:{kwargs['page']}")
        observation = _observation(
            "baidu_pp_structure", self.baidu_text, block_type=self.baidu_type
        )
        return {
            "engine": "baidu_pp_structure",
            "request_id": "baidu-test-request",
            "data_info": {"type": "image", "width": 1200, "height": 1600},
            "blocks": observation["blocks"],
        }

    def _adjudicate(
        self,
        image_path,
        *,
        page_number,
        width,
        height,
        codex_observation,
        apple_observation,
        diff,
        baidu_observation=None,
        **kwargs,
    ):
        self.calls.append(f"adjudicate:{page_number}")
        resolved_conflicts = [_resolved_conflict(conflict["id"]) for conflict in diff["conflicts"]]
        return SimpleNamespace(
            payload={
                "page": {"number": page_number, "width": width, "height": height},
                "final_blocks": _observation("codex_vision")["blocks"],
                "resolved_conflicts": resolved_conflicts,
                "tables": [],
                "formulas": [],
                "decision_evidence": ["bounded evidence"],
                "confidence": 0.98,
                "status": "accepted",
            },
            record={"engine": "codex_vision", "evidence_sha256": "decision-sha"},
        )


class VisionPageResultEngines(FakeEngines):
    def __post_init__(self):
        self.apple_seen = None
        super().__post_init__()

    def _vision(self, pdf_path, *, page, dpi, languages, **_control):
        self.calls.append(f"vision:{page}")
        payload = {
            "page": page,
            "image_sha256": _IMAGE_SHA256,
            "width": 1200,
            "height": 1600,
            "supported_languages": ["zh-Hans"],
            "observations": [
                {
                    "text": "第二行",
                    "confidence": 0.99,
                    "bounding_box": {"x": 0.1, "y": 0.4, "width": 0.4, "height": 0.1},
                    "candidates": [{"text": "第二行", "confidence": 0.99}],
                },
                {
                    "text": "一致文本",
                    "confidence": 0.98,
                    "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                    "candidates": [{"text": "一致文本", "confidence": 0.98}],
                },
            ],
        }
        return VisionPageResult(
            cache_key="vision-cache-key",
            pdf_sha256=self.pdf_sha256,
            page=page,
            dpi=dpi,
            helper_version="test-helper",
            language_config=tuple(languages),
            image_path="/trusted/page.png",
            image_sha256=_IMAGE_SHA256,
            width=1200,
            height=1600,
            supported_languages=("zh-Hans",),
            observation=OcrObservation(
                id="vision-observation",
                page_id="vision-page",
                engine="apple_vision",
                input_hash=self.pdf_sha256,
                engine_config_hash="vision-config",
                payload_json=json.dumps(payload, ensure_ascii=False),
                created_at=0,
            ),
        )

    def _adjudicate(self, *args, apple_observation, **kwargs):
        self.apple_seen = apple_observation
        return super()._adjudicate(*args, apple_observation=apple_observation, **kwargs)


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
        image_loader=lambda _path, **_control: _IMAGE_BYTES,
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


def test_batch_state_and_final_atomic_writes_retry_short_writes(tmp_path, monkeypatch):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    real_write = os.write
    write_sizes = []

    def short_write(fd, data):
        write_sizes.append(len(data))
        return real_write(fd, data[:11])

    monkeypatch.setattr(orchestrator_module, "_write_file", short_write, raising=False)

    result = _run(orchestrator, engines)

    assert result.status is BatchStatus.COMPLETED
    assert len(write_sizes) > 2
    for name in ("batch-state.json", "batch-final.json"):
        target = tmp_path / "ocr-state" / name
        assert json.loads(target.read_text(encoding="utf-8"))["status"] == "completed"
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("publish_method", ["_persist", "_publish_atomically"])
def test_batch_atomic_write_opens_directory_before_creating_temporary(
    tmp_path, monkeypatch, publish_method
):
    from parsing_core.workbench.ocr import atomic_io

    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0, deadline=None
    )
    events = []
    real_open_directory = orchestrator_module._open_directory
    real_create_temporary = atomic_io._create_temporary

    def record_open(path):
        events.append("open-directory")
        return real_open_directory(path)

    def record_create(directory_fd, prefix):
        os.fstat(directory_fd)
        events.append("create-temporary")
        return real_create_temporary(directory_fd, prefix)

    monkeypatch.setattr(orchestrator_module, "_open_directory", record_open)
    monkeypatch.setattr(atomic_io, "_create_temporary", record_create)

    if publish_method == "_persist":
        orchestrator._persist(state)
    else:
        orchestrator._publish_atomically(state, deadline=None)

    assert events[:2] == ["open-directory", "create-temporary"]


@pytest.mark.parametrize("failure_mode", ["zero", "error"])
@pytest.mark.parametrize(
    ("artifact_name", "temporary_pattern", "publish_method"),
    [
        ("batch-state.json", ".batch-state.*", "_persist"),
        ("batch-final.json", ".batch-final.*", "_publish_atomically"),
    ],
)
def test_batch_atomic_write_failure_preserves_target_cleans_temp_and_closes_fd(
    tmp_path,
    monkeypatch,
    failure_mode,
    artifact_name,
    temporary_pattern,
    publish_method,
):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path,
        [1],
        300,
        ["zh-Hans"],
        0,
        deadline=None,
    )
    target = orchestrator.state_root / artifact_name
    target.write_bytes(b"previous")
    failure = OSError("disk full")
    opened_fds = []

    def fail_write(fd, _data):
        opened_fds.append(fd)
        if failure_mode == "zero":
            return 0
        raise failure

    monkeypatch.setattr(orchestrator_module, "_write_file", fail_write, raising=False)

    with pytest.raises(OSError) as error:
        if publish_method == "_persist":
            orchestrator._persist(state)
        else:
            orchestrator._publish_atomically(state, deadline=None)

    if failure_mode == "zero":
        assert "no progress" in str(error.value)
    else:
        assert error.value is failure
    assert target.read_bytes() == b"previous"
    assert list(orchestrator.state_root.glob(temporary_pattern)) == []
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    ("publish_method", "temporary_pattern"),
    [("_persist", ".batch-state.*"), ("_publish_atomically", ".batch-final.*")],
)
def test_batch_atomic_writes_close_each_fd_once_without_closing_reused_fd(
    tmp_path, monkeypatch, publish_method, temporary_pattern
):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0, deadline=None
    )
    reused_path = tmp_path / f"reused-{publish_method}"
    real_close = os.close
    opened_fd = None
    reused_fd = None
    close_calls = []

    def record_write(fd, data):
        nonlocal opened_fd
        opened_fd = fd
        return os.write(fd, data)

    def close_and_reuse(fd):
        nonlocal reused_fd
        close_calls.append(fd)
        real_close(fd)
        if fd == opened_fd and reused_fd is None:
            reused_fd = os.open(reused_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            assert reused_fd == fd

    monkeypatch.setattr(orchestrator_module, "_write_file", record_write)
    monkeypatch.setattr(orchestrator_module, "_close_file", close_and_reuse, raising=False)

    try:
        if publish_method == "_persist":
            orchestrator._persist(state)
        else:
            orchestrator._publish_atomically(state, deadline=None)

        assert opened_fd is not None
        assert close_calls.count(opened_fd) == 1
        assert all(close_calls.count(fd) == 1 for fd in set(close_calls))
        assert reused_fd == opened_fd
        os.fstat(reused_fd)
        assert list(orchestrator.state_root.glob(temporary_pattern)) == []
    finally:
        if reused_fd is not None:
            real_close(reused_fd)


@pytest.mark.parametrize(
    ("artifact_name", "publish_method"),
    [("batch-state.json", "_persist"), ("batch-final.json", "_publish_atomically")],
)
def test_batch_directory_open_failure_is_precommit_and_preserves_original(
    tmp_path, monkeypatch, artifact_name, publish_method
):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0, deadline=None
    )
    target = orchestrator.state_root / artifact_name
    target.write_bytes(b"previous")
    failure = OSError("directory open failed")

    def fail_directory_open(_path):
        raise failure

    monkeypatch.setattr(orchestrator_module, "_open_directory", fail_directory_open, raising=False)

    with pytest.raises(OSError) as error:
        if publish_method == "_persist":
            orchestrator._persist(state)
        else:
            orchestrator._publish_atomically(state, deadline=None)

    assert error.value is failure
    assert target.read_bytes() == b"previous"


@pytest.mark.parametrize(
    ("artifact_name", "publish_method"),
    [("batch-state.json", "_persist"), ("batch-final.json", "_publish_atomically")],
)
def test_batch_directory_fsync_failure_reports_committed_artifact(
    tmp_path, monkeypatch, artifact_name, publish_method
):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0, deadline=None
    )
    target = orchestrator.state_root / artifact_name
    target.write_bytes(b"previous")
    failure = OSError("directory fsync failed")

    def fail_directory_fsync(_fd):
        raise failure

    monkeypatch.setattr(orchestrator_module, "_sync_directory", fail_directory_fsync, raising=False)

    with pytest.raises(OSError) as error:
        if publish_method == "_persist":
            orchestrator._persist(state)
        else:
            orchestrator._publish_atomically(state, deadline=None)

    assert type(error.value).__name__ == "AtomicCommitError"
    assert getattr(error.value, "committed", False) is True
    assert getattr(error.value, "durability_uncertain", False) is True
    assert error.value.__cause__ is failure
    assert json.loads(target.read_text(encoding="utf-8")) == state


@pytest.mark.parametrize("publish_method", ["_persist", "_publish_atomically"])
def test_batch_cleanup_failure_does_not_mask_write_error(tmp_path, monkeypatch, publish_method):
    from parsing_core.workbench.ocr import atomic_io

    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0, deadline=None
    )
    write_failure = OSError("write failed")
    cleanup_calls = []

    def fail_write(_fd, _data):
        raise write_failure

    def fail_cleanup(name, directory_fd):
        os.fstat(directory_fd)
        cleanup_calls.append(name)
        raise OSError("cleanup failed")

    monkeypatch.setattr(orchestrator_module, "_write_file", fail_write)
    monkeypatch.setattr(atomic_io, "_unlink_name", fail_cleanup)

    with pytest.raises(OSError) as error:
        if publish_method == "_persist":
            orchestrator._persist(state)
        else:
            orchestrator._publish_atomically(state, deadline=None)

    assert error.value is write_failure
    assert len(cleanup_calls) == 1


def test_batch_state_persists_exact_versioned_run_configuration(tmp_path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)

    result = orchestrator.run_batch(
        engines.pdf_path,
        pages=[1],
        dpi=320,
        languages=["en-US", "zh-Hans"],
        sample_rate=0.2,
    )

    assert result.status is BatchStatus.COMPLETED
    state = json.loads((tmp_path / "ocr-state" / "batch-state.json").read_text())
    assert state["schema_version"] == 2
    assert state["run_config"] == {
        "pages": [1],
        "dpi": 320,
        "languages": ["en-US", "zh-Hans"],
        "sample_rate": 0.2,
    }


def test_batch_state_read_rejects_in_place_change_with_restored_mtime(tmp_path, monkeypatch):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state = orchestrator._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0, deadline=None
    )
    orchestrator._persist(state)
    path = orchestrator.state_root / "batch-state.json"
    before = path.stat()
    real_read = os.read
    changed = False

    def read_then_touch(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            writer = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(writer, chunk[:1], 0)
            finally:
                os.close(writer)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return chunk

    monkeypatch.setattr(orchestrator_module.os, "read", read_then_touch)

    with pytest.raises(
        orchestrator_module._StateInvalid,
        match="changed while reading",
    ):
        orchestrator_module._read_batch_state(path)

    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns


def test_real_vision_page_result_is_canonicalized_before_orchestration(tmp_path):
    engines = VisionPageResultEngines()

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.COMPLETED
    assert engines.apple_seen is not None
    assert engines.apple_seen["page"] == {"number": 1, "width": 1200, "height": 1600}
    assert engines.apple_seen["input_fingerprint"] == _IMAGE_SHA256
    assert [block["text"] for block in engines.apple_seen["blocks"]] == [
        "一致文本",
        "第二行",
    ]
    assert engines.apple_seen["reading_order"] == [
        block["id"] for block in engines.apple_seen["blocks"]
    ]
    assert engines.apple_seen["blocks"][0]["bounding_box"] == {
        "x": 0.1,
        "y": 0.1,
        "width": 0.4,
        "height": 0.1,
    }
    assert all(block["uncertainty_reason"] == "" for block in engines.apple_seen["blocks"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(confidence=0.0),
        lambda payload: payload.update(final_blocks=[]),
        lambda payload: payload.update(decision_evidence=[]),
        lambda payload: payload["final_blocks"][0].update(confidence=0.5),
        lambda payload: payload["final_blocks"][0].update(uncertainty_reason="仍有歧义"),
        lambda payload: payload["final_blocks"][0].update(text=""),
    ],
)
def test_uncertain_final_adjudication_is_blocked_and_never_published(tmp_path, mutate):
    engines = FakeEngines()
    adjudicate = engines.codex.adjudicate_page

    def uncertain_adjudication(*args, **kwargs):
        result = adjudicate(*args, **kwargs)
        mutate(result.payload)
        return result

    engines.codex.adjudicate_page = uncertain_adjudication

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"
    assert result.pages[1].error == "ocr_evidence_uncertain"
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def _resolved_conflict(conflict_id: str, *, confidence: float = 0.98) -> dict:
    return {
        "id": conflict_id,
        "region": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
        "evidence": ["image region", "engine disagreement"],
        "decision": "以可见字形和版面证据裁决",
        "confidence": confidence,
    }


def test_alignment_conflict_with_empty_resolved_conflicts_is_blocked(tmp_path):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    adjudicate = engines.codex.adjudicate_page

    def omit_resolutions(*args, **kwargs):
        result = adjudicate(*args, **kwargs)
        result.payload["resolved_conflicts"] = []
        return result

    engines.codex.adjudicate_page = omit_resolutions

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_alignment_conflict_resolution_with_zero_confidence_is_blocked(tmp_path):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    adjudicate = engines.codex.adjudicate_page

    def zero_confidence(*args, **kwargs):
        result = adjudicate(*args, **kwargs)
        conflict_id = kwargs["diff"]["conflicts"][0].get("id", "review-conflict")
        result.payload["resolved_conflicts"] = [_resolved_conflict(conflict_id, confidence=0.0)]
        return result

    engines.codex.adjudicate_page = zero_confidence

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"


@pytest.mark.parametrize("attack", ["unknown", "duplicate", "missing"])
def test_conflict_resolution_ids_must_exactly_match_expected_set(tmp_path, attack):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    adjudicate = engines.codex.adjudicate_page

    def forged_resolution(*args, **kwargs):
        result = adjudicate(*args, **kwargs)
        conflict_ids = [
            conflict.get("id", f"review-conflict-{index}")
            for index, conflict in enumerate(kwargs["diff"]["conflicts"])
        ]
        if attack == "unknown":
            resolved_ids = [*conflict_ids, "unknown-conflict"]
        elif attack == "duplicate":
            resolved_ids = [*conflict_ids, conflict_ids[0]]
        else:
            resolved_ids = conflict_ids[:-1]
        result.payload["resolved_conflicts"] = [
            _resolved_conflict(conflict_id) for conflict_id in resolved_ids
        ]
        return result

    engines.codex.adjudicate_page = forged_resolution

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"


def test_baidu_sampling_mismatch_requires_explicit_conflict_resolution(tmp_path):
    engines = FakeEngines(baidu_text="百度识别为不同文本")
    adjudicate = engines.codex.adjudicate_page

    def omit_resolutions(*args, **kwargs):
        result = adjudicate(*args, **kwargs)
        result.payload["resolved_conflicts"] = []
        return result

    engines.codex.adjudicate_page = omit_resolutions

    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path,
        pages=[1],
        dpi=300,
        languages=["zh-Hans"],
        sample_rate=1,
    )

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"


def test_structural_type_mismatch_requires_explicit_conflict_resolution(tmp_path):
    engines = FakeEngines(codex_type="formula")
    adjudicate = engines.codex.adjudicate_page
    observed_conflicts = []

    def omit_resolutions(*args, **kwargs):
        observed_conflicts.extend(kwargs["diff"]["conflicts"])
        result = adjudicate(*args, **kwargs)
        result.payload["resolved_conflicts"] = []
        return result

    engines.codex.adjudicate_page = omit_resolutions

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"
    assert any(
        conflict["source"] == "apple-codex" and conflict["reason"] == "structure_type_conflict"
        for conflict in observed_conflicts
    )


def test_baidu_structural_type_mismatch_enters_expected_conflicts(tmp_path):
    engines = FakeEngines(baidu_type="table")
    adjudicate = engines.codex.adjudicate_page
    observed_conflicts = []

    def omit_resolutions(*args, **kwargs):
        observed_conflicts.extend(kwargs["diff"]["conflicts"])
        result = adjudicate(*args, **kwargs)
        result.payload["resolved_conflicts"] = []
        return result

    engines.codex.adjudicate_page = omit_resolutions

    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path,
        pages=[1],
        dpi=300,
        languages=["zh-Hans"],
        sample_rate=1,
    )

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"
    assert {(conflict["source"], conflict["reason"]) for conflict in observed_conflicts} == {
        ("apple-baidu", "structure_type_conflict"),
        ("codex-baidu", "structure_type_conflict"),
    }


def test_conflict_id_quantizes_equivalent_bbox_coordinates():
    conflict = {
        "reason": "structure_type_conflict",
        "region": {"x": 0.3, "y": 0.1, "width": 0.4, "height": 0.1},
        "apple_text": "净现值",
        "codex_text": "净现值",
        "apple_block_id": "apple-block",
        "codex_block_id": "codex-block",
        "evidence": {
            "candidate_structures": {
                "apple": {"type": "paragraph"},
                "codex": {"type": "formula"},
            }
        },
    }
    equivalent = {
        **conflict,
        "region": {**conflict["region"], "x": 0.1 + 0.2},
    }
    different = {
        **conflict,
        "region": {**conflict["region"], "x": 0.300002},
    }

    first = orchestrator_module._conflict_record(conflict, source="apple-codex", page=1)
    second = orchestrator_module._conflict_record(equivalent, source="apple-codex", page=1)
    boundary = orchestrator_module._conflict_record(different, source="apple-codex", page=1)

    assert first["id"] == second["id"]
    assert first["id"] != boundary["id"]


def test_expected_conflict_ids_are_stable_for_identical_primary_and_baidu_evidence(
    tmp_path,
):
    observed_ids = []
    for name in ("first", "second"):
        run_root = tmp_path / name
        run_root.mkdir()
        engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
        adjudicate = engines.codex.adjudicate_page

        def capture(*args, adjudicate=adjudicate, **kwargs):
            observed_ids.append([conflict["id"] for conflict in kwargs["diff"]["conflicts"]])
            return adjudicate(*args, **kwargs)

        engines.codex.adjudicate_page = capture
        assert _run(_orchestrator(run_root, engines), engines).status is BatchStatus.COMPLETED

    assert observed_ids[0] == observed_ids[1]
    assert len(observed_ids[0]) == len(set(observed_ids[0]))
    assert any(identifier.startswith("apple-codex-") for identifier in observed_ids[0])
    assert any(
        identifier.startswith(("apple-baidu-", "codex-baidu-")) for identifier in observed_ids[0]
    )


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


def test_valid_committed_final_wins_over_cancel_during_resume_validation(tmp_path, monkeypatch):
    engines = FakeEngines()
    first = _orchestrator(tmp_path, engines)
    assert _run(first, engines).status is BatchStatus.COMPLETED

    cancel = threading.Event()
    resumed = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    validate = resumed._completed_evidence_is_valid

    def validate_then_cancel(*args, **kwargs):
        valid = validate(*args, **kwargs)
        cancel.set()
        return valid

    monkeypatch.setattr(resumed, "_completed_evidence_is_valid", validate_then_cancel)

    result = _run(resumed, engines)

    assert cancel.is_set()
    assert result.status is BatchStatus.COMPLETED
    assert (tmp_path / "ocr-state" / "batch-final.json").is_file()
    state = json.loads((tmp_path / "ocr-state" / "batch-state.json").read_text())
    assert state["status"] == BatchStatus.COMPLETED.value


def test_cancel_at_loop_end_prevents_completed_state_and_final_publish(tmp_path, monkeypatch):
    engines = FakeEngines()
    cancel = threading.Event()
    orchestrator = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    persist = orchestrator._persist

    def persist_then_cancel(state):
        persist(state)
        page = state["pages"]["1"]
        if page.get("status") == PageStatus.COMPLETED.value:
            cancel.set()

    monkeypatch.setattr(orchestrator, "_persist", persist_then_cancel)

    result = _run(orchestrator, engines)

    assert result.status is BatchStatus.CANCELLED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_cancel_after_batch_completed_state_prevents_final_publish(tmp_path, monkeypatch):
    engines = FakeEngines()
    cancel = threading.Event()
    orchestrator = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    set_status = orchestrator._set_status

    def set_status_then_cancel(state, status):
        set_status(state, status)
        if status is BatchStatus.COMPLETED:
            cancel.set()

    monkeypatch.setattr(orchestrator, "_set_status", set_status_then_cancel)

    result = _run(orchestrator, engines)

    assert result.status is BatchStatus.CANCELLED
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_cancel_after_final_atomic_replace_keeps_committed_artifact(tmp_path, monkeypatch):
    engines = FakeEngines()
    cancel = threading.Event()
    orchestrator = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    replace = os.replace
    replace_calls = []

    def replace_then_cancel(source, target, directory_fd):
        replace_calls.append(Path(target).name)
        replace(
            Path(source).name,
            Path(target).name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        if Path(target).name == "batch-final.json":
            cancel.set()

    monkeypatch.setattr(orchestrator_module, "_replace_file", replace_then_cancel, raising=False)

    result = _run(orchestrator, engines)

    target = tmp_path / "ocr-state" / "batch-final.json"
    assert cancel.is_set()
    assert "batch-final.json" in replace_calls
    assert result.status is BatchStatus.COMPLETED
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "completed"


def test_cross_instance_cancel_cannot_delete_or_overwrite_committed_final(tmp_path, monkeypatch):
    winner_engines = FakeEngines()
    winner = _orchestrator(tmp_path, winner_engines)
    loser_engines = FakeEngines()
    loser = _orchestrator(tmp_path, loser_engines)
    loser_state = loser._load_or_create_state(
        loser_engines.pdf_path,
        [1],
        300,
        ["zh-Hans"],
        0,
        deadline=None,
    )
    loser_pages = loser._page_runs(loser_state["pages"])
    final_replaced = threading.Event()
    release_winner = threading.Event()
    loser_started = threading.Event()
    loser_done = threading.Event()
    real_replace = orchestrator_module._replace_file
    results = {}
    errors = []

    def pause_after_final_replace(source, target, directory_fd):
        real_replace(source, target, directory_fd)
        if Path(target).name == "batch-final.json":
            final_replaced.set()
            assert release_winner.wait(5)

    def run_winner():
        try:
            results["winner"] = _run(winner, winner_engines)
        except BaseException as exc:
            errors.append(exc)

    def finish_loser():
        loser_started.set()
        try:
            results["loser"] = loser._finish(
                loser_state,
                BatchStatus.CANCELLED,
                "ocr_cancelled",
                loser_pages,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            loser_done.set()

    monkeypatch.setattr(orchestrator_module, "_replace_file", pause_after_final_replace)
    winner_thread = threading.Thread(target=run_winner)
    loser_thread = threading.Thread(target=finish_loser)
    try:
        winner_thread.start()
        assert final_replaced.wait(5)
        final_path = winner.state_root / "batch-final.json"
        assert json.loads(final_path.read_text(encoding="utf-8"))["status"] == "completed"

        loser_thread.start()
        assert loser_started.wait(1)
        assert loser_done.wait(0.25) is False
        assert json.loads(final_path.read_text(encoding="utf-8"))["status"] == "completed"
    finally:
        release_winner.set()
        winner_thread.join(timeout=5)
        loser_thread.join(timeout=5)

    assert not winner_thread.is_alive()
    assert not loser_thread.is_alive()
    assert errors == []
    assert results["winner"].status is BatchStatus.COMPLETED
    assert results["loser"].status is BatchStatus.CANCELLED
    final = json.loads((winner.state_root / "batch-final.json").read_text(encoding="utf-8"))
    state = json.loads((winner.state_root / "batch-state.json").read_text(encoding="utf-8"))
    assert final["status"] == "completed"
    assert state == final


def test_committed_final_with_uncertain_durability_survives_cancelled_retry(tmp_path, monkeypatch):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    real_replace = orchestrator_module._replace_file
    real_sync_directory = orchestrator_module._sync_directory
    final_replaced = False
    sync_failure = OSError("final directory fsync failed")

    def mark_final_replace(source, target, directory_fd):
        nonlocal final_replaced
        real_replace(source, target, directory_fd)
        if Path(target).name == "batch-final.json":
            final_replaced = True

    def fail_after_final_replace(fd):
        if final_replaced:
            raise sync_failure
        real_sync_directory(fd)

    monkeypatch.setattr(orchestrator_module, "_replace_file", mark_final_replace)
    monkeypatch.setattr(orchestrator_module, "_sync_directory", fail_after_final_replace)

    with pytest.raises(OSError) as error:
        _run(orchestrator, engines)

    target = tmp_path / "ocr-state" / "batch-final.json"
    assert type(error.value).__name__ == "AtomicCommitError"
    assert getattr(error.value, "target", None) == target
    assert error.value.__cause__ is sync_failure
    committed_bytes = target.read_bytes()
    completed_calls = list(engines.calls)

    monkeypatch.setattr(orchestrator_module, "_replace_file", real_replace)
    monkeypatch.setattr(orchestrator_module, "_sync_directory", real_sync_directory)
    cancel = threading.Event()
    cancel.set()
    retry = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)

    result = _run(retry, engines)

    assert result.status is BatchStatus.COMPLETED
    assert target.read_bytes() == committed_bytes
    assert engines.calls == completed_calls


def test_cancelled_retry_discards_stale_mismatched_final(tmp_path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    assert _run(orchestrator, engines).status is BatchStatus.COMPLETED
    state_root = tmp_path / "ocr-state"
    target = state_root / "batch-final.json"
    stale = json.loads(target.read_text(encoding="utf-8"))
    stale["updated_at"] += 1
    target.write_text(json.dumps(stale), encoding="utf-8")
    cancel = threading.Event()
    cancel.set()

    result = _run(_orchestrator(tmp_path, engines, is_cancelled=cancel.is_set), engines)

    assert result.status is BatchStatus.CANCELLED
    assert not target.exists()
    state = json.loads((state_root / "batch-state.json").read_text(encoding="utf-8"))
    assert state["status"] == BatchStatus.COMPLETED.value


def test_invalid_final_schema_blocks_publication(tmp_path):
    engines = FakeEngines()

    def invalid_adjudication(*args, **kwargs):
        engines.calls.append("adjudicate:1")
        return SimpleNamespace(payload={"status": "accepted"}, record={})

    engines.codex.adjudicate_page = invalid_adjudication
    result = _orchestrator(tmp_path, engines).run_batch(
        engines.pdf_path, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0
    )

    assert result.status is BatchStatus.BLOCKED
    assert result.error == "ocr_evidence_uncertain"
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
    release = threading.Event()

    def blocked(*args, deadline=None, cancel_event=None, **kwargs):
        started.set()
        while not release.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        engines.calls.append("late-vision")
        return engines._vision(*args, **kwargs)

    engines.vision.recognize = blocked
    orchestrator = _orchestrator(tmp_path, engines)
    began = time.monotonic()
    try:
        result = _run(orchestrator, engines, timeout=0.05)
        elapsed = time.monotonic() - began
        leaked_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.name == "ocr-engine-call" and thread.is_alive()
        ]

        assert started.is_set()
        assert elapsed < 0.5
        assert result.status is BatchStatus.FAILED
        assert leaked_threads == []
        assert not (tmp_path / "ocr-state" / "batch-final.json").exists()
        state = json.loads((tmp_path / "ocr-state" / "batch-state.json").read_text())
        assert state["status"] == "failed"
        assert state["pages"]["1"]["status"] != "completed"
    finally:
        release.set()


def test_blocking_engine_isolated_by_batch_cancel(tmp_path):
    engines = FakeEngines()
    started = threading.Event()
    cancel = threading.Event()
    release = threading.Event()

    def blocked(*args, deadline=None, cancel_event=None, **kwargs):
        started.set()
        while not release.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        return engines._vision(*args, **kwargs)

    engines.vision.recognize = blocked
    orchestrator = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    result_holder = []
    worker = threading.Thread(
        target=lambda: result_holder.append(_run(orchestrator, engines, timeout=2)),
        name="orchestrator-cancel-test",
    )
    worker.start()
    try:
        assert started.wait(timeout=0.5)
        cancel.set()
        worker.join(timeout=0.5)
        leaked_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.name == "ocr-engine-call" and thread.is_alive()
        ]

        assert not worker.is_alive()
        assert result_holder[0].status is BatchStatus.CANCELLED
        assert leaked_threads == []
        assert not (tmp_path / "ocr-state" / "batch-final.json").exists()
    finally:
        release.set()
        worker.join(timeout=1)


def test_deadline_adapter_forwards_deadline_and_cancel_to_all_engines():
    calls = []

    class Client:
        def recognize(self, *args, deadline=None, cancel_event=None, **kwargs):
            calls.append(("recognize", deadline, cancel_event))

        def transcribe_page(self, *args, deadline=None, cancel_event=None, **kwargs):
            calls.append(("transcribe", deadline, cancel_event))

        def adjudicate_page(self, *args, deadline=None, cancel_event=None, **kwargs):
            calls.append(("adjudicate", deadline, cancel_event))

    deadline = time.monotonic() + 10
    cancel = threading.Event()
    adapter = routes_workbench._DeadlineAdapter(Client())

    adapter.recognize("source", deadline=deadline, cancel_event=cancel)
    adapter.transcribe_page("image", deadline=deadline, cancel_event=cancel)
    adapter.adjudicate_page("image", deadline=deadline, cancel_event=cancel)

    assert calls == [
        ("recognize", deadline, cancel),
        ("transcribe", deadline, cancel),
        ("adjudicate", deadline, cancel),
    ]


def test_production_image_loader_checks_cancel_before_file_access(tmp_path):
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(InterruptedError, match="cancelled"):
        routes_workbench._load_ocr_image(
            tmp_path / "does-not-exist.png",
            deadline=time.monotonic() + 10,
            cancel_event=cancel,
        )


def test_production_factory_trusts_only_vision_published_pages(tmp_path, monkeypatch):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfactory trusted root\n")
    source = SimpleNamespace(id="trusted-pages-source", file_path=str(pdf))
    course = SimpleNamespace(root_dir=str(tmp_path / "course"))
    captured = {}

    class VisionFixture:
        def __init__(self, *, cache_root, **_kwargs):
            from parsing_core.workbench.ocr.page_cache import PageCache

            self.cache = PageCache(cache_root)
            captured["vision"] = self

    class CodexFixture:
        def __init__(self, *, trusted_image_root, **_kwargs):
            captured["trusted_image_root"] = Path(trusted_image_root)

    monkeypatch.setattr(routes_workbench, "_find_vision_helper", lambda: tmp_path / "vision")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda: tmp_path / "codex")
    monkeypatch.setattr(routes_workbench, "VisionClient", VisionFixture)
    monkeypatch.setattr(routes_workbench, "CodexVisionExecutor", CodexFixture)
    monkeypatch.setattr(routes_workbench, "BaiduOcrClient", lambda **_kwargs: object())
    monkeypatch.setattr(routes_workbench, "RegisteredPdfSources", lambda _paths: object())
    monkeypatch.setenv("PDF2MD_BAIDU_API_KEY", "test-key")

    routes_workbench._OCR_WORKFLOWS.pop(source.id, None)
    workflow = routes_workbench._ocr_workflow(source, course)
    try:
        workflow._factory(lambda: False)
        pages_root = captured["vision"].cache.pages_dir
        assert captured["trusted_image_root"] == pages_root
        assert captured["trusted_image_root"] != workflow.paths.root
        assert captured["trusted_image_root"].is_relative_to(workflow.paths.root / "cache")
    finally:
        routes_workbench._OCR_WORKFLOWS.pop(source.id, None)


def test_baidu_upload_is_bound_to_vision_hash_and_mismatch_is_not_persisted(tmp_path):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    image = tmp_path / "vision-page.png"
    expected_bytes = b"expected-image"
    image.write_bytes(b"replacement-image")
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()

    def vision(pdf_path, *, page, dpi, languages, **_control):
        engines.calls.append(f"vision:{page}")
        apple = _observation("apple_vision", engines.apple_text)
        apple["input_fingerprint"] = expected_hash
        return SimpleNamespace(
            page=page,
            image_path=str(image),
            image_sha256=expected_hash,
            width=1200,
            height=1600,
            pdf_sha256=engines.pdf_sha256,
            observation=apple,
        )

    engines.vision.recognize = vision
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\nimage hash binding\n")
    engines.pdf_path = pdf
    engines.pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    orchestrator = OcrOrchestrator(
        vision=engines.vision,
        codex=engines.codex,
        baidu=engines.baidu,
        state_root=tmp_path / "ocr-state",
        image_loader=OcrOrchestrator._load_image,
    )

    result = _run(orchestrator, engines)

    assert result.status is BatchStatus.FAILED
    assert "baidu:1" not in engines.calls
    state = json.loads((tmp_path / "ocr-state" / "batch-state.json").read_text())
    assert "baidu" not in state["pages"]["1"]
    assert not (tmp_path / "ocr-state" / "batch-final.json").exists()


def test_engine_error_containing_cancel_text_is_still_generic_failed(tmp_path):
    engines = FakeEngines()

    def spoofed_cancel(*_args, **_kwargs):
        raise RuntimeError("remote payload says cancel accepted")

    engines.codex.transcribe_page = spoofed_cancel

    result = _run(_orchestrator(tmp_path, engines), engines)

    assert result.status is BatchStatus.FAILED
    assert result.error == "ocr_engine_failed"
    assert result.pages[1].status is PageStatus.FAILED
    assert result.pages[1].error == "ocr_engine_failed"


def test_production_factory_maps_cancelled_codex_version_probe_to_cancelled_workflow(
    tmp_path, monkeypatch
):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\nversion probe cancellation\n")
    source = SimpleNamespace(id="version-cancel-source", file_path=str(pdf))
    course = SimpleNamespace(root_dir=str(tmp_path))
    probe_started = threading.Event()
    probe_finished = threading.Event()

    class CancelledVersionProbe:
        def __init__(self, *, cancel_event, **_kwargs):
            probe_started.set()
            while not cancel_event.is_set():
                time.sleep(0.01)
            probe_finished.set()
            raise CodexVisionError("codex cli cancelled")

    monkeypatch.setattr(workflow_module, "count_pdf_pages", lambda _path: 1)
    monkeypatch.setattr(routes_workbench, "_find_vision_helper", lambda: tmp_path / "vision")
    monkeypatch.setattr(routes_workbench, "resolve_codex_path", lambda: tmp_path / "codex")
    monkeypatch.setattr(routes_workbench, "RegisteredPdfSources", lambda _paths: object())

    class VisionFixture:
        def __init__(self, *, cache_root, **_kwargs):
            self.cache = SimpleNamespace(pages_dir=Path(cache_root) / "pages")

    monkeypatch.setattr(routes_workbench, "VisionClient", VisionFixture)
    monkeypatch.setattr(routes_workbench, "CodexVisionExecutor", CancelledVersionProbe)
    monkeypatch.setenv("PDF2MD_BAIDU_API_KEY", "test-key")

    routes_workbench._OCR_WORKFLOWS.pop(source.id, None)
    workflow = routes_workbench._ocr_workflow(source, course)
    try:
        workflow.start()
        assert probe_started.wait(timeout=1)
        workflow.cancel()
        deadline = time.monotonic() + 1
        while workflow.status()["status"] == WorkflowStatus.RUNNING.value:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert workflow.status()["status"] == WorkflowStatus.CANCELLED.value
        assert probe_finished.is_set()
        assert workflow._thread is not None and not workflow._thread.is_alive()
    finally:
        routes_workbench._OCR_WORKFLOWS.pop(source.id, None)


def test_baidu_cancel_waits_for_cooperative_transport_before_returning(tmp_path):
    engines = FakeEngines(apple_text="利润为 10%", codex_text="利润为 40%")
    cancel = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def transport(_request, *, timeout=None, cancel_event=None):
        started.set()
        while not release.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            time.sleep(min(0.01, timeout or 0.01))
        finished.set()
        raise RuntimeError("transport stopped")

    engines.baidu = BaiduOcrClient(
        api_key="secret-key", transport=transport, timeout=2, max_retries=0
    )
    orchestrator = _orchestrator(tmp_path, engines, is_cancelled=cancel.is_set)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(_run(orchestrator, engines, timeout=3)),
        name="baidu-orchestrator-cancel-test",
    )
    worker.start()
    try:
        assert started.wait(timeout=1)
        cancel.set()
        worker.join(timeout=1)
        leaked_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.name == "ocr-engine-call" and thread.is_alive()
        ]

        assert not worker.is_alive()
        assert results[0].status is BatchStatus.CANCELLED
        assert finished.is_set()
        assert leaked_threads == []
    finally:
        release.set()
        worker.join(timeout=1)


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
    assert engines.calls[len(first_calls) :] == ["vision:1", "codex:1", "adjudicate:1"]


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


@pytest.mark.parametrize(
    "evidence_key, forged",
    [
        ("codex", {"record": {"forged": True}, "payload": {"forged": True}}),
        ("alignment", {"status": "consistent"}),
        ("decision", {"record": {}, "payload": {"status": "accepted"}}),
    ],
)
def test_resume_rejects_forged_evidence_and_rebuilds_page(tmp_path, evidence_key, forged):
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


@pytest.mark.parametrize("attack", ["symlink", "oversize", "insecure_mode"])
def test_batch_state_recovery_rejects_untrusted_regular_file_attacks(tmp_path, attack):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state_root = tmp_path / "ocr-state"
    state_root.mkdir()
    state_path = state_root / "batch-state.json"
    if attack == "symlink":
        outside = tmp_path / "outside-state.json"
        outside.write_text("{}", encoding="utf-8")
        state_path.symlink_to(outside)
    elif attack == "oversize":
        state_path.write_bytes(b"x" * (orchestrator_module._MAX_BATCH_STATE_BYTES + 1))
        state_path.chmod(0o600)
    else:
        state_path.write_text("{}", encoding="utf-8")
        state_path.chmod(0o644)

    began = time.monotonic()
    result = _run(orchestrator, engines)

    assert time.monotonic() - began < 0.5
    assert result.status is BatchStatus.FAILED
    assert result.error == "ocr_state_invalid"
    assert engines.calls == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_batch_state_fifo_fails_without_blocking(tmp_path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    state_root = tmp_path / "ocr-state"
    state_root.mkdir()
    state_path = state_root / "batch-state.json"
    os.mkfifo(state_path, 0o600)
    result_box = []

    worker = threading.Thread(target=lambda: result_box.append(_run(orchestrator, engines)))
    worker.start()
    worker.join(timeout=0.25)
    blocked = worker.is_alive()
    if blocked:
        writer = os.open(state_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, b"{}")
        finally:
            os.close(writer)
        worker.join(timeout=1)

    assert blocked is False
    assert result_box[0].status is BatchStatus.FAILED
    assert result_box[0].error == "ocr_state_invalid"
    assert engines.calls == []
