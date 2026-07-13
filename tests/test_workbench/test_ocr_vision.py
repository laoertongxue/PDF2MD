import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import pytest

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
    shell=os.environ.get("SHELL"),
)
if mode == "timeout_ignore_term":
    signal.signal(signal.SIGTERM, lambda signum, frame: log("term"))
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
with log_path.open("a", encoding="utf-8") as handle:
    event = {"event": "child_start", "pid": os.getpid(), "pgid": os.getpgrp()}
    handle.write(json.dumps(event, sort_keys=True) + "\n")
signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))
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
with log_path.open("a", encoding="utf-8") as handle:
    event = {"event": "pipe_holder_start", "pid": os.getpid(), "pgid": os.getpgrp()}
    handle.write(json.dumps(event) + "\n")
signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))
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
data = (
    f"image:{Path(command['pdf_path']).name}:"
    f"{sha(pdf_bytes)}:{command['page']}:{command['dpi']}:{command['languages']}:{helper_label}"
).encode()
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
        assert any(event["event"] == "term" for event in _events(log))
        time.sleep(0.1)
        assert not [
            line
            for line in subprocess.check_output(["ps", "-axo", "command"], text=True).splitlines()
            if str(fake_helper) in line
        ]


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
    client, log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, post_sleep=0.2
    )
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
    client, log = _client(
        tmp_path, fake_helper, pdf, monkeypatch=monkeypatch, mode="exit_nonzero"
    )

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    lock_path = next((tmp_path / "cache" / "locks").glob("*.lock"))
    inode_after_failure = lock_path.stat().st_ino
    _configure_helper(fake_helper, log=log, mode="success")
    _recognize(client, pdf)

    assert lock_path.stat().st_ino == inode_after_failure


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
    assert Path(command["pdf_path"]).parent == tmp_path / "cache" / "source_snapshots"
    assert command["languages"] == ["en-US", "zh-Hans"]
    assert start["output_root_mode"] == 0o700
    assert "PDF2MD_VISION_OUTPUT_ROOT" in start["env_keys"]
    assert "PYTHONPATH" not in start["env_keys"]
    assert "DYLD_INSERT_LIBRARIES" not in start["env_keys"]
    assert "LC_SECRET_TOKEN" not in start["env_keys"]
    assert "OPENAI_API_KEY" not in start["env_keys"]
    assert "KEYCHAIN_PASSWORD" not in start["env_keys"]
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
    snapshot_path = Path(commands[0]["pdf_path"])
    assert commands[1]["pdf_path"] == str(snapshot_path)
    assert snapshot_path.parent == tmp_path / "cache" / "source_snapshots"
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
    assert Path(commands[1]["pdf_path"]).read_bytes() == original_bytes


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

    def counted_copy(source_fd: int, destination: Path) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return real_copy(source_fd, destination)

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
        event["child_pid"]
        for event in events
        if event["event"] == "pipe_holder_spawned"
    )
    try:
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


def test_cache_root_symlink_is_rejected_without_writing_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "cache-link"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PageCacheError):
        PageCache(root)

    assert list(outside.iterdir()) == []


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


def test_helper_replacement_after_client_init_is_rejected(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    _write_fake_helper(fake_helper, label="replaced")
    _configure_helper(fake_helper, log=log, label="replaced")

    with pytest.raises(VisionClientError):
        _recognize(client, pdf)

    assert not any(event["event"] == "command" for event in _events(log))


def test_helper_symlink_after_client_init_is_rejected(
    tmp_path, fake_helper, pdf, monkeypatch
):
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


def test_timeout_kills_helper_process_group_without_child_process_leak(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(
        tmp_path,
        fake_helper,
        pdf,
        monkeypatch=monkeypatch,
        mode="spawn_child_ignore_term",
        timeout=0.5,
    )
    current_pgid = os.getpgrp()

    with pytest.raises(VisionClientError) as error:
        _recognize(client, pdf)

    assert str(error.value) == "vision helper timed out"
    events = _events(log)
    child_pid = next(event["pid"] for event in events if event["event"] == "child_start")
    helper_start = next(event for event in events if event["event"] == "start")
    try:
        assert helper_start["pgid"] != current_pgid
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


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


def test_cache_metadata_rejects_extra_fields_and_rebuilds(
    tmp_path, fake_helper, pdf, monkeypatch
):
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


def test_cache_image_hardlink_is_rejected_and_rebuilt(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)
    first = _recognize(client, pdf)
    linked = Path(first.image_path).with_name("linked.image")
    os.link(first.image_path, linked)

    _recognize(client, pdf)

    assert sum(event["event"] == "command" for event in _events(log)) == 2
    assert list((tmp_path / "cache").rglob("*.corrupt-*"))


@pytest.mark.parametrize("failure", [PermissionError, ProcessLookupError])
def test_process_group_cleanup_falls_back_when_killpg_races(
    monkeypatch, failure
):
    from parsing_core.workbench.ocr import vision

    class StubProcess:
        def __init__(self):
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls = []

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if timeout is not None and len(self.wait_calls) < 2:
                raise subprocess.TimeoutExpired("helper", timeout)

    process = StubProcess()

    def raced_killpg(_pgid, _signal):
        raise failure()

    monkeypatch.setattr(vision.os, "killpg", raced_killpg)
    vision.VisionClient._terminate_process(process, os.getpgrp() + 1)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [0.5, 0.5]


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


def test_unrepairable_source_snapshot_failure_cleans_target_and_temporary(
    tmp_path, monkeypatch
):
    from parsing_core.workbench.ocr import page_cache

    cache = PageCache(tmp_path / "cache")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source-for-unrepairable-snapshot")
    real_copy = page_cache._copy_source_snapshot_and_hash

    def corrupt_copy(source_fd, destination):
        digest = real_copy(source_fd, destination)
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


def test_language_input_with_small_padding_is_normalized(
    tmp_path, fake_helper, pdf, monkeypatch
):
    client, log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    _recognize(client, pdf, languages=[" en-US ", " zh-Hans "])

    command = next(event for event in _events(log) if event["event"] == "command")
    assert command["command"]["languages"] == ["en-US", "zh-Hans"]


def test_thread_lock_map_releases_entry_after_lock_use(
    tmp_path, fake_helper, pdf, monkeypatch
):
    from parsing_core.workbench.ocr import page_cache

    with page_cache._THREAD_LOCKS_GUARD:
        page_cache._THREAD_LOCKS.clear()
    client, _log = _client(tmp_path, fake_helper, pdf, monkeypatch=monkeypatch)

    _recognize(client, pdf)

    with page_cache._THREAD_LOCKS_GUARD:
        assert page_cache._THREAD_LOCKS == {}


def _bundled_swift_helper_path() -> Path | None:
    machine = platform.machine()
    binary_name = {
        "arm64": "vision-ocr-aarch64-apple-darwin",
        "aarch64": "vision-ocr-aarch64-apple-darwin",
        "x86_64": "vision-ocr-x86_64-apple-darwin",
    }.get(machine)
    candidates = [
        Path("parsing-core-app/src-tauri/target/debug/vision-ocr"),
        Path("parsing-core-app/src-tauri/target/debug/bundle/macos/PDF2MD.app/Contents/MacOS/vision-ocr"),
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
    if helper is None:
        pytest.skip("bundled Swift Vision helper executable is not present")
    fixture_pdf = Path(
        "parsing-core-app/src-tauri/tests/vision-ocr-fixtures/bilingual.pdf"
    ).resolve()
    if not fixture_pdf.exists():
        pytest.skip("bundled Swift Vision helper PDF fixture is not present")

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
