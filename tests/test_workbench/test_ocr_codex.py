import hashlib
import json
import os
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

import pytest

from parsing_core.workbench.ocr.codex_vision import (
    CodexVisionError,
    CodexVisionExecutor,
    validate_codex_exec_argv,
)

FAKE_CODEX = r"""
import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).with_suffix(".config.json")
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
log_path = Path(config["log_path"])
mode = config.get("mode", "success")
version = config.get("version", "codex-cli 9.9.9")
log_path.parent.mkdir(parents=True, exist_ok=True)

def log(event, **extra):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, **extra}, sort_keys=True) + "\n")

if sys.argv[1:] == ["--version"]:
    if mode == "version_huge_stdout":
        sys.stdout.write("x" * (2 * 1024 * 1024))
        sys.stdout.flush()
        raise SystemExit(0)
    if mode == "version_spawn_child_timeout":
        child_code = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path
log_path = Path(sys.argv[1])
with log_path.open("a", encoding="utf-8") as handle:
    event = {"event": "version_child_start", "pid": os.getpid(), "pgid": os.getpgrp()}
    handle.write(json.dumps(event) + "\n")
signal.signal(signal.SIGTERM, lambda signum, frame: None)
while True:
    time.sleep(1)
'''
        child = subprocess.Popen([sys.executable, "-c", child_code, str(log_path)])
        log("version_child_spawned", child_pid=child.pid)
        signal.signal(signal.SIGTERM, lambda signum, frame: None)
        while True:
            time.sleep(1)
    print(version)
    raise SystemExit(0)

log(
    "start",
    argv=sys.argv,
    env_keys=sorted(os.environ),
    cwd=os.getcwd(),
    pgid=os.getpgrp(),
    pid=os.getpid(),
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
    handle.write(json.dumps(event) + "\n")
signal.signal(signal.SIGTERM, lambda signum, frame: None)
while True:
    time.sleep(1)
'''
    child = subprocess.Popen([sys.executable, "-c", child_code, str(log_path)])
    log("child_spawned", child_pid=child.pid)
    signal.signal(signal.SIGTERM, lambda signum, frame: log("term"))
    while True:
        time.sleep(1)

if mode == "exit_nonzero":
    print(
        "secret stderr /Users/laoer/Documents/PDF2MD/book.pdf 教材 OPENAI_API_KEY",
        file=sys.stderr,
    )
    raise SystemExit(42)
if mode == "huge_stdout":
    sys.stdout.write("x" * (2 * 1024 * 1024))
    sys.stdout.flush()
    raise SystemExit(0)
if mode == "huge_stderr":
    sys.stderr.write("x" * (2 * 1024 * 1024))
    sys.stderr.flush()

if mode == "prefill_then_read":
    sys.stdout.write("x" * (2 * 1024 * 1024))
    sys.stdout.flush()

if mode == "prefill_512_then_read":
    sys.stdout.write("x" * (512 * 1024))
    sys.stdout.flush()

prompt = sys.stdin.read()
log(
    "prompt",
    prompt=prompt,
    image_exists=Path("page.png").is_file(),
    schema_exists=Path(sys.argv[sys.argv.index("--output-schema") + 1]).is_file(),
    image_size=Path("page.png").stat().st_size,
    image_sha256=hashlib.sha256(Path("page.png").read_bytes()).hexdigest(),
)

if mode == "prefill_512_then_read":
    raise SystemExit(0)

output_name = sys.argv[sys.argv.index("--output-last-message") + 1]
schema_name = sys.argv[sys.argv.index("--output-schema") + 1]
counter = CONFIG_PATH.with_suffix(".attempt.count")
attempt = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
counter.write_text(str(attempt), encoding="utf-8")

if mode == "invalid_once" and attempt == 1:
    Path(output_name).write_text('{"page": 1, "blocks": []}', encoding="utf-8")
    raise SystemExit(0)
if mode == "always_invalid":
    Path(output_name).write_text('{"page": 1, "blocks": []}', encoding="utf-8")
    raise SystemExit(0)
if mode == "huge_result":
    Path(output_name).write_text(json.dumps({"blob": "x" * (2 * 1024 * 1024)}), encoding="utf-8")
    raise SystemExit(0)

if mode == "result_fifo":
    Path(output_name).unlink(missing_ok=True)
    os.mkfifo(output_name)
    raise SystemExit(0)

if mode == "result_growth":
    Path(output_name).write_text("{" + "x" * (2 * 1024 * 1024), encoding="utf-8")
    raise SystemExit(0)

if "page-adjudication" in schema_name:
    payload = {
        "page": {"number": 1, "width": 1200, "height": 1600},
        "final_blocks": [
            {
                "id": "b1",
                "type": "paragraph",
                "text": "visible final text",
                "region": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "candidates": [],
                "uncertainty_reason": "",
                "reading_order": 1,
                "table": None,
                "formula": None,
                "source_region": "r1",
                "confidence": 0.91,
            }
        ],
        "resolved_conflicts": [
            {
                "id": "c1",
                "region": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "evidence": ["image-visible", "apple-character-shape"],
                "decision": "visible final text",
                "confidence": 0.91,
            }
        ],
        "tables": [],
        "formulas": [],
        "decision_evidence": ["region evidence beats vote count"],
        "confidence": 0.91,
        "status": "accepted",
    }
else:
    payload = {
        "page": {"number": 1, "width": 1200, "height": 1600},
        "blocks": [
            {
                "id": "b1",
                "type": "paragraph",
                "text": "visible text only",
                "region": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "candidates": [{"text": "visible text only", "confidence": 0.9}],
                "uncertainty_reason": "",
                "reading_order": 1,
                "table": None,
                "formula": None,
                "source_region": "r1",
                "confidence": 0.9,
            }
        ],
        "uncertain_items": [],
        "reading_order": ["b1"],
    }
Path(output_name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
if mode == "result_symlink":
    outside = Path(output_name).with_name("outside-result.json")
    outside.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    Path(output_name).unlink()
    Path(output_name).symlink_to(outside)
"""


