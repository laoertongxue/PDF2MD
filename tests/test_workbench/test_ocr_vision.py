import json
import os
import platform
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from parsing_core.workbench.ocr.orchestrator import BatchStatus, OcrOrchestrator
from parsing_core.workbench.ocr.page_cache import CacheInputs, PageCache, PageCacheError
from parsing_core.workbench.ocr.vision import (
    RegisteredPdfSources,
    VisionClient,
    VisionClientError,
)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


FAKE_HELPER = r"""
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).with_suffix(".config.json")
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
log_path = Path(config["log_path"])
mode = config.get("mode", "success")
schema_mode = config.get("schema_mode")
post_sleep = float(config.get("post_sleep", 0))
helper_label = config.get("label", "default")
log_path.parent.mkdir(parents=True, exist_ok=True)

def log(event, **extra):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, **extra}, sort_keys=True) + "\n")

def sha(data):
    return hashlib.sha256(data).hexdigest()

log(
    "start",
    argv=sys.argv,
    env_keys=sorted(os.environ),
    output_root=os.environ.get("PDF2MD_VISION_OUTPUT_ROOT"),
    output_root_mode=stat.S_IMODE(Path(os.environ["PDF2MD_VISION_OUTPUT_ROOT"]).stat().st_mode),
    pgid=os.getpgrp(),
    pid=os.getpid(),
    sid=os.getsid(0),
    shell=os.environ.get("SHELL"),
)
if mode == "timeout_ignore_term":
    signal.signal(signal.SIGTERM, lambda signum, frame: log("term"))
    while True:
        time.sleep(1)
if mode == "close_pipes_ignore_term":
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    log("pipes_closed")
    os.close(0)
    os.close(1)
    os.close(2)
    while True:
        time.sleep(1)
if mode == "spawn_child_ignore_term":
    child_code = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

log_path = Path(sys.argv[1])
os.environ.clear()
with log_path.open("a", encoding="utf-8") as handle:
    event = {
        "event": "child_start",
        "env_keys": sorted(os.environ),
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "sid": os.getsid(0),
    }
    handle.write(json.dumps(event, sort_keys=True) + "\n")
signal.signal(signal.SIGTERM, lambda signum, frame: None)
while True:
    time.sleep(1)
'''
    child = subprocess.Popen([sys.executable, "-c", child_code, str(log_path)])
    log("child_spawned", child_pid=child.pid)
    signal.signal(signal.SIGTERM, lambda signum, frame: log("term"))
    while True:
        time.sleep(1)
if mode == "parent_exits_child_holds_pipes":
    child_code = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

log_path = Path(sys.argv[1])
os.environ.clear()
with log_path.open("a", encoding="utf-8") as handle:
    event = {
        "event": "pipe_holder_start",
        "env_keys": sorted(os.environ),
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "sid": os.getsid(0),
    }
    handle.write(json.dumps(event) + "\n")
signal.signal(signal.SIGTERM, lambda signum, frame: None)
while True:
    time.sleep(1)
'''
    child = subprocess.Popen([sys.executable, "-c", child_code, str(log_path)])
    log("pipe_holder_spawned", child_pid=child.pid)
    sys.exit(0)

line = sys.stdin.readline()
if mode == "eof":
    sys.exit(0)
if mode == "exit_nonzero":
    sys.exit(7)
if mode == "invalid_json":
    print("{not-json", flush=True)
    sys.exit(0)
if mode == "huge_stdout":
    sys.stdout.buffer.write(b"x" * (2 * 1024 * 1024))
    sys.stdout.flush()
    sys.exit(0)
if mode == "huge_stderr":
    sys.stderr.buffer.write(b"x" * (2 * 1024 * 1024))
    sys.stderr.flush()

command = json.loads(line)
log("command", command=command)
if mode == "success_child_devnull_ignore_term":
    child_code = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

log_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
parent_pid = int(sys.argv[3])
signal.signal(signal.SIGTERM, lambda signum, frame: None)
os.environ.clear()
with log_path.open("a", encoding="utf-8") as handle:
    event = {
        "event": "success_child_start",
        "env_keys": sorted(os.environ),
        "parent_pid": parent_pid,
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "sid": os.getsid(0),
    }
    handle.write(json.dumps(event, sort_keys=True) + "\n")
ready_path.write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
'''
    ready_path = log_path.with_name(f"{log_path.name}.{os.getpid()}.ready")
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(log_path), str(ready_path), str(os.getpid())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    log("success_child_spawned", child_pid=child.pid)
    ready_deadline = time.monotonic() + 2
    while not ready_path.exists():
        if time.monotonic() >= ready_deadline:
            child.kill()
            raise RuntimeError("success child did not start")
        time.sleep(0.01)
if mode == "structured_error":
    print(
        json.dumps({"error": {"code": "vision_failed", "message": "OCR secret text"}}),
        flush=True,
    )
    sys.exit(0)

root = Path(os.environ["PDF2MD_VISION_OUTPUT_ROOT"])
job_dir = root / command["output_dir"]
job_dir.mkdir(parents=True, exist_ok=True)
image = job_dir / "page.png"
pdf_bytes = Path(command["pdf_path"]).read_bytes()
fixture_payload = (
    f"image:{Path(command['pdf_path']).name}:"
    f"{sha(pdf_bytes)}:{command['page']}:{command['dpi']}:{command['languages']}:{helper_label}"
).encode()
data = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + (100).to_bytes(4, "big")
    + (200).to_bytes(4, "big")
    + b"\x08\x02\x00\x00\x00"
    + fixture_payload
)
image.write_bytes(data)
image_hash = sha(data)
response_path = str(Path(command["output_dir"]) / image.name)

if mode == "hash_mismatch":
    image_hash = "0" * 64
elif mode == "absolute_path":
    response_path = str(image)
elif mode == "escape_path":
    response_path = "../escaped.png"
    (root / "escaped.png").write_bytes(data)
elif mode == "symlink":
    image.unlink()
    outside = root / "outside.png"
    outside.write_bytes(data)
    image.symlink_to(outside)
elif mode == "hardlink":
    source = root / "hardlink-source.png"
    source.write_bytes(data)
    image.unlink()
    os.link(source, image)
elif mode == "non_regular":
    image.unlink()
    image.mkdir()

payload = {
    "page": command["page"],
    "image_path": response_path,
    "image_sha256": image_hash,
    "width": 100,
    "height": 200,
    "supported_languages": ["en-US", "zh-Hans"],
    "observations": [
        {
            "text": "OCR secret text",
            "confidence": 0.9,
            "bounding_box": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
            "candidates": [{"text": "OCR secret text", "confidence": 0.9}],
        }
    ],
}
if schema_mode == "wrong_page":
    payload["page"] = command["page"] + 1
elif schema_mode == "bad_size":
    payload["width"] = 0
elif schema_mode == "bad_confidence":
    payload["observations"][0]["confidence"] = 1.1
elif schema_mode == "bad_bbox":
    payload["observations"][0]["bounding_box"]["width"] = 1.2
elif schema_mode == "bbox_x_overflow":
    payload["observations"][0]["bounding_box"]["x"] = 0.8
    payload["observations"][0]["bounding_box"]["width"] = 0.3
elif schema_mode == "bbox_y_overflow":
    payload["observations"][0]["bounding_box"]["y"] = 0.8
    payload["observations"][0]["bounding_box"]["height"] = 0.3
elif schema_mode == "nan":
    payload["observations"][0]["confidence"] = float("nan")
elif schema_mode == "duplicate":
    payload["observations"].append(dict(payload["observations"][0]))
elif schema_mode == "unknown_top_level":
    payload["debug_path"] = command["pdf_path"] + " OCR secret text"
elif schema_mode == "unknown_observation":
    payload["observations"][0]["debug_path"] = command["pdf_path"] + " OCR secret text"
elif schema_mode == "unknown_bounding_box":
    payload["observations"][0]["bounding_box"]["debug_path"] = (
        command["pdf_path"] + " OCR secret text"
    )
elif schema_mode == "unknown_candidate":
    payload["observations"][0]["candidates"][0]["debug_path"] = (
        command["pdf_path"] + " OCR secret text"
    )
elif schema_mode == "large_text":
    payload["observations"][0]["text"] = "x" * 20000
    payload["observations"][0]["candidates"][0]["text"] = "x" * 20000
elif schema_mode == "too_many_candidates":
    payload["observations"][0]["candidates"] = [
        {"text": f"candidate-{index}", "confidence": 0.9}
        for index in range(20)
    ]
elif schema_mode == "too_many_observations":
    payload["observations"] = [
        {
            "text": f"text-{index}",
            "confidence": 0.9,
            "bounding_box": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
            "candidates": [{"text": f"text-{index}", "confidence": 0.9}],
        }
        for index in range(300)
    ]

print(json.dumps(payload, allow_nan=True), flush=True)
if mode == "extra_stdout":
    print(json.dumps(payload), flush=True)
if mode == "replace_after_response":
    image.write_bytes(b"replacement")
time.sleep(post_sleep)
"""


def _write_fake_helper(path: Path, *, label: str = "default") -> Path:
    helper = path
    helper.write_text(
        f"#!{sys.executable}\nHELPER_LABEL = {label!r}\n{FAKE_HELPER}",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    return helper


def _configure_helper(
    helper: Path,
    *,
    log: Path,
    mode: str = "success",
    schema_mode: str | None = None,
    post_sleep: float = 0,
    label: str = "default",
) -> None:
    helper.with_suffix(".config.json").write_text(
        json.dumps(
            {
                "label": label,
                "log_path": str(log),
                "mode": mode,
                "post_sleep": post_sleep,
                "schema_mode": schema_mode,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def fake_helper(tmp_path: Path) -> Path:
    helper = _write_fake_helper(tmp_path / "fake_vision_helper.py")
    _configure_helper(helper, log=tmp_path / "helper.log")
    return helper


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-registered-source")
    return path


def _client(
    tmp_path: Path,
    fake_helper: Path,
    pdf: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    mode: str = "success",
    timeout: float = 2,
    helper_version: str = "vision-test-v1",
    schema_mode: str | None = None,
    post_sleep: float = 0,
) -> tuple[VisionClient, Path]:
    log = tmp_path / "helper.log"
    _configure_helper(
        fake_helper,
        log=log,
        mode=mode,
        schema_mode=schema_mode,
        post_sleep=post_sleep,
    )
    client = VisionClient(
        helper_path=fake_helper,
        cache_root=tmp_path / "cache",
        source_validator=RegisteredPdfSources([pdf]),
        helper_version=helper_version,
        timeout=timeout,
        python_executable=sys.executable,
    )
    return client, log


def _events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _wait_for_event(log: Path, name: str, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _events(log):
            if event.get("event") == name:
                return event
        time.sleep(0.01)
    raise AssertionError(f"helper event {name!r} was not observed")


def _vision_orchestrator(tmp_path: Path, client: VisionClient, cancel: threading.Event):
    def never(*_args, **_kwargs):
        raise AssertionError("unexpected engine")

    return OcrOrchestrator(
        vision=client,
        codex=SimpleNamespace(transcribe_page=never, adjudicate_page=never),
        baidu=SimpleNamespace(recognize=never),
        state_root=tmp_path / "orchestrator-state",
        image_loader=lambda _path, **_kwargs: b"image",
        is_cancelled=cancel.is_set,
    )


def _kill_fixture_group(root: dict) -> None:
    process_group_id = root["pgid"]
    if process_group_id != root["pid"] or process_group_id == os.getpgrp():
        raise AssertionError("fixture helper did not use a dedicated process group")
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _recognize(client: VisionClient, pdf: Path, **kwargs):
    return client.recognize(
        pdf,
        page=kwargs.get("page", 1),
        dpi=kwargs.get("dpi", 144),
        languages=kwargs.get("languages", ["en-US", "zh-Hans"]),
    )


def _exception_surface(error: BaseException) -> str:
    return "\n".join(
        (
            str(error),
            repr(error),
            repr(error.__cause__),
            repr(error.__context__),
            "".join(traceback.format_exception(error)),
        )
    )


def test_helper_uses_dedicated_process_group_in_caller_session(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    _recognize(client, pdf)

    start = next(event for event in _events(log) if event["event"] == "start")
    assert start["pgid"] == start["pid"]
    assert start["pgid"] != os.getpgrp()
    assert start["sid"] == os.getsid(0)


def test_success_cleans_devnull_child_that_ignores_term(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="success_child_devnull_ignore_term",
    )

    result = _recognize(client, pdf)

    events = _events(log)
    helper_start = next(event for event in events if event["event"] == "start")
    child_start = next(event for event in events if event["event"] == "success_child_start")
    child_pid = child_start["pid"]
    try:
        assert result.page == 1
        assert child_start["env_keys"] == []
        assert child_start["parent_pid"] == helper_start["pid"]
        assert child_start["pgid"] == helper_start["pgid"]
        assert child_start["sid"] == helper_start["sid"]
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_same_page_and_config_uses_cache_without_second_helper_call(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    first = _recognize(client, pdf, languages=["zh-Hans", "en-US"])
    second = _recognize(client, pdf, languages=["en-US", "zh-Hans"])

    assert first == second
    assert sum(event["event"] == "command" for event in _events(log)) == 1
    payload = json.loads(first.observation.payload_json)
    observation = payload["observations"][0]
    assert set(observation) == {"text", "confidence", "bounding_box", "candidates"}
    assert set(observation["bounding_box"]) == {"x", "y", "width", "height"}
    assert set(observation["candidates"][0]) == {"text", "confidence"}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"page": 1}, {"page": 2}),
        ({"dpi": 144}, {"dpi": 200}),
        ({"languages": ["en-US"]}, {"languages": ["zh-Hans"]}),
    ],
)
def test_cache_key_changes_miss_for_page_dpi_or_language(
    tmp_path, fake_helper, pdf, monkeypatch, first, second
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    _recognize(client, pdf, **first)
    _recognize(client, pdf, **second)

    assert sum(event["event"] == "command" for event in _events(log)) == 2


def test_cache_key_changes_miss_for_helper_version(tmp_path, fake_helper, pdf, monkeypatch):
    first, log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, helper_version="vision-test-v1"
    )
    _recognize(first, pdf)
    second = VisionClient(
        helper_path=fake_helper,
        cache_root=tmp_path / "cache",
        source_validator=RegisteredPdfSources([pdf]),
        helper_version="vision-test-v2",
        timeout=2,
        python_executable=sys.executable,
    )
    _recognize(second, pdf)

    assert sum(event["event"] == "command" for event in _events(log)) == 2


@pytest.mark.parametrize(
    ("mode", "expected_message"),
    [
        ("exit_nonzero", "vision helper failed"),
        ("eof", "vision helper returned no response"),
        ("invalid_json", "vision helper returned invalid response"),
        ("structured_error", "vision helper reported an error"),
        ("timeout_ignore_term", "vision helper timed out"),
    ],
)
def test_helper_failures_are_recoverable_sanitized_errors(
    tmp_path, fake_helper, pdf, monkeypatch, mode, expected_message
):
    timeout = 1.0 if mode == "timeout_ignore_term" else 2.0
    client, log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, mode=mode, timeout=timeout
    )

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    rendered = _exception_surface(error.value)
    assert str(pdf) not in rendered
    assert "OCR secret text" not in rendered
    assert "book.pdf" not in rendered
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert str(error.value) == expected_message
    if mode == "timeout_ignore_term":
        events = _events(log)
        assert any(event["event"] == "term" for event in events)
        helper_pid = next(event["pid"] for event in events if event["event"] == "start")
        assert _wait_until_gone(helper_pid)


def test_public_error_has_no_sensitive_exception_chain_or_traceback(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    pdf.unlink()

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    rendered = _exception_surface(error.value)
    assert str(pdf) not in rendered
    assert pdf.name not in rendered
    assert "OCR secret text" not in rendered
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "mode",
    [
        "hash_mismatch",
        "escape_path",
        "symlink",
        "replace_after_response",
        "hardlink",
        "non_regular",
    ],
)
def test_untrusted_helper_image_path_and_file_identity_are_verified(
    tmp_path, fake_helper, pdf, monkeypatch, mode
):
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, mode=mode)

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    assert list((tmp_path / "cache").rglob("*.tmp")) == []


def test_absolute_helper_image_path_inside_job_dir_is_accepted(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, mode="absolute_path")

    result = _recognize(client, pdf)

    command = next(event for event in _events(log) if event["event"] == "command")["command"]
    assert Path(result.image_path).is_file()
    assert not Path(command["output_dir"]).is_absolute()


def test_cache_hit_verifies_metadata_and_hash_then_rebuilds_corruption(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    first = _recognize(client, pdf)
    Path(first.image_path).write_bytes(b"corrupt-cache")

    rebuilt = _recognize(client, pdf)

    assert rebuilt.image_sha256 == first.image_sha256
    assert Path(rebuilt.image_path).read_bytes() != b"corrupt-cache"
    assert sum(event["event"] == "command" for event in _events(log)) == 2
    quarantine = list((tmp_path / "cache").rglob("*.corrupt-*"))
    assert quarantine


def test_cache_publish_is_atomic_and_concurrent_same_key_calls_helper_once(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, post_sleep=0.2)
    results = []
    errors = []

    def worker():
        try:
            results.append(_recognize(client, pdf))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 4
    assert len({result.image_path for result in results}) == 1
    assert sum(event["event"] == "command" for event in _events(log)) == 1
    assert len(list((tmp_path / "cache" / "source_snapshots").glob("*.pdf"))) == 1
    assert list((tmp_path / "cache").rglob("*.tmp")) == []
    assert list((tmp_path / "cache").rglob("job-*")) == []


@pytest.mark.parametrize(
    "schema_mode",
    [
        "wrong_page",
        "bad_size",
        "bad_confidence",
        "bad_bbox",
        "nan",
        "duplicate",
        "unknown_top_level",
        "unknown_observation",
        "unknown_bounding_box",
        "unknown_candidate",
    ],
)
def test_helper_response_schema_boundaries_are_rejected(
    tmp_path, fake_helper, pdf, monkeypatch, schema_mode
):
    client, _log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, schema_mode=schema_mode
    )

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    rendered = _exception_surface(error.value)
    assert str(pdf) not in rendered
    assert "OCR secret text" not in rendered


def test_pdf_source_must_be_registered_absolute_and_not_symlink(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    other = tmp_path / "other.pdf"
    other.write_bytes(b"other")
    link = tmp_path / "book-link.pdf"
    link.symlink_to(pdf)

    with pytest.raises(VisionClientError):
        _recognize(client, Path("book.pdf"))
    with pytest.raises(VisionClientError):
        _recognize(client, other)
    with pytest.raises(VisionClientError):
        _recognize(client, link)


def test_registered_pdf_requires_canonical_suffix_and_pdf_magic(tmp_path):
    pdf_magic_txt = tmp_path / "renamed-text.txt"
    pdf_magic_txt.write_bytes(b"%PDF-looks-like-pdf")
    non_pdf = tmp_path / "not-a-pdf.pdf"
    non_pdf.write_text("plain text", encoding="utf-8")

    with pytest.raises(VisionClientError):
        RegisteredPdfSources([pdf_magic_txt])
    with pytest.raises(VisionClientError):
        RegisteredPdfSources([non_pdf])


def test_registered_pdf_rejects_inode_rebinding(tmp_path, fake_helper, pdf, monkeypatch):
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"%PDF-replacement")
    replacement.replace(pdf)

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)


def test_registration_detects_rebinding_after_nofollow_open(tmp_path, pdf, monkeypatch):
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"%PDF-replacement")
    real_open = __import__("os").open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == pdf and not replaced:
            replacement.replace(pdf)
            replaced = True
        return fd

    monkeypatch.setattr("parsing_core.workbench.ocr.vision.os.open", replacing_open)

    with pytest.raises(VisionClientError):
        RegisteredPdfSources([pdf])


def test_registered_pdf_detects_rebinding_after_nofollow_open(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"%PDF-replacement")
    real_open = __import__("os").open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == pdf and not replaced:
            replacement.replace(pdf)
            replaced = True
        return fd

    monkeypatch.setattr("parsing_core.workbench.ocr.vision.os.open", replacing_open)

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    assert not any(event["event"] == "command" for event in _events(log))


def test_cache_lock_file_is_stable_coordination_inode_after_failure(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, mode="exit_nonzero")

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    lock_path = next((tmp_path / "cache" / "locks").glob("*.lock"))
    inode_after_failure = lock_path.stat().st_ino
    _configure_helper(fake_helper, log=log, mode="success")
    _recognize(client, pdf)

    assert lock_path.stat().st_ino == inode_after_failure


def test_thread_cache_lock_polls_cancel_while_waiting(tmp_path):
    cache = PageCache(tmp_path / "cache")
    cache_key = "a" * 64
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def hold_lock():
        with cache.lock(cache_key):
            holder_entered.set()
            release_holder.wait(timeout=2)

    holder = threading.Thread(target=hold_lock, name="page-cache-lock-holder")
    holder.start()
    assert holder_entered.wait(timeout=1)
    cancel = threading.Event()
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            with cache.lock(
                cache_key,
                deadline=time.monotonic() + 1,
                cancel_event=cancel,
            ):
                raise AssertionError("cancelled waiter must not acquire the lock")
        assert time.monotonic() - started < 0.3
    finally:
        timer.cancel()
        release_holder.set()
        holder.join(timeout=1)

    assert not holder.is_alive()


def test_process_cache_lock_polls_absolute_deadline_without_corrupting_lock(tmp_path):
    import fcntl

    cache = PageCache(tmp_path / "cache")
    cache_key = "b" * 64
    lock_path = cache.locks_dir / f"{cache_key}.lock"
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            with cache.lock(cache_key, deadline=time.monotonic() + 0.12):
                raise AssertionError("deadline waiter must not acquire the lock")
        assert time.monotonic() - started < 0.35
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    assert lock_path.is_file()


def test_helper_env_and_argv_are_exact_and_shell_is_not_used(
    tmp_path, fake_helper, pdf, monkeypatch
):
    monkeypatch.setenv("PYTHONPATH", "/tmp/secret-pythonpath")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/secret-dylib")
    monkeypatch.setenv("LC_SECRET_TOKEN", "secret-locale-token")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-api-key")
    monkeypatch.setenv("KEYCHAIN_PASSWORD", "secret-keychain")
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    result = _recognize(client, pdf)

    start = next(event for event in _events(log) if event["event"] == "start")
    command = next(event for event in _events(log) if event["event"] == "command")["command"]
    assert start["argv"] == [str(fake_helper)]
    assert Path(start["output_root"]).is_absolute()
    assert not Path(start["output_root"]).is_symlink()
    assert not Path(command["output_dir"]).is_absolute()
    assert command["command"] == "render_and_recognize"
    assert command["pdf_path"] != str(pdf)
    assert command["pdf_path"].startswith("/dev/fd/")
    assert command["languages"] == ["en-US", "zh-Hans"]
    assert start["output_root_mode"] == 0o700
    assert "PDF2MD_VISION_OUTPUT_ROOT" in start["env_keys"]
    assert "PYTHONPATH" not in start["env_keys"]
    assert "DYLD_INSERT_LIBRARIES" not in start["env_keys"]
    assert "LC_SECRET_TOKEN" not in start["env_keys"]
    assert "OPENAI_API_KEY" not in start["env_keys"]
    assert "KEYCHAIN_PASSWORD" not in start["env_keys"]
    assert "PDF2MD_HELPER_CLEANUP_TOKEN" not in start["env_keys"]
    assert Path(result.image_path).exists()