def _png(width: int, height: int, suffix: bytes = b"") -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + suffix
    )


def _write_fake_codex(path: Path, *, mode: str = "success", version: str = "codex-cli 9.9.9"):
    path.write_text(f"#!{sys.executable}\n{FAKE_CODEX}", encoding="utf-8")
    path.chmod(0o700)
    path.with_suffix(".config.json").write_text(
        json.dumps(
            {"log_path": str(path.with_suffix(".log")), "mode": mode, "version": version},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    return _write_fake_codex(tmp_path / "fake_codex.py")


@pytest.fixture
def page_image(tmp_path: Path) -> Path:
    image = tmp_path / "cache" / "pages" / "aa" / "page.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_png(1200, 1600, b"verified-page-image"))
    image.chmod(0o400)
    return image


def _events(fake_codex: Path) -> list[dict]:
    log = fake_codex.with_suffix(".log")
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _executor(fake_codex: Path, tmp_path: Path, *, timeout: float = 2) -> CodexVisionExecutor:
    return CodexVisionExecutor(
        codex_path=fake_codex,
        temp_root=tmp_path / "jobs",
        trusted_image_root=tmp_path / "cache",
        timeout=timeout,
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


def _sample_transcription() -> dict:
    return {
        "page": {"number": 1, "width": 1200, "height": 1600},
        "blocks": [],
        "uncertain_items": [],
        "reading_order": [],
    }


def _resolve_ref(schema: dict, value: dict) -> dict:
    ref = value.get("$ref")
    if not ref:
        return value
    current = schema
    for part in ref.removeprefix("#/").split("/"):
        current = current[part]
    return current


def _sample_apple() -> dict:
    return {
        "engine": "apple_vision",
        "observations": [
            {
                "text": "visible apple text",
                "confidence": 0.7,
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
            }
        ],
    }


def _sample_diff() -> dict:
    return {
        "conflicts": [
            {
                "id": "c1",
                "region": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "codex": "visible text only",
                "apple": "visible apple text",
            }
        ]
    }


def test_transcription_schema_is_strict_structured_object():
    schema = json.loads(
        Path("src/parsing_core/workbench/ocr/schemas/page-transcription.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["page", "blocks", "uncertain_items", "reading_order"]
    block = _resolve_ref(schema, schema["properties"]["blocks"]["items"])
    assert block["additionalProperties"] is False
    assert set(block["properties"]["type"]["enum"]) == {
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
    }
    bbox = _resolve_ref(schema, block["properties"]["bounding_box"])
    assert bbox["additionalProperties"] is False
    bbox_x = _resolve_ref(schema, bbox["properties"]["x"])
    bbox_width = _resolve_ref(schema, bbox["properties"]["width"])
    confidence = _resolve_ref(schema, block["properties"]["confidence"])
    assert bbox_x["minimum"] == 0
    assert bbox_x["maximum"] == 1
    assert bbox_width["maximum"] == 1
    assert confidence["minimum"] == 0
    assert confidence["maximum"] == 1
    assert "table" in block["properties"]
    assert "formula" in block["properties"]
    assert "markdown" not in schema["properties"]


def test_adjudication_schema_requires_region_evidence_and_status():
    schema = json.loads(
        Path("src/parsing_core/workbench/ocr/schemas/page-adjudication.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "page",
        "final_blocks",
        "resolved_conflicts",
        "tables",
        "formulas",
        "decision_evidence",
        "confidence",
        "status",
    ]
    assert schema["properties"]["status"]["enum"] == ["accepted", "needs_review", "rejected"]
    conflict = _resolve_ref(schema, schema["properties"]["resolved_conflicts"]["items"])
    assert {"region", "evidence"} <= set(conflict["required"])
    assert conflict["additionalProperties"] is False


def test_transcription_exec_uses_fixed_ephemeral_readonly_argv_and_visible_only_prompt(
    tmp_path, fake_codex, page_image, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-api-key")
    monkeypatch.setenv("KEYCHAIN_PASSWORD", "secret-keychain")
    monkeypatch.setenv("PYTHONPATH", "/tmp/secret-pythonpath")
    executor = _executor(fake_codex, tmp_path)

    result = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    start = next(event for event in _events(fake_codex) if event["event"] == "start")
    prompt = next(event for event in _events(fake_codex) if event["event"] == "prompt")
    assert start["argv"][1:] == [
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--image",
        "page.png",
        "--output-schema",
        "page-transcription.json",
        "--output-last-message",
        "result.json",
        "-",
    ]
    assert Path(start["cwd"]).parent == tmp_path / "jobs"
    assert prompt["image_exists"] is True
    assert prompt["schema_exists"] is True
    assert "only transcribe visible content" in prompt["prompt"]
    assert "mark invisible or inferred content as uncertain" in prompt["prompt"]
    assert str(page_image) not in prompt["prompt"]
    assert "PDF2MD" not in prompt["prompt"]
    assert "教材" not in prompt["prompt"]
    assert "OPENAI_API_KEY" not in start["env_keys"]
    assert "KEYCHAIN_PASSWORD" not in start["env_keys"]
    assert "PYTHONPATH" not in start["env_keys"]
    assert result.payload["blocks"][0]["text"] == "visible text only"
    assert result.record["kind"] == "transcription"
    assert result.record["codex_version"] == "codex-cli 9.9.9"
    assert result.record["codex_sha256"]
    assert result.record["cache_key"]


def test_codex_popen_never_uses_shell(tmp_path, fake_codex, page_image, monkeypatch):
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    calls = []
    real_popen = codex_vision.subprocess.Popen

    def wrapped_popen(*args, **kwargs):
        calls.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(codex_vision.subprocess, "Popen", wrapped_popen)

    _executor(fake_codex, tmp_path).transcribe_page(
        page_image, page_number=1, width=1200, height=1600
    )

    assert calls
    assert all(call.get("shell") is not True for call in calls)


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "exec", "--resume", "old", "--sandbox", "read-only", "-"],
        ["codex", "exec", "--session", "old", "--sandbox", "read-only", "-"],
        ["codex", "exec", "--sandbox", "workspace-write", "-"],
        ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "-"],
    ],
)
def test_old_session_resume_and_dangerous_sandbox_argv_are_rejected(argv):
    with pytest.raises(CodexVisionError):
        validate_codex_exec_argv(argv)


def test_adjudication_exec_uses_readonly_inputs_and_evidence_prompt(
    tmp_path, fake_codex, page_image
):
    crops = []
    for index in range(4):
        crop = page_image.parent / f"crop-{index}.png"
        crop.write_bytes(_png(100, 100, f"crop-{index}".encode()))
        crop.chmod(0o400)
        crops.append(crop)
    executor = _executor(fake_codex, tmp_path)

    result = executor.adjudicate_page(
        page_image,
        page_number=1,
        width=1200,
        height=1600,
        codex_observation=_sample_transcription(),
        apple_observation=_sample_apple(),
        baidu_observation={"engine": "baidu_pp_structure", "observations": []},
        diff=_sample_diff(),
        crop_images=crops,
    )

    starts = [event for event in _events(fake_codex) if event["event"] == "start"]
    assert starts[-1]["argv"][1:] == [
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--image",
        "page.png",
        "--image",
        "crop-1.png",
        "--image",
        "crop-2.png",
        "--image",
        "crop-3.png",
        "--image",
        "crop-4.png",
        "--output-schema",
        "page-adjudication.json",
        "--output-last-message",
        "result.json",
        "-",
    ]
    prompt = [event for event in _events(fake_codex) if event["event"] == "prompt"][-1]["prompt"]
    assert "resolve conflicts using region-specific evidence" in prompt
    assert "do not decide by majority vote alone" in prompt
    assert str(page_image) not in prompt
    assert result.payload["status"] == "accepted"
    assert result.payload["resolved_conflicts"][0]["region"]
    assert result.payload["resolved_conflicts"][0]["evidence"]


def test_adjudication_rejects_more_than_four_crops_and_unverified_paths(
    tmp_path, fake_codex, page_image
):
    executor = _executor(fake_codex, tmp_path)
    crops = []
    for index in range(5):
        crop = page_image.parent / f"crop-too-many-{index}.png"
        crop.write_bytes(_png(100, 100, b"crop"))
        crops.append(crop)
    with pytest.raises(CodexVisionError, match="too many crop images"):
        executor.adjudicate_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            codex_observation=_sample_transcription(),
            apple_observation=_sample_apple(),
            diff=_sample_diff(),
            crop_images=crops,
        )

    outside = tmp_path / "outside.png"
    outside.write_bytes(_png(1200, 1600, b"outside"))
    link = page_image.parent / "link.png"
    link.symlink_to(outside)
    with pytest.raises(CodexVisionError, match="image input is not available"):
        executor.transcribe_page(link, page_number=1, width=1200, height=1600)

    with pytest.raises(CodexVisionError, match="image input is not available"):
        executor.transcribe_page(Path("/etc/hosts"), page_number=1, width=1200, height=1600)


def test_large_prefill_and_large_prompt_are_drained_until_deadline(tmp_path, page_image):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="prefill_then_read")
    executor = _executor(fake_codex, tmp_path, timeout=0.5)

    with pytest.raises(CodexVisionError, match="codex cli output exceeded limit"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert list((tmp_path / "jobs").glob("*")) == []


def test_large_stdin_and_stdout_are_drained_concurrently(tmp_path):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="prefill_512_then_read")
    (tmp_path / "cache").mkdir()
    (tmp_path / "page.png").write_bytes(_png(1, 1))
    executor = _executor(fake_codex, tmp_path)
    argv = [
        str(fake_codex),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--output-schema",
        "page-transcription.json",
        "--output-last-message",
        "result.json",
        "-",
    ]
    output = executor._communicate(argv, "p" * (2 * 1024 * 1024), tmp_path)
    assert len(output) == 512 * 1024
    assert any(event["event"] == "prompt" for event in _events(fake_codex))


def test_timeout_kills_parent_and_child_and_cleans_tempdir(tmp_path, page_image):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="spawn_child_ignore_term")
    executor = _executor(fake_codex, tmp_path, timeout=0.5)

    with pytest.raises(CodexVisionError, match="codex cli timed out"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    events = _events(fake_codex)
    child_pid = next(event["pid"] for event in events if event["event"] == "child_start")
    try:
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
    assert list((tmp_path / "jobs").glob("*")) == []


def test_version_probe_rejects_unbounded_output(tmp_path):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="version_huge_stdout")
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        _executor(fake_codex, tmp_path)