def test_source_snapshot_is_reused_after_in_place_source_rewrite(
    tmp_path, fake_helper, pdf, monkeypatch
):
    original_bytes = pdf.read_bytes()
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    first = _recognize(client, pdf, page=1)
    pdf.write_bytes(b"%PDF-mutated-in-place")
    second = _recognize(client, pdf, page=2)

    commands = [event["command"] for event in _events(log) if event["event"] == "command"]
    assert len(commands) == 2
    assert all(command["pdf_path"].startswith("/dev/fd/") for command in commands)
    snapshot_path = tmp_path / "cache" / "source_snapshots" / f"{first.pdf_sha256}.pdf"
    assert snapshot_path.name == f"{first.pdf_sha256}.pdf"
    assert snapshot_path.read_bytes() == original_bytes
    assert second.pdf_sha256 == first.pdf_sha256


def test_reused_snapshot_is_rehashed_after_in_place_snapshot_tamper(
    tmp_path, fake_helper, pdf, monkeypatch
):
    original_bytes = pdf.read_bytes()
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    first = _recognize(client, pdf, page=1)
    snapshot_path = tmp_path / "cache" / "source_snapshots" / f"{first.pdf_sha256}.pdf"
    snapshot_path.chmod(0o600)
    snapshot_path.write_bytes(b"%PDF-tampered-snapshot")
    snapshot_path.chmod(0o400)

    second = _recognize(client, pdf, page=2)

    commands = [event["command"] for event in _events(log) if event["event"] == "command"]
    assert len(commands) == 2
    assert second.pdf_sha256 == first.pdf_sha256
    assert snapshot_path.read_bytes() == original_bytes
    assert commands[1]["pdf_path"].startswith("/dev/fd/")


def test_helper_reads_fd_bound_snapshot_when_path_is_replaced_after_open(
    tmp_path, fake_helper, pdf, monkeypatch
):
    original_bytes = pdf.read_bytes()
    replacement_bytes = b"%PDF-path-replacement-must-not-reach-helper"
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    digest = _sha256(original_bytes)
    snapshot_path = tmp_path / "cache" / "source_snapshots" / f"{digest}.pdf"
    displaced = snapshot_path.with_name(f".{snapshot_path.name}.displaced")
    real_run_helper = VisionClient._run_helper
    swapped = False

    def swap_path_then_run(self, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            snapshot_path.rename(displaced)
            snapshot_path.write_bytes(replacement_bytes)
            snapshot_path.chmod(0o400)
            swapped = True
        return real_run_helper(self, *args, **kwargs)

    monkeypatch.setattr(VisionClient, "_run_helper", swap_path_then_run)

    result = _recognize(client, pdf)

    image_bytes = Path(result.image_path).read_bytes()
    command = next(event["command"] for event in _events(log) if event["event"] == "command")
    assert swapped is True
    assert command["pdf_path"].startswith("/dev/fd/")
    assert _sha256(original_bytes).encode() in image_bytes
    assert _sha256(replacement_bytes).encode() not in image_bytes


def test_snapshot_inode_index_is_bounded_lru(tmp_path, fake_helper):
    sources = []
    for index in range(3):
        source = tmp_path / f"book-{index}.pdf"
        source.write_bytes(f"%PDF-book-{index}".encode())
        sources.append(source)
    log = tmp_path / "helper.log"
    _configure_helper(fake_helper, log=log)
    client = VisionClient(
        helper_path=fake_helper,
        cache_root=tmp_path / "cache",
        source_validator=RegisteredPdfSources(sources),
        helper_version="bounded-index",
        timeout=2,
        python_executable=sys.executable,
        snapshot_index_max_entries=2,
    )
    identities = [(source.stat().st_dev, source.stat().st_ino) for source in sources]

    for source in sources:
        client.recognize(source, page=1, dpi=72, languages=["en-US"])

    assert len(client._source_snapshots) == 2
    assert identities[0] not in client._source_snapshots
    assert list(client._source_snapshots) == identities[1:]


def test_recognize_repairs_corrupt_snapshot_target_in_one_call(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    digest = _sha256(pdf.read_bytes())
    snapshot_dir = tmp_path / "cache" / "source_snapshots"
    corrupt_target = snapshot_dir / f"{digest}.pdf"
    corrupt_target.write_bytes(b"corrupt-snapshot")
    corrupt_target.chmod(0o400)

    result = _recognize(client, pdf)

    assert result.pdf_sha256 == digest
    assert sum(event["event"] == "command" for event in _events(log)) == 1
    snapshots = list(snapshot_dir.glob("*.pdf"))
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == pdf.read_bytes()
    assert list(snapshot_dir.glob("*.corrupt-*"))


def test_source_snapshot_hashing_is_once_per_book_for_multi_page_recognition(
    tmp_path, fake_helper, pdf, monkeypatch
):
    from parsing_core.workbench.ocr import page_cache

    second_pdf = tmp_path / "second.pdf"
    second_pdf.write_bytes(b"%PDF-second-registered-source")
    log = tmp_path / "helper.log"
    _configure_helper(fake_helper, log=log)
    hash_calls = 0
    real_copy = page_cache._copy_source_snapshot_and_hash

    def counted_copy(source_fd: int, destination: Path, **control) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return real_copy(source_fd, destination, **control)

    monkeypatch.setattr(page_cache, "_copy_source_snapshot_and_hash", counted_copy)
    client = VisionClient(
        helper_path=fake_helper,
        cache_root=tmp_path / "cache",
        source_validator=RegisteredPdfSources([pdf, second_pdf]),
        helper_version="vision-test-v1",
        timeout=2,
        python_executable=sys.executable,
    )

    for page in (1, 2, 3):
        _recognize(client, pdf, page=page)
        _recognize(client, second_pdf, page=page)

    assert hash_calls <= 2
    assert len(list((tmp_path / "cache" / "source_snapshots").glob("*.pdf"))) == 2


def test_source_snapshot_copy_checks_cancel_between_hash_chunks(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-" + b"x" * (3 * 1024 * 1024))
    cancel = threading.Event()
    read_calls = 0
    real_read = page_cache.os.read

    def read_then_cancel(fd, size):
        nonlocal read_calls
        chunk = real_read(fd, size)
        if chunk:
            read_calls += 1
            if read_calls == 1:
                cancel.set()
        return chunk

    monkeypatch.setattr(page_cache.os, "read", read_then_cancel)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            cache.publish_source_snapshot(
                source_fd,
                deadline=time.monotonic() + 2,
                cancel_event=cancel,
            )
    finally:
        os.close(source_fd)

    assert read_calls == 1
    assert not list(cache.source_snapshots_dir.glob("*.pdf"))
    assert not list(cache.source_snapshots_dir.glob("*.tmp"))


def test_batch_pdf_snapshot_hash_checks_cancel_between_chunks(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import orchestrator as orchestrator_module

    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-" + b"y" * (3 * 1024 * 1024))
    cancel = threading.Event()
    read_calls = 0
    real_read = orchestrator_module.os.read

    def read_then_cancel(fd, size):
        nonlocal read_calls
        chunk = real_read(fd, size)
        if chunk:
            read_calls += 1
            if read_calls == 1:
                cancel.set()
        return chunk

    monkeypatch.setattr(orchestrator_module.os, "read", read_then_cancel)

    with pytest.raises(InterruptedError, match="cancelled"):
        orchestrator_module._snapshot_pdf(
            source,
            deadline=time.monotonic() + 2,
            cancel_event=cancel,
        )

    assert read_calls == 1


def test_timeout_cleans_group_when_parent_exits_but_child_holds_pipes(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="parent_exits_child_holds_pipes",
        timeout=0.5,
    )

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    assert str(error.value) == "vision helper timed out"
    events = _events(log)
    child_pid = next(
        event["child_pid"] for event in events if event["event"] == "pipe_holder_spawned"
    )
    helper_start = next(event for event in events if event["event"] == "start")
    pipe_holder = next(event for event in events if event["event"] == "pipe_holder_start")
    try:
        assert pipe_holder["env_keys"] == []
        assert pipe_holder["pgid"] == helper_start["pgid"]
        assert pipe_holder["sid"] == helper_start["sid"] == os.getsid(0)
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_pages_prefix_symlink_cannot_escape_cache(tmp_path):
    cache = PageCache(tmp_path / "cache")
    outside = tmp_path / "outside"
    outside.mkdir()
    (cache.pages_dir / "ab").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PageCacheError):
        cache.temporary_image_path("ab" + "0" * 62)

    assert not list(outside.iterdir())


def test_write_all_uses_injected_writer_with_zero_copy_memoryviews():
    from parsing_core.workbench.ocr.atomic_io import write_all

    payload = b"zero-copy-payload"
    views = []

    def short_writer(_fd, data):
        views.append(data)
        return min(3, len(data))

    write_all(123, payload, writer=short_writer)

    assert len(views) > 1
    assert all(isinstance(view, memoryview) for view in views)
    assert all(view.obj is payload for view in views)


def test_cache_metadata_atomic_write_retries_short_writes(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "a" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    expected = {"schema_version": 1, "title": "数据、模型与决策" * 8}
    real_write = os.write
    write_sizes = []

    def short_write(fd, data):
        write_sizes.append(len(data))
        return real_write(fd, data[:7])

    monkeypatch.setattr(page_cache, "_write_file", short_write, raising=False)

    cache._write_meta_atomic(entry_dir, expected)

    target = entry_dir / "meta.json"
    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert len(write_sizes) > 1
    assert target.stat().st_mode & 0o777 == 0o600


def test_cache_metadata_opens_directory_before_creating_temporary(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import atomic_io, page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "0" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    events = []
    real_open_directory = page_cache._open_directory
    real_create_temporary = atomic_io._create_temporary

    def record_open(path):
        events.append("open-directory")
        return real_open_directory(path)

    def record_create(directory_fd, prefix):
        os.fstat(directory_fd)
        events.append("create-temporary")
        return real_create_temporary(directory_fd, prefix)

    monkeypatch.setattr(page_cache, "_open_directory", record_open)
    monkeypatch.setattr(atomic_io, "_create_temporary", record_create)

    cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert events[:2] == ["open-directory", "create-temporary"]


def test_cache_metadata_atomic_write_rejects_zero_progress(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "b" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    opened_fds = []

    def stop_write(fd, _data):
        opened_fds.append(fd)
        return 0

    monkeypatch.setattr(page_cache, "_write_file", stop_write, raising=False)

    with pytest.raises(OSError, match="no progress"):
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert target.read_bytes() == b"previous"
    assert list(entry_dir.glob(".meta.*.tmp")) == []
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_cache_metadata_atomic_write_propagates_write_error(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "c" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    failure = OSError("disk full")

    opened_fds = []

    def fail_write(fd, _data):
        opened_fds.append(fd)
        raise failure

    monkeypatch.setattr(page_cache, "_write_file", fail_write, raising=False)

    with pytest.raises(OSError) as error:
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert error.value is failure
    assert target.read_bytes() == b"previous"
    assert list(entry_dir.glob(".meta.*.tmp")) == []
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("failure_stage", ["fsync", "close", "replace"])
def test_cache_metadata_atomic_write_cleans_up_before_publish_failure(
    tmp_path, monkeypatch, failure_stage
):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "d" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    failure = OSError(f"{failure_stage} failed")
    opened_fds = []
    real_close = os.close

    def record_write(fd, data):
        opened_fds.append(fd)
        return os.write(fd, data)

    monkeypatch.setattr(page_cache, "_write_file", record_write, raising=False)
    if failure_stage == "fsync":

        def fail_fsync(_fd):
            raise failure

        monkeypatch.setattr(page_cache, "_fsync_file", fail_fsync, raising=False)
    elif failure_stage == "close":

        def fail_first_close(fd):
            real_close(fd)
            if fd in opened_fds:
                raise failure

        monkeypatch.setattr(page_cache, "_close_file", fail_first_close, raising=False)
    else:

        def fail_replace(_source, _target, _directory_fd):
            raise failure

        monkeypatch.setattr(page_cache, "_replace_file", fail_replace, raising=False)

    with pytest.raises(OSError) as error:
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert error.value is failure
    assert target.read_bytes() == b"previous"
    assert list(entry_dir.glob(".meta.*.tmp")) == []
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_cache_metadata_atomic_write_closes_each_fd_once_without_closing_reused_fd(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "e" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    reused_path = tmp_path / "reused-cache-fd"
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
        if opened_fd is None:
            real_close(fd)
            return
        close_calls.append(fd)
        real_close(fd)
        if fd == opened_fd and reused_fd is None:
            reused_fd = os.open(reused_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            assert reused_fd == fd

    monkeypatch.setattr(page_cache, "_write_file", record_write)
    monkeypatch.setattr(page_cache, "_close_file", close_and_reuse)

    try:
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

        assert opened_fd is not None
        assert close_calls.count(opened_fd) == 1
        assert all(close_calls.count(fd) == 1 for fd in set(close_calls))
        assert reused_fd == opened_fd
        os.fstat(reused_fd)
    finally:
        if reused_fd is not None:
            real_close(reused_fd)


def test_cache_metadata_close_error_never_retries_reused_fd(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "7" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    sentinel_path = tmp_path / "close-error-sentinel"
    close_failure = OSError("close reported failure after releasing fd")
    real_close = os.close
    opened_fd = None
    sentinel_fd = None
    close_calls = []

    def record_write(fd, data):
        nonlocal opened_fd
        opened_fd = fd
        return os.write(fd, data)

    def close_reuse_then_raise(fd):
        nonlocal sentinel_fd
        if opened_fd is None:
            real_close(fd)
            return
        close_calls.append(fd)
        real_close(fd)
        if sentinel_fd is None:
            sentinel_fd = os.open(sentinel_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            assert sentinel_fd == fd
        raise close_failure

    monkeypatch.setattr(page_cache, "_write_file", record_write)
    monkeypatch.setattr(page_cache, "_close_file", close_reuse_then_raise)

    try:
        with pytest.raises(OSError) as error:
            cache._write_meta_atomic(entry_dir, {"schema_version": 1})

        assert error.value is close_failure
        assert opened_fd is not None
        assert sentinel_fd == opened_fd
        assert close_calls.count(opened_fd) == 1
        assert all(close_calls.count(fd) == 1 for fd in set(close_calls))
        os.fstat(sentinel_fd)
        assert target.read_bytes() == b"previous"
        assert list(entry_dir.glob(".meta.*.tmp")) == []
    finally:
        if sentinel_fd is not None:
            try:
                real_close(sentinel_fd)
            except OSError:
                pass


def test_directory_chain_close_error_preserves_reused_parent_and_closes_child_once(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import page_cache

    base = tmp_path / "base"
    (base / "child").mkdir(parents=True)
    sentinel_path = tmp_path / "directory-parent-sentinel"
    close_failure = OSError("parent close reported failure")
    real_close = os.close
    sentinel_fd = None
    returned_fd = None
    close_calls = []

    def close_parent_then_fail(fd):
        nonlocal sentinel_fd
        close_calls.append(fd)
        real_close(fd)
        if sentinel_fd is None:
            sentinel_fd = os.open(sentinel_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            assert sentinel_fd == fd
            raise close_failure

    monkeypatch.setattr(page_cache, "_close_file", close_parent_then_fail)

    try:
        with pytest.raises(OSError) as error:
            returned_fd = page_cache._open_directory_chain(base, ("child",), create=False)

        assert error.value is close_failure
        assert sentinel_fd is not None
        assert close_calls.count(sentinel_fd) == 1
        assert len(close_calls) == 2
        child_fd = close_calls[1]
        assert child_fd != sentinel_fd
        os.fstat(sentinel_fd)
        with pytest.raises(OSError):
            os.fstat(child_fd)
    finally:
        if returned_fd is not None:
            real_close(returned_fd)
        if sentinel_fd is not None:
            try:
                real_close(sentinel_fd)
            except OSError:
                pass


def test_cache_metadata_directory_open_failure_is_precommit_and_preserves_original(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "f" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    failure = OSError("directory open failed")

    def fail_directory_open(_path):
        raise failure

    monkeypatch.setattr(page_cache, "_open_directory", fail_directory_open, raising=False)

    with pytest.raises(OSError) as error:
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert error.value is failure
    assert target.read_bytes() == b"previous"
    assert list(entry_dir.glob(".meta.*.tmp")) == []


def test_cache_metadata_directory_fsync_failure_reports_committed_artifact(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "1" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    failure = OSError("directory fsync failed")

    def fail_directory_fsync(_fd):
        raise failure

    monkeypatch.setattr(page_cache, "_sync_directory", fail_directory_fsync, raising=False)

    with pytest.raises(OSError) as error:
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert type(error.value).__name__ == "AtomicCommitError"
    assert getattr(error.value, "committed", False) is True
    assert getattr(error.value, "durability_uncertain", False) is True
    assert error.value.__cause__ is failure
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert list(entry_dir.glob(".meta.*.tmp")) == []


def test_cache_metadata_cleanup_failure_does_not_mask_write_error(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import atomic_io, page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "2" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    write_failure = OSError("write failed")
    cleanup_calls = []

    def fail_write(_fd, _data):
        raise write_failure

    def fail_cleanup(name, directory_fd):
        os.fstat(directory_fd)
        cleanup_calls.append(name)
        raise OSError("cleanup failed")

    monkeypatch.setattr(page_cache, "_write_file", fail_write)
    monkeypatch.setattr(atomic_io, "_unlink_name", fail_cleanup)

    with pytest.raises(OSError) as error:
        cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert error.value is write_failure
    assert len(cleanup_calls) == 1
    assert target.read_bytes() == b"previous"


def test_cache_metadata_does_not_delete_reused_temporary_name_after_commit(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import atomic_io

    cache = PageCache(tmp_path / "cache")
    cache_key = "6" * 64
    cache.temporary_image_path(cache_key)
    entry_dir = cache.entry_dir(cache_key)
    target = entry_dir / "meta.json"
    target.write_bytes(b"previous")
    cleanup_calls = []

    def record_cleanup(name, directory_fd):
        os.fstat(directory_fd)
        cleanup_calls.append(name)

    monkeypatch.setattr(atomic_io, "_unlink_name", record_cleanup)

    cache._write_meta_atomic(entry_dir, {"schema_version": 1})

    assert cleanup_calls == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 1}


def test_cache_image_directory_open_failure_is_precommit(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "3" * 64
    image = b"new-image"
    image_sha256 = _sha256(image)
    temporary = cache.temporary_image_path(cache_key)
    temporary.write_bytes(image)
    target = cache.entry_dir(cache_key) / f"{image_sha256}.image"
    failure = OSError("directory open failed")

    def fail_directory_open(_path):
        raise failure

    monkeypatch.setattr(page_cache, "_open_directory", fail_directory_open)

    with pytest.raises(OSError) as error:
        cache.publish(
            cache_key=cache_key,
            inputs=CacheInputs("d" * 64, 1, 144, "helper", ("en-US",)),
            image_bytes_path=temporary,
            image_sha256=image_sha256,
            width=10,
            height=10,
            supported_languages=("en-US",),
            observations=(),
        )

    assert error.value is failure
    assert not target.exists()
    assert not temporary.exists()
    assert not (target.parent / "meta.json").exists()


def test_cache_image_directory_fsync_failure_reports_committed_image(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "4" * 64
    image = b"committed-image"
    image_sha256 = _sha256(image)
    temporary = cache.temporary_image_path(cache_key)
    temporary.write_bytes(image)
    target = cache.entry_dir(cache_key) / f"{image_sha256}.image"
    failure = OSError("directory fsync failed")

    def fail_directory_fsync(_fd):
        raise failure

    monkeypatch.setattr(page_cache, "_sync_directory", fail_directory_fsync)

    with pytest.raises(OSError) as error:
        cache.publish(
            cache_key=cache_key,
            inputs=CacheInputs("d" * 64, 1, 144, "helper", ("en-US",)),
            image_bytes_path=temporary,
            image_sha256=image_sha256,
            width=10,
            height=10,
            supported_languages=("en-US",),
            observations=(),
        )

    assert type(error.value).__name__ == "AtomicCommitError"
    assert getattr(error.value, "target", None) == target
    assert getattr(error.value, "durability_uncertain", False) is True
    assert error.value.__cause__ is failure
    assert target.read_bytes() == image
    assert not (target.parent / "meta.json").exists()


def test_atomic_replace_file_fsyncs_temporary_before_publish(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import atomic_io, page_cache

    directory = tmp_path / "atomic"
    directory.mkdir()
    temporary = directory / ".artifact.tmp"
    target = directory / "artifact.bin"
    temporary.write_bytes(b"not-durable-yet")
    failure = OSError("temporary fsync failed")
    replace_calls = []

    def fail_regular_file_fsync(fd):
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        raise failure

    def record_replace(source, destination, directory_fd):
        os.fstat(directory_fd)
        replace_calls.append((Path(source).name, Path(destination).name))

    monkeypatch.setattr(atomic_io.os, "fsync", fail_regular_file_fsync)

    with pytest.raises(OSError) as error:
        atomic_io.atomic_replace_file(
            temporary=temporary,
            target=target,
            close_file=os.close,
            open_directory=page_cache._open_directory,
            replace_file=record_replace,
            sync_directory=lambda _fd: None,
            unlink_temporary=page_cache._unlink_temporary,
        )

    assert error.value is failure
    assert replace_calls == []
    assert not target.exists()


def test_atomic_replace_file_fullfsync_unsupported_keeps_fsync_fallback(tmp_path, monkeypatch):
    import errno

    from parsing_core.workbench.ocr import atomic_io, page_cache

    directory = tmp_path / "atomic-fullfsync"
    directory.mkdir()
    temporary = directory / ".artifact.tmp"
    target = directory / "artifact.bin"
    temporary.write_bytes(b"durable-with-fallback")
    fsync_calls = []
    fullfsync_calls = []

    def record_fsync(fd):
        fsync_calls.append((fd, os.fstat(fd).st_ino))

    def unsupported_fullfsync(fd, operation, *_args):
        fullfsync_calls.append((fd, operation))
        raise OSError(errno.ENOTSUP, "full fsync unsupported")

    monkeypatch.setattr(atomic_io.sys, "platform", "darwin")
    monkeypatch.setattr(atomic_io.os, "fsync", record_fsync)
    monkeypatch.setattr(atomic_io.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(atomic_io.fcntl, "fcntl", unsupported_fullfsync)

    atomic_io.atomic_replace_file(
        temporary=temporary,
        target=target,
        close_file=os.close,
        open_directory=page_cache._open_directory,
        replace_file=page_cache._replace_file,
        sync_directory=lambda _fd: None,
        unlink_temporary=page_cache._unlink_temporary,
    )

    assert len(fsync_calls) == 1
    assert len(fullfsync_calls) == 1
    assert target.read_bytes() == b"durable-with-fallback"


def test_atomic_replace_file_fullfsync_io_failure_prevents_publish(tmp_path, monkeypatch):
    import errno

    from parsing_core.workbench.ocr import atomic_io, page_cache

    directory = tmp_path / "atomic-fullfsync-error"
    directory.mkdir()
    temporary = directory / ".artifact.tmp"
    target = directory / "artifact.bin"
    temporary.write_bytes(b"not-fully-durable")
    failure = OSError(errno.EIO, "full fsync failed")
    replace_calls = []

    monkeypatch.setattr(atomic_io.sys, "platform", "darwin")
    monkeypatch.setattr(atomic_io.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(atomic_io.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(
        atomic_io.fcntl,
        "fcntl",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(OSError) as error:
        atomic_io.atomic_replace_file(
            temporary=temporary,
            target=target,
            close_file=os.close,
            open_directory=page_cache._open_directory,
            replace_file=lambda *_args: replace_calls.append(True),
            sync_directory=lambda _fd: None,
            unlink_temporary=page_cache._unlink_temporary,
        )

    assert error.value is failure
    assert replace_calls == []
    assert not target.exists()


def test_atomic_replace_file_detects_last_check_after_temporary_swap(tmp_path):
    from parsing_core.workbench.ocr import atomic_io, page_cache

    directory = tmp_path / "atomic"
    directory.mkdir()
    temporary = directory / ".artifact.tmp"
    displaced = directory / ".artifact.displaced"
    target = directory / "artifact.bin"
    temporary.write_bytes(b"expected")

    def swap_then_replace(source, destination, directory_fd):
        os.rename(
            Path(source).name,
            displaced.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replacement_fd = os.open(
            Path(source).name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.write(replacement_fd, b"replacement")
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)
        os.replace(
            Path(source).name,
            Path(destination).name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )

    with pytest.raises(atomic_io.AtomicCommitError) as error:
        atomic_io.atomic_replace_file(
            temporary=temporary,
            target=target,
            close_file=os.close,
            open_directory=page_cache._open_directory,
            replace_file=swap_then_replace,
            sync_directory=os.fsync,
            unlink_temporary=page_cache._unlink_temporary,
        )

    assert error.value.committed is True
    assert error.value.identity_uncertain is True
    assert target.read_bytes() == b"replacement"
    assert displaced.read_bytes() == b"expected"


def test_atomic_replace_file_cleanup_preserves_name_swapped_after_identity_check(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import atomic_io, page_cache

    directory = tmp_path / "atomic"
    directory.mkdir()
    temporary = directory / ".artifact.tmp"
    displaced = directory / ".artifact.displaced"
    target = directory / "artifact.bin"
    temporary.write_bytes(b"owned-temporary")
    failure = OSError("publish failed")
    publish_failed = False
    swapped = False
    real_stat = atomic_io.os.stat

    def fail_publish(_source, _destination, _directory_fd):
        nonlocal publish_failed
        publish_failed = True
        raise failure

    def stat_then_swap(path, *args, **kwargs):
        nonlocal swapped
        info = real_stat(path, *args, **kwargs)
        if publish_failed and not swapped and path == temporary.name:
            directory_fd = kwargs["dir_fd"]
            os.rename(
                temporary.name,
                displaced.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            replacement_fd = os.open(
                temporary.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(replacement_fd, b"winner")
            finally:
                os.close(replacement_fd)
            swapped = True
        return info

    monkeypatch.setattr(atomic_io.os, "stat", stat_then_swap)

    with pytest.raises(OSError) as error:
        atomic_io.atomic_replace_file(
            temporary=temporary,
            target=target,
            close_file=os.close,
            open_directory=page_cache._open_directory,
            replace_file=fail_publish,
            sync_directory=os.fsync,
            unlink_temporary=page_cache._unlink_temporary,
        )

    assert error.value is failure
    assert swapped is True
    assert temporary.read_bytes() == b"winner"
    assert displaced.read_bytes() == b"owned-temporary"
    assert not target.exists()


def test_atomic_replace_file_loser_never_overwrites_or_removes_winner(tmp_path):
    from parsing_core.workbench.ocr import atomic_io, page_cache

    directory = tmp_path / "atomic"
    directory.mkdir()
    winner_temporary = directory / ".winner.tmp"
    loser_temporary = directory / ".loser.tmp"
    target = directory / "artifact.bin"
    winner_temporary.write_bytes(b"same-content")
    loser_temporary.write_bytes(b"same-content")

    common = {
        "target": target,
        "close_file": os.close,
        "open_directory": page_cache._open_directory,
        "replace_file": page_cache._publish_file_exclusive,
        "sync_directory": os.fsync,
        "unlink_temporary": page_cache._unlink_temporary,
    }
    atomic_io.atomic_replace_file(temporary=winner_temporary, **common)
    winner_identity = (target.stat().st_dev, target.stat().st_ino)

    with pytest.raises(FileExistsError):
        atomic_io.atomic_replace_file(temporary=loser_temporary, **common)

    assert (target.stat().st_dev, target.stat().st_ino) == winner_identity
    assert target.read_bytes() == b"same-content"
    assert not loser_temporary.exists()


def test_cache_cancel_after_image_replace_keeps_committed_image(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "5" * 64
    image = b"cancelled-after-commit"
    image_sha256 = _sha256(image)
    temporary = cache.temporary_image_path(cache_key)
    temporary.write_bytes(image)
    target = cache.entry_dir(cache_key) / f"{image_sha256}.image"
    cancel = threading.Event()
    replace = os.replace
    replace_calls = []

    def replace_then_cancel(source, destination, directory_fd):
        replace_calls.append(Path(destination).name)
        replace(
            Path(source).name,
            Path(destination).name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        if Path(destination).suffix == ".image":
            cancel.set()

    monkeypatch.setattr(page_cache, "_replace_file", replace_then_cancel)

    with pytest.raises(InterruptedError, match="cancelled"):
        cache.publish(
            cache_key=cache_key,
            inputs=CacheInputs("d" * 64, 1, 144, "helper", ("en-US",)),
            image_bytes_path=temporary,
            image_sha256=image_sha256,
            width=10,
            height=10,
            supported_languages=("en-US",),
            observations=(),
            cancel_event=cancel,
        )

    assert cancel.is_set()
    assert target.name in replace_calls
    assert target.read_bytes() == image
    assert not (target.parent / "meta.json").exists()
    assert not list(target.parent.glob("*.corrupt-*"))


def test_cache_root_symlink_is_rejected_without_writing_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "cache-link"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PageCacheError):
        PageCache(root)

    assert list(outside.iterdir()) == []


def test_page_cache_hardens_owned_root_to_private_mode(tmp_path):
    root = tmp_path / "cache"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    cache = PageCache(root)

    info = cache.root.lstat()
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(info.st_mode) == 0o700


def test_page_cache_rejects_non_sticky_writable_ancestor(tmp_path):
    unsafe_parent = tmp_path / "shared"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)

    with pytest.raises(PageCacheError, match="cache directory is not available"):
        PageCache(unsafe_parent / "cache")

    assert not (unsafe_parent / "cache").exists()


def test_page_cache_concurrent_first_create_reopens_and_validates(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    root = tmp_path / "cache"
    real_mkdir = page_cache.os.mkdir
    injected = False

    def concurrent_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if path == root.name and not injected:
            injected = True
            real_mkdir(path, mode, dir_fd=dir_fd)
            raise FileExistsError(path)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(page_cache.os, "mkdir", concurrent_mkdir)

    cache = PageCache(root)

    assert injected is True
    assert cache.root.is_dir()
    assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700


def test_page_cache_rejects_root_replaced_after_prepare_without_touching_replacement(
    tmp_path, monkeypatch
):
    root = tmp_path / "cache"
    displaced = tmp_path / "cache-displaced"
    real_prepare = PageCache._prepare_root
    swapped = False

    def prepare_then_swap(path):
        nonlocal swapped
        prepared = real_prepare(path)
        path.rename(displaced)
        path.mkdir(mode=0o700)
        swapped = True
        return prepared

    monkeypatch.setattr(PageCache, "_prepare_root", staticmethod(prepare_then_swap))

    with pytest.raises(PageCacheError, match="cache directory is not available"):
        PageCache(root)

    assert swapped is True
    assert list(root.iterdir()) == []
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_page_cache_rejects_runtime_root_replacement_without_touching_replacement(
    tmp_path,
):
    root = tmp_path / "cache"
    displaced = tmp_path / "cache-displaced"
    cache = PageCache(root)
    root.rename(displaced)
    root.mkdir(mode=0o700)
    marker = root / "winner"
    marker.write_bytes(b"do-not-touch")

    with pytest.raises(PageCacheError, match="cache directory is not available"):
        cache.make_job_dir("a" * 64)

    assert [(path.name, path.read_bytes()) for path in root.iterdir()] == [
        ("winner", b"do-not-touch")
    ]


def test_same_digest_source_publishers_are_serialized_across_cache_instances(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    first_cache = PageCache(cache_root)
    second_cache = PageCache(cache_root)
    first_source = tmp_path / "first.pdf"
    second_source = tmp_path / "second.pdf"
    source_bytes = b"%PDF-identical-content-different-inodes"
    first_source.write_bytes(source_bytes)
    second_source.write_bytes(source_bytes)
    assert first_source.stat().st_ino != second_source.stat().st_ino

    real_validate = PageCache.validate_source_snapshot
    guard = threading.Lock()
    first_validation_started = threading.Event()
    active = 0
    max_active = 0

    def slow_validate(self, *args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            first_validation_started.set()
        try:
            time.sleep(0.1)
            return real_validate(self, *args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(PageCache, "validate_source_snapshot", slow_validate)
    results = []
    failures = []

    def publish(cache, source):
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            results.append(cache.publish_source_snapshot(fd))
        except BaseException as exc:
            failures.append(exc)
        finally:
            os.close(fd)

    first = threading.Thread(target=publish, args=(first_cache, first_source))
    second = threading.Thread(target=publish, args=(second_cache, second_source))
    first.start()
    assert first_validation_started.wait(timeout=2)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert len(results) == 2
    assert max_active == 1
    assert results[0].identity == results[1].identity


def test_source_quarantine_swap_restores_valid_winner(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "source.pdf"
    source_bytes = b"%PDF-valid-winner"
    source.write_bytes(source_bytes)
    digest = _sha256(source_bytes)
    target = cache.source_snapshots_dir / f"{digest}.pdf"
    target.write_bytes(b"corrupt")
    target.chmod(0o400)
    valid_winner = cache.source_snapshots_dir / ".valid-winner.tmp"
    valid_winner.write_bytes(source_bytes)
    valid_winner.chmod(0o400)
    winner_identity = (valid_winner.stat().st_dev, valid_winner.stat().st_ino)
    displaced_corrupt = cache.source_snapshots_dir / ".displaced-corrupt"
    real_rename_exclusive = page_cache.rename_exclusive
    swapped = False

    def swap_winner_at_quarantine(source_path, target_path, directory_fd):
        nonlocal swapped
        if Path(source_path).name == target.name and not swapped:
            os.rename(
                Path(source_path).name,
                displaced_corrupt.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.rename(
                valid_winner.name,
                Path(source_path).name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            swapped = True
        return real_rename_exclusive(source_path, target_path, directory_fd)

    monkeypatch.setattr(page_cache, "rename_exclusive", swap_winner_at_quarantine)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        snapshot = cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    assert swapped is True
    assert snapshot.identity == winner_identity
    assert (target.stat().st_dev, target.stat().st_ino) == winner_identity
    assert target.read_bytes() == source_bytes


def test_source_snapshot_single_file_limit_fails_closed(tmp_path):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    cache = PageCache(
        tmp_path / "cache",
        limits=CacheLimits(max_source_file_bytes=16, max_source_total_bytes=64),
    )
    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-" + b"x" * 32)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
            cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    assert list(cache.source_snapshots_dir.iterdir()) == []


def test_page_image_and_metadata_single_file_limits_fail_closed(tmp_path):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    image_limits = CacheLimits(max_page_image_bytes=4, max_pages_total_bytes=4096)
    image_cache = PageCache(tmp_path / "image-cache", limits=image_limits)
    cache_key = "1" * 64
    image = b"too-large"
    temporary = image_cache.temporary_image_path(cache_key)
    temporary.write_bytes(image)
    with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
        image_cache.publish(
            cache_key=cache_key,
            inputs=CacheInputs("a" * 64, 1, 144, "helper", ("en-US",)),
            image_bytes_path=temporary,
            image_sha256=_sha256(image),
            width=10,
            height=10,
            supported_languages=("en-US",),
            observations=(),
        )

    metadata_limits = CacheLimits(
        max_page_metadata_bytes=128,
        max_pages_total_bytes=4096,
    )
    metadata_cache = PageCache(tmp_path / "metadata-cache", limits=metadata_limits)
    metadata_key = "2" * 64
    metadata_image = b"ok"
    metadata_temporary = metadata_cache.temporary_image_path(metadata_key)
    metadata_temporary.write_bytes(metadata_image)
    with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
        metadata_cache.publish(
            cache_key=metadata_key,
            inputs=CacheInputs("b" * 64, 1, 144, "helper", ("en-US",)),
            image_bytes_path=metadata_temporary,
            image_sha256=_sha256(metadata_image),
            width=10,
            height=10,
            supported_languages=("en-US",),
            observations=({"text": "x" * 256},),
        )


def test_job_single_file_and_total_reservations_are_bounded(tmp_path):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    cache = PageCache(
        tmp_path / "cache",
        limits=CacheLimits(
            max_job_file_bytes=8,
            max_job_bytes=16,
            max_jobs_total_bytes=16,
        ),
    )
    _relative, job_dir, first_job = cache.make_job_dir("a" * 64)
    try:
        (job_dir / "oversized.bin").write_bytes(b"x" * 9)
        with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
            cache.validate_job_usage(first_job)
        with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
            cache.make_job_dir("b" * 64)
    finally:
        cache.cleanup_job_dir(first_job)


def test_job_cleanup_rejects_unregistered_evidence_directory(tmp_path):
    cache = PageCache(tmp_path / "cache")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    marker = evidence / "state.json"
    marker.write_bytes(b"published evidence")

    with pytest.raises(PageCacheError, match="job cache cleanup is not available"):
        cache.cleanup_job_dir(evidence)

    assert marker.read_bytes() == b"published evidence"


def test_job_cleanup_failure_keeps_conservative_capacity_reservation(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    cache = PageCache(
        tmp_path / "cache",
        limits=CacheLimits(max_job_bytes=16, max_jobs_total_bytes=16),
    )
    _relative, output, job_root = cache.make_job_dir("a" * 64)
    (output / "full.bin").write_bytes(b"x" * 16)
    monkeypatch.setattr(cache, "_delete_candidate", lambda _candidate: False)

    with pytest.raises(PageCacheError, match="job cache cleanup is not available"):
        cache.cleanup_job_dir(job_root)
    with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
        cache.make_job_dir("b" * 64)

    assert job_root.exists()


def test_source_snapshot_total_limit_evicts_lru(tmp_path):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    limits = CacheLimits(
        max_source_file_bytes=64,
        max_source_total_bytes=45,
        max_page_cache_bytes=4096,
    )
    cache = PageCache(tmp_path / "cache", limits=limits)
    snapshots = []
    for label in (b"a", b"b"):
        source = tmp_path / f"{label.decode()}.pdf"
        source.write_bytes(b"%PDF-" + label * 15)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            snapshots.append(cache.publish_source_snapshot(fd))
        finally:
            os.close(fd)
    cache.validate_source_snapshot(snapshots[0], verify_hash=True)

    third = tmp_path / "c.pdf"
    third.write_bytes(b"%PDF-" + b"c" * 15)
    third_fd = os.open(third, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        third_snapshot = cache.publish_source_snapshot(third_fd)
    finally:
        os.close(third_fd)

    assert snapshots[0].path.exists()
    assert not snapshots[1].path.exists()
    assert third_snapshot.path.exists()


def test_page_total_limit_uses_lru_and_skips_locked_entries(tmp_path):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    def publish_page(cache, cache_key, marker):
        image = marker * 64
        temporary = cache.temporary_image_path(cache_key)
        temporary.write_bytes(image)
        inputs = CacheInputs(marker.hex() * 32, 1, 144, "helper", ("en-US",))
        cache.publish(
            cache_key=cache_key,
            inputs=inputs,
            image_bytes_path=temporary,
            image_sha256=_sha256(image),
            width=10,
            height=10,
            supported_languages=("en-US",),
            observations=(),
        )
        return inputs

    root = tmp_path / "cache"
    initial = PageCache(root)
    first_key = "1" * 64
    second_key = "2" * 64
    first_inputs = publish_page(initial, first_key, b"a")
    publish_page(initial, second_key, b"b")
    first_dir = initial.entry_dir(first_key)
    second_dir = initial.entry_dir(second_key)
    used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    entry_size = sum(path.stat().st_size for path in first_dir.iterdir() if path.is_file())
    limits = CacheLimits(
        max_pages_total_bytes=used + entry_size // 2,
        max_page_cache_bytes=used * 2,
    )
    cache = PageCache(root, limits=limits)
    os.utime(first_dir, ns=(1, 1))
    os.utime(second_dir, ns=(2, 2))
    assert cache.load_valid(first_key, first_inputs) is not None
    third_key = "3" * 64
    publish_page(cache, third_key, b"c")

    assert first_dir.exists()
    assert not second_dir.exists()

    held = threading.Event()
    release = threading.Event()

    def hold_first_lock():
        with cache.lock(first_key):
            with cache.lock(third_key):
                held.set()
                release.wait(timeout=5)

    holder = threading.Thread(target=hold_first_lock)
    holder.start()
    assert held.wait(timeout=2)
    try:
        with pytest.raises(PageCacheError, match="OCR cache capacity exceeded"):
            publish_page(cache, "4" * 64, b"d")
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive()
    assert first_dir.exists()


def test_cache_quota_scan_entry_count_is_bounded(tmp_path):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    root = tmp_path / "cache"
    cache = PageCache(root)
    for index in range(3):
        (cache.source_snapshots_dir / f"junk-{index}").write_bytes(b"x")

    with pytest.raises(PageCacheError, match="OCR cache scan limit exceeded"):
        PageCache(root, limits=CacheLimits(max_scan_entries=2))


def test_cache_parent_symlink_is_rejected_without_writing_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "parent-link"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PageCacheError):
        PageCache(parent / "cache")

    assert list(outside.iterdir()) == []


def test_helper_without_owner_execute_bit_is_rejected_at_initialization(
    tmp_path, fake_helper, pdf, monkeypatch
):
    _configure_helper(fake_helper, log=tmp_path / "helper.log")
    fake_helper.chmod(0o600)

    with pytest.raises(VisionClientError, match="vision helper is not available"):
        _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)


def test_helper_replacement_after_client_init_is_rejected(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    _write_fake_helper(fake_helper, label="replaced")
    _configure_helper(fake_helper, log=log, label="replaced")

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    assert not any(event["event"] == "command" for event in _events(log))


def test_helper_symlink_after_client_init_is_rejected(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    target = _write_fake_helper(tmp_path / "replacement_helper.py", label="symlink-target")
    _configure_helper(target, log=log, label="symlink-target")
    fake_helper.unlink()
    fake_helper.symlink_to(target)

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    assert not any(event["event"] == "command" for event in _events(log))


def test_helper_content_hash_is_part_of_cache_key_even_with_same_declared_version(
    tmp_path, fake_helper, pdf, monkeypatch
):
    first, log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, helper_version="same-version"
    )
    first_result = _recognize(first, pdf)
    second_helper = _write_fake_helper(tmp_path / "second_helper.py", label="second")
    _configure_helper(second_helper, log=log, label="second")
    second = VisionClient(
        helper_path=second_helper,
        cache_root=tmp_path / "cache",
        source_validator=RegisteredPdfSources([pdf]),
        helper_version="same-version",
        timeout=2,
        python_executable=sys.executable,
    )

    second_result = _recognize(second, pdf)

    assert second_result.image_sha256 != first_result.image_sha256
    assert sum(event["event"] == "command" for event in _events(log)) == 2


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_gone(pid: int, *, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def test_orchestrator_cancel_waits_for_vision_root_child_and_engine_thread(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="spawn_child_ignore_term",
        timeout=5,
    )
    cancel = threading.Event()
    orchestrator = _vision_orchestrator(tmp_path, client, cancel)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            orchestrator.run_batch(
                pdf, pages=[1], dpi=144, languages=["en-US"], sample_rate=0, timeout=4
            )
        ),
        name="vision-orchestrator-cancel-test",
    )
    worker.start()
    child = _wait_for_event(log, "child_start")
    root = _wait_for_event(log, "start")
    cancel.set()
    worker.join(timeout=3)
    leaked_engine_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.name == "ocr-engine-call" and thread.is_alive()
    ]
    root_alive = _pid_alive(root["pid"])
    child_alive = _pid_alive(child["pid"])
    try:
        assert not worker.is_alive()
        assert results[0].status is BatchStatus.CANCELLED
        assert not root_alive, f"Vision root PID {root['pid']} survived CANCELLED"
        assert not child_alive, f"Vision child PID {child['pid']} survived CANCELLED"
        assert leaked_engine_threads == []

        _configure_helper(fake_helper, log=log, mode="success")
        assert _recognize(client, pdf).page == 1
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)
        worker.join(timeout=1)


def test_orchestrator_deadline_waits_for_vision_root_child_and_engine_thread(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="spawn_child_ignore_term",
        timeout=5,
    )
    orchestrator = _vision_orchestrator(tmp_path, client, threading.Event())

    result = orchestrator.run_batch(
        pdf, pages=[1], dpi=144, languages=["en-US"], sample_rate=0, timeout=0.5
    )
    child = _wait_for_event(log, "child_spawned")
    root = _wait_for_event(log, "start")
    leaked_engine_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.name == "ocr-engine-call" and thread.is_alive()
    ]
    root_alive = _pid_alive(root["pid"])
    child_alive = _pid_alive(child["child_pid"])
    try:
        assert result.status is BatchStatus.FAILED
        assert result.error == "ocr_timeout"
        assert result.pages[1].error == "ocr_timeout"
        assert not root_alive, f"Vision root PID {root['pid']} survived timeout"
        assert not child_alive, f"Vision child PID {child['child_pid']} survived timeout"
        assert leaked_engine_threads == []
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)


def test_vision_cancel_is_polled_after_helper_closes_all_pipes(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="close_pipes_ignore_term",
        timeout=5,
    )
    cancel = threading.Event()
    outcomes = []

    def recognize():
        try:
            client.recognize(
                pdf,
                page=1,
                dpi=144,
                languages=["en-US"],
                deadline=time.monotonic() + 4,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=recognize, name="vision-closed-pipes-cancel-test")
    worker.start()
    _wait_for_event(log, "pipes_closed")
    root = _wait_for_event(log, "start")
    cancel.set()
    worker.join(timeout=1)
    worker_alive = worker.is_alive()
    root_alive = _pid_alive(root["pid"])
    try:
        assert not worker_alive
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], VisionClientError)
        assert "cancelled" in str(outcomes[0])
        assert not root_alive
    finally:
        if root_alive:
            _kill_fixture_group(root)
        worker.join(timeout=1)


def test_timeout_kills_dedicated_group_after_child_clears_env_and_ignores_term(
    tmp_path, fake_helper, pdf, monkeypatch
):
    from parsing_core.workbench.ocr import vision

    process_scan_calls = []

    def unavailable_process_scan(*args, **kwargs):
        process_scan_calls.append((args, kwargs))
        raise FileNotFoundError("/bin/ps")

    monkeypatch.setattr(vision.subprocess, "run", unavailable_process_scan)
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="spawn_child_ignore_term",
        timeout=1.0,
    )
    current_pgid = os.getpgrp()
    current_sid = os.getsid(0)

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    assert str(error.value) == "vision helper timed out"
    events = _events(log)
    child_pid = next(event["pid"] for event in events if event["event"] == "child_start")
    child_start = next(event for event in events if event["event"] == "child_start")
    helper_start = next(event for event in events if event["event"] == "start")
    try:
        assert process_scan_calls == []
        assert helper_start["pgid"] == helper_start["pid"]
        assert helper_start["pgid"] != current_pgid
        assert helper_start["sid"] == current_sid
        assert child_start["env_keys"] == []
        assert child_start["pgid"] == helper_start["pgid"]
        assert child_start["sid"] == current_sid
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_timeout_of_one_helper_does_not_kill_concurrent_helper(tmp_path, pdf, monkeypatch):
    slow_dir = tmp_path / "slow"
    timeout_dir = tmp_path / "timeout"
    slow_dir.mkdir()
    timeout_dir.mkdir()
    slow_helper = _write_fake_helper(slow_dir / "vision_helper.py", label="slow")
    timeout_helper = _write_fake_helper(timeout_dir / "vision_helper.py", label="timeout")
    slow_log = slow_dir / "helper.log"
    timeout_log = timeout_dir / "helper.log"
    _configure_helper(slow_helper, log=slow_log, post_sleep=3.0, label="slow")
    _configure_helper(
        timeout_helper,
        log=timeout_log,
        mode="spawn_child_ignore_term",
        label="timeout",
    )
    slow_client = VisionClient(
        helper_path=slow_helper,
        cache_root=slow_dir / "cache",
        source_validator=RegisteredPdfSources([pdf]),
        helper_version="slow-v1",
        timeout=5,
        python_executable=sys.executable,
    )
    timeout_client = VisionClient(
        helper_path=timeout_helper,
        cache_root=timeout_dir / "cache",
        source_validator=RegisteredPdfSources([pdf]),
        helper_version="timeout-v1",
        timeout=1.0,
        python_executable=sys.executable,
    )
    slow_results = []
    slow_errors = []

    def run_slow_helper():
        try:
            slow_results.append(_recognize(slow_client, pdf))
        except BaseException as error:
            slow_errors.append(error)

    slow_thread = threading.Thread(target=run_slow_helper)
    slow_thread.start()
    deadline = time.time() + 2
    while time.time() < deadline and not any(
        event["event"] == "start" for event in _events(slow_log)
    ):
        time.sleep(0.02)
    slow_start = next(event for event in _events(slow_log) if event["event"] == "start")

    with pytest.raises(VisionClientError, match="vision helper timed out"):
        _recognize(timeout_client, pdf)

    timeout_events = _events(timeout_log)
    timeout_start = next(event for event in timeout_events if event["event"] == "start")
    timeout_child_pid = next(
        event["pid"] for event in timeout_events if event["event"] == "child_start"
    )
    try:
        assert timeout_start["pgid"] != slow_start["pgid"]
        assert _pid_alive(slow_start["pid"])
        slow_thread.join(timeout=4)
        assert not slow_thread.is_alive()
        assert slow_errors == []
        assert len(slow_results) == 1
        assert _wait_until_gone(timeout_child_pid)
    finally:
        if slow_thread.is_alive():
            slow_thread.join(timeout=4)
        if _pid_alive(timeout_child_pid):
            os.kill(timeout_child_pid, signal.SIGKILL)


def test_process_group_verification_failure_only_terminates_known_root(
    tmp_path, fake_helper, pdf, monkeypatch
):
    from parsing_core.workbench.ocr import vision

    spawned = []
    killpg_calls = []
    real_popen = vision.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def fail_group_verification(_pid):
        raise OSError("process group unavailable")

    def record_killpg(group_id, signal_number):
        killpg_calls.append((group_id, signal_number))

    monkeypatch.setattr(vision.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(vision.os, "getpgid", fail_group_verification)
    monkeypatch.setattr(vision.os, "killpg", record_killpg)
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    with pytest.raises(VisionClientError, match="vision helper is not available"):
        _recognize(client, pdf)

    assert len(spawned) == 1
    assert killpg_calls == []
    assert _wait_until_gone(spawned[0].pid)


def test_session_verification_failure_cleans_getpgid_confirmed_group(monkeypatch):
    from parsing_core.workbench.ocr import vision

    class StubProcess:
        pid = 12_345
        returncode = None
        stdin = None
        stdout = None
        stderr = None

    process = StubProcess()
    group_cleanup_calls = []
    root_cleanup_calls = []

    monkeypatch.setattr(vision.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(vision.os, "getpgrp", lambda: 111)
    monkeypatch.setattr(
        vision.os,
        "getsid",
        lambda pid: 222 if pid == 0 else (_ for _ in ()).throw(OSError("sid unavailable")),
    )
    monkeypatch.setattr(vision.os, "getpgid", lambda _pid: process.pid)
    monkeypatch.setattr(
        vision,
        "_terminate_helper_process",
        lambda candidate, group_id: group_cleanup_calls.append((candidate, group_id)),
    )
    monkeypatch.setattr(
        vision,
        "_terminate_known_process",
        lambda candidate: root_cleanup_calls.append(candidate),
    )

    with pytest.raises(OSError, match="sid unavailable"):
        vision._spawn_helper_process(["helper"])

    assert group_cleanup_calls == [(process, process.pid)]
    assert root_cleanup_calls == []


@pytest.mark.parametrize(
    ("mode", "expected_message"),
    [
        ("huge_stdout", "vision helper output exceeded limit"),
        ("huge_stderr", "vision helper output exceeded limit"),
        ("extra_stdout", "vision helper returned invalid response"),
    ],
)
def test_helper_stdout_stderr_and_single_response_are_bounded(
    tmp_path, fake_helper, pdf, monkeypatch, mode, expected_message
):
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, mode=mode)

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    assert str(error.value) == expected_message


@pytest.mark.parametrize(
    "schema_mode",
    [
        "large_text",
        "too_many_candidates",
        "too_many_observations",
        "bbox_x_overflow",
        "bbox_y_overflow",
    ],
)
def test_helper_response_size_boundaries_are_rejected(
    tmp_path, fake_helper, pdf, monkeypatch, schema_mode
):
    client, _log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, schema_mode=schema_mode
    )

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)


def test_cache_metadata_rejects_extra_fields_and_rebuilds(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    _recognize(client, pdf)
    meta_path = next((tmp_path / "cache" / "pages").rglob("meta.json"))
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["extra_debug"] = "should not be accepted"
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    _recognize(client, pdf)

    assert sum(event["event"] == "command" for event in _events(log)) == 2
    assert list((tmp_path / "cache").rglob("*.corrupt-*"))


def test_cache_metadata_binds_image_name_to_expected_hash_filename(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    first = _recognize(client, pdf)
    image_path = Path(first.image_path)
    renamed = image_path.with_name("renamed.image")
    image_path.rename(renamed)
    meta_path = renamed.with_name("meta.json")
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["image_name"] = renamed.name
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    _recognize(client, pdf)

    assert sum(event["event"] == "command" for event in _events(log)) == 2
    assert list((tmp_path / "cache").rglob("*.corrupt-*"))


def test_cache_image_hardlink_is_rejected_and_rebuilt(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    first = _recognize(client, pdf)
    linked = Path(first.image_path).with_name("linked.image")
    os.link(first.image_path, linked)

    _recognize(client, pdf)

    assert sum(event["event"] == "command" for event in _events(log)) == 2
    assert list((tmp_path / "cache").rglob("*.corrupt-*"))


@pytest.mark.parametrize(
    ("failure", "expected_signals", "cleanup_fails"),
    [
        (PermissionError, [signal.SIGTERM, signal.SIGKILL], True),
        (ProcessLookupError, [], False),
    ],
)
def test_verified_process_group_cleanup_never_falls_back_to_root_pid(
    monkeypatch, failure, expected_signals, cleanup_fails
):
    from parsing_core.workbench.ocr import vision

    class StubProcess:
        def __init__(self):
            self.pid = os.getpgrp() + 10_000
            self.returncode = 0
            self.terminate_calls = 0
            self.kill_calls = 0

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

    process = StubProcess()
    killpg_calls = []

    def raced_killpg(group_id, signal_number):
        killpg_calls.append((group_id, signal_number))
        raise failure()

    monkeypatch.setattr(vision.os, "killpg", raced_killpg)
    monkeypatch.setattr(vision.time, "sleep", lambda _seconds: None)
    if cleanup_fails:
        with pytest.raises(RuntimeError, match="helper process cleanup failed"):
            vision.VisionClient._terminate_process(process, process.pid)
    else:
        vision.VisionClient._terminate_process(process, process.pid)

    assert [signal_number for _group_id, signal_number in killpg_calls if signal_number] == (
        expected_signals
    )
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_verified_process_group_cleanup_skips_kill_when_term_empties_group(monkeypatch):
    from parsing_core.workbench.ocr import vision

    class StubProcess:
        pid = os.getpgrp() + 10_000
        returncode = 0

        def terminate(self):
            raise AssertionError("verified group must not fall back to root terminate")

        def kill(self):
            raise AssertionError("verified group must not fall back to root kill")

    group_exists = True
    signals = []

    def killpg(_group_id, signal_number):
        nonlocal group_exists
        if signal_number == 0:
            if not group_exists:
                raise ProcessLookupError
            return
        signals.append(signal_number)
        if signal_number == signal.SIGTERM:
            group_exists = False

    monkeypatch.setattr(vision.os, "killpg", killpg)
    monkeypatch.setattr(
        vision.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("empty group must not sleep")),
    )

    vision.VisionClient._terminate_process(StubProcess(), StubProcess.pid)

    assert signals == [signal.SIGTERM]


def test_helper_cleanup_raises_when_process_group_survives_kill(monkeypatch):
    from parsing_core.workbench.ocr import vision

    class StubProcess:
        pid = os.getpgrp() + 20_000
        returncode = 0
        stdin = None
        stdout = None
        stderr = None

    signals = []
    wait_calls = []

    monkeypatch.setattr(vision, "_helper_process_group_exists", lambda _group_id: True)
    monkeypatch.setattr(
        vision,
        "_wait_for_helper_process_group_exit",
        lambda _process, _group_id: wait_calls.append(1) or False,
    )
    monkeypatch.setattr(
        vision.os,
        "killpg",
        lambda group_id, signal_number: signals.append((group_id, signal_number)),
    )
    monkeypatch.setattr(vision, "_reap_helper_root", lambda _process: None)

    with pytest.raises(RuntimeError, match="helper process cleanup failed"):
        vision._terminate_helper_process(StubProcess(), StubProcess.pid)

    assert [signal_number for _group_id, signal_number in signals] == [
        signal.SIGTERM,
        signal.SIGKILL,
    ]
    assert wait_calls == [1, 1]


def test_cache_publish_replaces_bad_existing_target_with_correct_bytes(tmp_path):
    cache = PageCache(tmp_path / "cache")
    cache_key = "a" * 64
    inputs = CacheInputs("b" * 64, 1, 144, "helper", ("en-US",))
    correct = b"correct-image"
    image_sha256 = _sha256(correct)
    entry_dir = cache.entry_dir(cache_key)
    entry_dir.mkdir(parents=True)
    target = entry_dir / f"{image_sha256}.image"
    target.write_bytes(b"bad-image")
    temporary = cache.temporary_image_path(cache_key)
    temporary.write_bytes(correct)

    result = cache.publish(
        cache_key=cache_key,
        inputs=inputs,
        image_bytes_path=temporary,
        image_sha256=image_sha256,
        width=10,
        height=10,
        supported_languages=("en-US",),
        observations=(),
    )

    assert Path(result.image_path).read_bytes() == correct
    assert target.stat().st_nlink == 1


def test_helper_image_copy_polls_cancel_and_cleans_temporary(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "output"
    job_dir.mkdir(parents=True)
    source = job_dir / "page.png"
    source.write_bytes(b"x" * (3 * 1024 * 1024))
    destination = tmp_path / "cache" / ".page.tmp"
    destination.parent.mkdir()
    cancel = threading.Event()
    real_read = page_cache.os.read
    reads = 0

    def cancel_after_first_read(fd, size):
        nonlocal reads
        chunk = real_read(fd, size)
        reads += 1
        if reads == 1:
            cancel.set()
        return chunk

    monkeypatch.setattr(page_cache.os, "read", cancel_after_first_read)

    with pytest.raises(InterruptedError, match="cancelled"):
        page_cache.copy_verified_helper_image(
            jobs_root=jobs_root,
            job_dir=job_dir,
            relative_image_path="output/page.png",
            expected_sha256=_sha256(source.read_bytes()),
            destination=destination,
            deadline=time.monotonic() + 5,
            cancel_event=cancel,
        )

    assert reads == 1
    assert not destination.exists()


def test_cancelled_cache_hash_preserves_existing_publication(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    cache_key = "c" * 64
    inputs = CacheInputs("d" * 64, 1, 144, "helper", ("en-US",))
    image = b"published-image"
    image_sha256 = _sha256(image)
    temporary = cache.temporary_image_path(cache_key)
    temporary.write_bytes(image)
    published = cache.publish(
        cache_key=cache_key,
        inputs=inputs,
        image_bytes_path=temporary,
        image_sha256=image_sha256,
        width=10,
        height=10,
        supported_languages=("en-US",),
        observations=(),
    )
    cancel = threading.Event()
    real_read = page_cache.os.read

    def cancel_on_read(fd, size):
        chunk = real_read(fd, size)
        cancel.set()
        return chunk

    monkeypatch.setattr(page_cache.os, "read", cancel_on_read)

    with pytest.raises(InterruptedError, match="cancelled"):
        cache.load_valid(
            cache_key,
            inputs,
            deadline=time.monotonic() + 5,
            cancel_event=cancel,
        )

    assert Path(published.image_path).read_bytes() == image
    assert (Path(published.image_path).parent / "meta.json").is_file()
    assert not list(cache.pages_dir.rglob("*.corrupt-*"))


def test_source_snapshot_corrupt_existing_target_is_removed_and_rebuilt(tmp_path):
    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source-for-snapshot")
    digest = _sha256(source.read_bytes())
    target = cache.source_snapshots_dir / f"{digest}.pdf"
    target.write_bytes(b"partial")
    target.chmod(0o400)

    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        first = cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    assert first.path.read_bytes() == source.read_bytes()
    assert list(cache.source_snapshots_dir.glob("*.corrupt-*"))

    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        snapshot = cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    assert snapshot.path.read_bytes() == source.read_bytes()
    assert snapshot.path.stat().st_nlink == 1


def test_source_snapshot_directory_fsync_failure_reports_committed_target(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "source.pdf"
    source_bytes = b"%PDF-source-committed-before-directory-fsync"
    source.write_bytes(source_bytes)
    digest = _sha256(source_bytes)
    real_sync = page_cache._fsync_directory
    sync_calls = 0
    failure = OSError("snapshot directory fsync failed")

    def fail_after_link(path):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise failure
        real_sync(path)

    monkeypatch.setattr(page_cache, "_fsync_directory", fail_after_link)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(OSError) as error:
            cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    target = cache.source_snapshots_dir / f"{digest}.pdf"
    assert type(error.value).__name__ == "AtomicCommitError"
    assert getattr(error.value, "committed", False) is True
    assert getattr(error.value, "durability_uncertain", False) is True
    assert error.value.__cause__ is failure
    assert target.read_bytes() == source_bytes


def test_source_snapshot_cleanup_failure_does_not_mask_precommit_error(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source-precommit-failure")
    link_failure = OSError("snapshot link failed")
    cleanup_failure = OSError("snapshot cleanup failed")
    real_unlink = Path.unlink

    def fail_link(_source, _target):
        raise link_failure

    def fail_temporary_cleanup(path, *args, **kwargs):
        if path.name.startswith(".snapshot-"):
            raise cleanup_failure
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(page_cache.os, "link", fail_link)
    monkeypatch.setattr(page_cache.Path, "unlink", fail_temporary_cleanup)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(OSError) as error:
            cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    assert error.value is link_failure
    assert getattr(error.value, "committed", False) is False
    assert not list(cache.source_snapshots_dir.glob("*.pdf"))


def test_unrepairable_source_snapshot_failure_cleans_target_and_temporary(tmp_path, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source-for-unrepairable-snapshot")
    real_copy = page_cache._copy_source_snapshot_and_hash

    def corrupt_copy(source_fd, destination, **control):
        digest = real_copy(source_fd, destination, **control)
        destination.write_bytes(b"corrupt-after-copy")
        return digest

    monkeypatch.setattr(page_cache, "_copy_source_snapshot_and_hash", corrupt_copy)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(PageCacheError):
            cache.publish_source_snapshot(source_fd)
    finally:
        os.close(source_fd)

    assert not list(cache.source_snapshots_dir.glob("*.pdf"))
    assert not list(cache.source_snapshots_dir.glob("*.tmp"))
    assert list(cache.source_snapshots_dir.glob("*.corrupt-*"))


def test_page_cache_accepts_macos_var_system_alias_without_following_attacker_links(
    tmp_path,
):
    if platform.system() != "Darwin" or not Path("/var").is_symlink():
        pytest.skip("macOS /var system alias is not present")
    private_tmp = Path(tempfile.mkdtemp(prefix="pdf2md-cache-"))
    if private_tmp.parts[:2] == ("/", "var"):
        root = private_tmp
    elif private_tmp.parts[:3] == ("/", "private", "var"):
        root = Path("/var", *private_tmp.parts[3:])
    else:
        shutil.rmtree(private_tmp, ignore_errors=True)
        pytest.skip("temporary directory is not under macOS private var")
    try:
        cache = PageCache(root)
        assert cache.pages_dir.is_dir()
    finally:
        shutil.rmtree(private_tmp, ignore_errors=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": True},
        {"page": -1},
        {"page": 10001},
        {"dpi": False},
        {"dpi": -1},
        {"dpi": 601},
    ],
)
def test_page_and_dpi_reject_bool_negative_and_excessive_values(
    tmp_path, fake_helper, pdf, monkeypatch, kwargs
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    with pytest.raises(VisionClientError):
        _recognize(client, pdf, **kwargs)

    assert not any(event["event"] == "command" for event in _events(log))


@pytest.mark.parametrize(
    "languages",
    [
        [f"lang-{index}" for index in range(129)],
        ["x" * 74],
    ],
)
def test_input_language_configuration_rejects_excessive_count_and_length(
    tmp_path, fake_helper, pdf, monkeypatch, languages
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf, languages=languages)

    assert str(error.value) == "vision OCR could not complete"
    assert not any(event["event"] == "command" for event in _events(log))


def test_language_raw_length_is_checked_before_strip(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    padded_language = " en-US" + (" " * 995)

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf, languages=[padded_language])

    assert str(error.value) == "vision OCR could not complete"
    assert not any(event["event"] == "command" for event in _events(log))


def test_language_input_with_small_padding_is_normalized(tmp_path, fake_helper, pdf, monkeypatch):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    _recognize(client, pdf, languages=[" en-US ", " zh-Hans "])

    command = next(event for event in _events(log) if event["event"] == "command")
    assert command["command"]["languages"] == ["en-US", "zh-Hans"]


def test_thread_lock_map_releases_entry_after_lock_use(tmp_path, fake_helper, pdf, monkeypatch):
    from parsing_core.workbench.ocr import page_cache

    with page_cache._THREAD_LOCKS_GUARD:
        page_cache._THREAD_LOCKS.clear()
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    _recognize(client, pdf)

    with page_cache._THREAD_LOCKS_GUARD:
        assert page_cache._THREAD_LOCKS == {}


def test_vision_cache_result_path_is_accepted_by_codex_pages_trust_root(
    tmp_path, fake_helper, pdf, monkeypatch
):
    from parsing_core.workbench.ocr.codex_vision import _copy_verified_image

    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    result = _recognize(client, pdf)
    destination = tmp_path / "codex-fixture" / "page.png"
    destination.parent.mkdir()

    _copy_verified_image(
        result.image_path,
        destination,
        trusted_root=client.cache.pages_dir,
        expected_sha256=result.image_sha256,
        expected_width=result.width,
        expected_height=result.height,
    )

    assert destination.read_bytes() == Path(result.image_path).read_bytes()


def _bundled_swift_helper_path() -> Path | None:
    machine = platform.machine()
    binary_name = {
        "arm64": "vision-ocr-aarch64-apple-darwin",
        "aarch64": "vision-ocr-aarch64-apple-darwin",
        "x86_64": "vision-ocr-x86_64-apple-darwin",
    }.get(machine)
    candidates = [
        Path("parsing-core-app/src-tauri/target/debug/vision-ocr"),
        Path(
            "parsing-core-app/src-tauri/target/debug/bundle/macos/PDF2MD.app/Contents/MacOS/vision-ocr"
        ),
    ]
    if binary_name is not None:
        candidates.insert(0, Path("parsing-core-app/src-tauri/binaries") / binary_name)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists() and os.access(resolved, os.X_OK):
            return resolved
    return None


def test_macos_bundled_swift_helper_absolute_image_path_protocol(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("macOS bundled Swift Vision helper integration test")
    helper = _bundled_swift_helper_path()
    assert helper is not None, "bundled Swift Vision helper executable is required on macOS"
    fixture_pdf = Path(
        "parsing-core-app/src-tauri/tests/vision-ocr-fixtures/bilingual.pdf"
    ).resolve()
    assert fixture_pdf.exists(), "bundled Swift Vision helper PDF fixture is required on macOS"

    client = VisionClient(
        helper_path=helper,
        cache_root=tmp_path / "cache",
        source_validator=RegisteredPdfSources([fixture_pdf]),
        helper_version="swift-real-helper-test",
        timeout=30,
    )

    result = client.recognize(fixture_pdf, page=1, dpi=72, languages=["en-US"])

    assert result.page == 1
    assert Path(result.image_path).is_file()