def test_version_probe_timeout_kills_parent_and_child(tmp_path):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="version_spawn_child_timeout")
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        _executor(fake_codex, tmp_path, timeout=0.5)

    events = _events(fake_codex)
    child_pid = next(
        event["child_pid"] for event in events if event["event"] == "version_child_spawned"
    )
    try:
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("huge_stdout", "codex cli output exceeded limit"),
        ("huge_stderr", "codex cli output exceeded limit"),
        ("huge_result", "codex cli result exceeded limit"),
        ("exit_nonzero", "codex cli failed"),
    ],
)
def test_stdout_stderr_result_limits_and_errors_are_sanitized(
    tmp_path, page_image, mode, message
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode=mode)
    executor = _executor(fake_codex, tmp_path)

    with pytest.raises(CodexVisionError) as error:
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    rendered = _exception_surface(error.value)
    assert str(error.value) == message
    assert "/Users/laoer/Documents/PDF2MD/book.pdf" not in rendered
    assert "教材" not in rendered
    assert "OPENAI_API_KEY" not in rendered


@pytest.mark.parametrize("mode", ["result_symlink", "result_fifo"])
def test_result_json_rejects_symlink_and_fifo_without_blocking(tmp_path, page_image, mode):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode=mode)
    executor = _executor(fake_codex, tmp_path, timeout=0.5)
    def raise_if_blocked(_signum, _frame):
        raise AssertionError("result reader blocked")

    previous = signal.signal(signal.SIGALRM, raise_if_blocked)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(CodexVisionError, match="codex cli returned invalid json"):
            executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_result_json_growth_is_bounded(tmp_path, page_image):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="result_growth")
    with pytest.raises(CodexVisionError, match="codex cli result exceeded limit"):
        _executor(fake_codex, tmp_path).transcribe_page(
            page_image, page_number=1, width=1200, height=1600
        )


def test_schema_failure_retries_once_then_succeeds_with_clean_tempdirs(tmp_path, page_image):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="invalid_once")
    executor = _executor(fake_codex, tmp_path)

    result = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert result.payload["blocks"][0]["text"] == "visible text only"
    assert result.record["attempts"] == 2
    assert len([event for event in _events(fake_codex) if event["event"] == "start"]) == 2
    assert list((tmp_path / "jobs").glob("*")) == []


def test_schema_failure_twice_is_recoverable_and_cleans_tempdirs(tmp_path, page_image):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="always_invalid")
    executor = _executor(fake_codex, tmp_path)

    with pytest.raises(CodexVisionError, match="codex cli returned invalid schema"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert len([event for event in _events(fake_codex) if event["event"] == "start"]) == 2
    assert list((tmp_path / "jobs").glob("*")) == []


def test_executable_symlink_replacement_permissions_and_env_leak_are_rejected(
    tmp_path, page_image, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-api-key")
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py")
    fake_codex.chmod(0o722)
    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        _executor(fake_codex, tmp_path)

    fake_codex.chmod(0o700)
    executor = _executor(fake_codex, tmp_path)
    replacement = _write_fake_codex(tmp_path / "replacement.py")
    fake_codex.unlink()
    fake_codex.symlink_to(replacement)

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    assert not any(event["event"] == "start" for event in _events(fake_codex))


def test_observation_and_diff_inputs_are_strict_json_bounded_and_sanitized(
    tmp_path, fake_codex, page_image
):
    executor = _executor(fake_codex, tmp_path)
    with pytest.raises(CodexVisionError, match="codex cli input is too large"):
        executor.adjudicate_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            codex_observation={"text": "x" * (2 * 1024 * 1024)},
            apple_observation=_sample_apple(),
            diff=_sample_diff(),
        )

    too_deep = value = {}
    for _ in range(40):
        value["next"] = {}
        value = value["next"]
    with pytest.raises(CodexVisionError, match="codex cli input is invalid"):
        executor.adjudicate_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            codex_observation=too_deep,
            apple_observation=_sample_apple(),
            diff=_sample_diff(),
        )

    sensitive_path = "/Users/laoer/Documents/" + "PDF2MD/" + "sensitive.pdf"
    with pytest.raises(CodexVisionError) as error:
        executor.adjudicate_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            codex_observation={"debug_path": sensitive_path},
            apple_observation=_sample_apple(),
            diff=_sample_diff(),
        )
    assert sensitive_path not in _exception_surface(error.value)


def test_image_root_hash_and_format_are_enforced(tmp_path, fake_codex, page_image):
    executor = _executor(fake_codex, tmp_path)
    expected = __import__("hashlib").sha256(page_image.read_bytes()).hexdigest()
    result = executor.transcribe_page(
        page_image, page_number=1, width=1200, height=1600, expected_image_sha256=expected
    )
    assert result.payload["page"]["number"] == 1

    with pytest.raises(CodexVisionError, match="image input is not available"):
        executor.transcribe_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            expected_image_sha256="0" * 64,
        )

    plain = page_image.parent / "plain.txt"
    plain.write_bytes(b"not an image")
    with pytest.raises(CodexVisionError, match="image input is not available"):
        executor.transcribe_page(plain, page_number=1, width=1200, height=1600)

    hardlink = page_image.parent / "hardlink.png"
    hardlink.hardlink_to(page_image)
    with pytest.raises(CodexVisionError, match="image input is not available"):
        executor.transcribe_page(hardlink, page_number=1, width=1200, height=1600)


def test_verified_image_copy_preserves_all_source_bytes(tmp_path, fake_codex, page_image):
    executor = _executor(fake_codex, tmp_path)
    executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    prompt_event = [event for event in _events(fake_codex) if event["event"] == "prompt"][-1]
    assert prompt_event["image_size"] == page_image.stat().st_size
    assert prompt_event["image_sha256"] == hashlib.sha256(page_image.read_bytes()).hexdigest()


def test_jsonschema_validator_is_used_and_validator_errors_are_sanitized(
    tmp_path, fake_codex, page_image, monkeypatch
):
    executor = _executor(fake_codex, tmp_path)
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    class BrokenValidator:
        def iter_errors(self, value):
            raise TypeError("secret /Users/laoer/Documents/PDF2MD")

    monkeypatch.setattr(codex_vision, "_SCHEMA_VALIDATORS", {"transcription": BrokenValidator()})
    with pytest.raises(CodexVisionError, match="codex cli returned invalid schema") as error:
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    assert "secret /Users/laoer/Documents/PDF2MD" not in str(error.value)


def test_non_dict_page_and_path_like_observation_are_wrapped(
    tmp_path, fake_codex, page_image, monkeypatch
):
    executor = _executor(fake_codex, tmp_path)
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    monkeypatch.setattr(
        codex_vision,
        "_read_result_json",
        lambda path, **_kwargs: {
            "page": [],
            "blocks": [],
            "uncertain_items": [],
            "reading_order": [],
        },
    )
    with pytest.raises(CodexVisionError, match="codex cli returned invalid schema"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    for value in ("/tmp/course/page.png", "file:///tmp/course/page.png", "../course/page.png"):
        with pytest.raises(CodexVisionError, match="codex cli input is invalid"):
            executor.adjudicate_page(
                page_image,
                page_number=1,
                width=1200,
                height=1600,
                codex_observation={"debug": value},
                apple_observation=_sample_apple(),
                diff=_sample_diff(),
            )


def test_real_codex_cli_dry_run_is_skipped_when_unavailable(tmp_path, page_image):
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("real codex cli is not installed")
    try:
        executor = CodexVisionExecutor(
            codex_path=codex,
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=0.1,
        )
    except CodexVisionError as exc:
        pytest.skip(f"real codex cli is not a direct safe executable: {exc}")
    with pytest.raises(CodexVisionError):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_gone(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)
