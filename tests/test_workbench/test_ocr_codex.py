import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import parsing_core.workbench.ocr.codex_vision as codex_vision_module
import parsing_core.workbench.secure_codex as secure_codex_module
from parsing_core.workbench.codex_cli import resolve_codex_path
from parsing_core.workbench.ocr.codex_vision import (
    CodexVisionError,
    CodexVisionExecutor,
    validate_codex_exec_argv,
)
from parsing_core.workbench.ocr.orchestrator import BatchStatus, OcrOrchestrator

_DISABLED_FEATURES = secure_codex_module.DISABLED_CODEX_FEATURES

_SUPPORTED_VERSION = "codex-cli 0.142.1"
_FEATURES_OUTPUT = "\n".join(
    [
        *(f"{name:<36} stable             false" for name in _DISABLED_FEATURES),
        "resize_all_images                   removed            true",
        "terminal_resize_reflow              removed            true",
        "tui_app_server                      removed            true",
    ]
)


def _secure_exec_prefix(executable: str) -> list[str]:
    prefix = [
        executable,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
    ]
    for feature in _DISABLED_FEATURES:
        prefix.extend(["--disable", feature])
    prefix.extend(["--sandbox", "read-only"])
    return prefix


FAKE_CODEX = r"""
import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path('__PDF2MD_CONFIG_PATH__')
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
log_path = Path(config["log_path"])
mode = config.get("mode", "success")
version = config.get("version", "codex-cli 0.142.1")
log_path.parent.mkdir(parents=True, exist_ok=True)

def log(event, **extra):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, **extra}, sort_keys=True) + "\n")

def spawn_success_child(kind):
    child_code = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

log_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
kind = sys.argv[3]
parent_pid = int(sys.argv[4])
signal.signal(signal.SIGTERM, lambda signum, frame: None)
os.environ.clear()
with log_path.open("a", encoding="utf-8") as handle:
    event = {
        "event": "success_child_start",
        "env_keys": sorted(os.environ),
        "kind": kind,
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
    ready_path = log_path.with_name(f"{log_path.name}.{kind}.{os.getpid()}.ready")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(log_path),
            str(ready_path),
            kind,
            str(os.getpid()),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    log("success_child_spawned", child_pid=child.pid, kind=kind)
    ready_deadline = time.monotonic() + 2
    while not ready_path.exists():
        if time.monotonic() >= ready_deadline:
            child.kill()
            raise RuntimeError("success child did not start")
        time.sleep(0.01)

if sys.argv[-2:] == ["features", "list"]:
    print(config["features_output"])
    raise SystemExit(0)

if sys.argv[1:] == ["--version"]:
    log("version_start", pgid=os.getpgrp(), pid=os.getpid(), sid=os.getsid(0))
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
ready_path = Path(sys.argv[2])
os.environ.clear()
signal.signal(signal.SIGTERM, lambda signum, frame: None)
with log_path.open("a", encoding="utf-8") as handle:
    event = {
        "event": "version_child_start",
        "env_keys": sorted(os.environ),
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "sid": os.getsid(0),
    }
    handle.write(json.dumps(event) + "\n")
ready_path.write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(1)
'''
        ready_path = log_path.with_suffix(".version-child-ready")
        ready_path.unlink(missing_ok=True)
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(log_path), str(ready_path)]
        )
        log("version_child_spawned", child_pid=child.pid)
        ready_deadline = time.monotonic() + 2
        while not ready_path.exists():
            if time.monotonic() >= ready_deadline:
                child.kill()
                raise RuntimeError("version child did not become ready")
            time.sleep(0.01)
        log("version_child_ready", child_pid=child.pid)
        signal.signal(signal.SIGTERM, lambda signum, frame: None)
        while True:
            time.sleep(1)
    if mode == "version_close_pipes_ignore_term":
        signal.signal(signal.SIGTERM, lambda signum, frame: None)
        log("version_pipes_closed")
        os.close(0)
        os.close(1)
        os.close(2)
        while True:
            time.sleep(1)
    if mode == "success_child_devnull_ignore_term":
        spawn_success_child("version")
    print(version)
    raise SystemExit(0)

log(
    "start",
    argv=sys.argv,
    env_keys=sorted(os.environ),
    cwd=os.getcwd(),
    pgid=os.getpgrp(),
    pid=os.getpid(),
    sid=os.getsid(0),
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
if mode == "success_child_devnull_ignore_term":
    spawn_success_child("exec")
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


def _write_fake_codex(
    path: Path,
    *,
    mode: str = "success",
    version: str = _SUPPORTED_VERSION,
    features_output: str = _FEATURES_OUTPUT,
):
    config_path = path.with_suffix(".config.json")
    fake_source = FAKE_CODEX.replace("__PDF2MD_CONFIG_PATH__", str(config_path))
    path.write_text(f"#!{sys.executable}\n{fake_source}", encoding="utf-8")
    path.chmod(0o700)
    config_path.write_text(
        json.dumps(
            {
                "features_output": features_output,
                "log_path": str(path.with_suffix(".log")),
                "mode": mode,
                "version": version,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    return _write_fake_codex(tmp_path / "fake_codex.py")


_LOCAL_BINARY_POLICY_TOKEN = object()


@dataclass(frozen=True)
class _LocalBinaryPolicyGrant:
    path: Path
    token: object

    def permits(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return self.token is _LOCAL_BINARY_POLICY_TOKEN and self.path == resolved


class _LocalFakeCodexPolicy:
    def __init__(self, original_policy: Callable[..., None]) -> None:
        self._original_policy = original_policy
        self._allowed: dict[Path, secure_codex_module.ExecutableIdentity] = {}

    def authorize(self, path: Path) -> _LocalBinaryPolicyGrant:
        fd, identity = secure_codex_module._open_executable(path, enforce_official_policy=False)
        descriptor = fd
        fd = -1
        os.close(descriptor)
        self._allowed[identity.path] = identity
        return _LocalBinaryPolicyGrant(identity.path, _LOCAL_BINARY_POLICY_TOKEN)

    def verify(
        self,
        path: Path,
        fd: int,
        identity: secure_codex_module.ExecutableIdentity,
    ) -> None:
        if self._allowed.get(path) == identity:
            return
        self._original_policy(path, fd, identity)


@pytest.fixture
def local_fake_codex_policy() -> Iterator[_LocalFakeCodexPolicy]:
    original_policy = secure_codex_module._OFFICIAL_BINARY_POLICY_PROBE
    assert original_policy is secure_codex_module._verify_official_binary_policy
    policy = _LocalFakeCodexPolicy(original_policy)
    with pytest.MonkeyPatch.context() as policy_patch:
        policy_patch.setattr(
            secure_codex_module,
            "_OFFICIAL_BINARY_POLICY_PROBE",
            policy.verify,
        )
        yield policy
    assert secure_codex_module._OFFICIAL_BINARY_POLICY_PROBE is original_policy


def test_fake_codex_without_explicit_local_policy_is_rejected(tmp_path: Path) -> None:
    fake = _write_fake_codex(tmp_path / "unauthorized-fake-codex.py")
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        CodexVisionExecutor(
            codex_path=fake,
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=5,
        )


def test_local_binary_policy_fixture_allows_only_granted_fake_and_restores(
    tmp_path: Path, local_fake_codex_policy
) -> None:
    allowed = _write_fake_codex(tmp_path / "allowed-fake-codex.py")
    denied = _write_fake_codex(tmp_path / "denied-fake-codex.py")
    (tmp_path / "cache").mkdir()
    _executor(allowed, tmp_path, local_fake_codex_policy)

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        CodexVisionExecutor(
            codex_path=denied,
            temp_root=tmp_path / "denied-jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=5,
        )


@pytest.mark.parametrize(
    "timeout",
    [True, False, 0, -1, math.nan, math.inf, -math.inf, 3600.001, "60"],
)
def test_codex_vision_rejects_invalid_timeout_before_binary_policy(
    tmp_path: Path,
    timeout: object,
) -> None:
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="^codex cli failed$"):
        CodexVisionExecutor(
            codex_path=tmp_path / "sensitive-fake-codex",
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=timeout,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "deadline_factory",
    [
        lambda: True,
        lambda: False,
        lambda: 0,
        lambda: -1,
        lambda: math.nan,
        lambda: math.inf,
        lambda: -math.inf,
        lambda: time.monotonic() + 3600.001,
    ],
)
def test_codex_vision_rejects_invalid_constructor_deadline(
    tmp_path: Path,
    local_fake_codex_policy,
    deadline_factory,
) -> None:
    fake = _write_fake_codex(tmp_path / "fake-codex.py")
    local_fake_codex_policy.authorize(fake)
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="^codex cli failed$"):
        CodexVisionExecutor(
            codex_path=fake,
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=2,
            deadline=deadline_factory(),
        )


def test_codex_vision_page_deadline_rejects_infinite_remaining_time(
    tmp_path: Path,
    fake_codex: Path,
    page_image: Path,
    local_fake_codex_policy,
) -> None:
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    with pytest.raises(CodexVisionError, match="^codex cli failed$"):
        executor.transcribe_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            deadline=math.inf,
        )


def test_codex_vision_communicate_rejects_oversized_prompt_before_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_codex: Path,
    local_fake_codex_policy,
) -> None:
    (tmp_path / "cache").mkdir()
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    def unexpected_run(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized prompt reached secure runner")

    monkeypatch.setattr(executor._runner, "run", unexpected_run)

    with executor._runner.private_task() as task_dir:
        with pytest.raises(CodexVisionError, match="^codex cli input is too large$"):
            executor._communicate(
                _secure_exec_prefix(str(executor.codex_path))
                + [
                    "--image",
                    "page.png",
                    "--output-schema",
                    "page-transcription.json",
                    "--output-last-message",
                    "result.json",
                    "-",
                ],
                "教材" * (256 * 1024),
                task_dir,
            )


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


def _exec_starts(fake_codex: Path, schema_name: str | None = None) -> list[dict]:
    starts = [event for event in _events(fake_codex) if event.get("event") == "start"]
    if schema_name is None:
        return starts
    return [event for event in starts if schema_name in event.get("argv", [])]


def _wait_for_event(fake_codex: Path, name: str, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _events(fake_codex):
            if event.get("event") == name:
                return event
        time.sleep(0.01)
    raise AssertionError(f"codex event {name!r} was not observed")


def _set_fake_codex_mode(fake_codex: Path, mode: str) -> None:
    config_path = fake_codex.with_suffix(".config.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = mode
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")


def _orchestrator_observation(text: str) -> dict:
    return {
        "id": "apple-observation",
        "engine": "apple_vision",
        "input_fingerprint": "image-sha",
        "page": {"number": 1, "width": 1200, "height": 1600},
        "blocks": [
            {
                "id": "apple-block",
                "type": "paragraph",
                "text": text,
                "region": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                "confidence": 0.9,
                "reading_order": 1,
                "candidates": [],
                "uncertainty_reason": "",
                "table": None,
                "formula": None,
                "source_region": "r1",
            }
        ],
        "uncertain_items": [],
        "reading_order": ["apple-block"],
    }


def _codex_orchestrator(
    tmp_path: Path,
    executor: CodexVisionExecutor,
    page_image: Path,
    cancel: threading.Event,
) -> tuple[OcrOrchestrator, Path]:
    pdf = tmp_path / "orchestrator-book.pdf"
    pdf.write_bytes(b"%PDF-1.7\nCodex cancellation fixture\n")
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    image_sha256 = hashlib.sha256(page_image.read_bytes()).hexdigest()

    def recognize(*_args, **_kwargs):
        observation = _orchestrator_observation("visible text only")
        observation["input_fingerprint"] = image_sha256
        return SimpleNamespace(
            page=1,
            image_path=str(page_image),
            image_sha256=image_sha256,
            width=1200,
            height=1600,
            pdf_sha256=pdf_sha256,
            observation=observation,
        )

    def never(*_args, **_kwargs):
        raise AssertionError("unexpected Baidu")

    orchestrator = OcrOrchestrator(
        vision=SimpleNamespace(recognize=recognize),
        codex=executor,
        baidu=SimpleNamespace(recognize=never),
        state_root=tmp_path / "codex-orchestrator-state",
        image_loader=lambda _path, **_kwargs: b"image",
        is_cancelled=cancel.is_set,
    )
    return orchestrator, pdf


def _kill_fixture_group(root: dict) -> None:
    process_group_id = root["pgid"]
    if process_group_id != root["pid"] or process_group_id == os.getpgrp():
        raise AssertionError("fixture Codex did not use a dedicated process group")
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _executor(
    fake_codex: Path,
    tmp_path: Path,
    local_binary_policy: _LocalFakeCodexPolicy,
    *,
    timeout: float = 2,
    task_observer=None,
    cache_limits=None,
) -> CodexVisionExecutor:
    policy_grant = local_binary_policy.authorize(fake_codex)
    if not policy_grant.permits(fake_codex):
        raise AssertionError("fake Codex requires an explicit local policy grant")
    options = {}
    if task_observer is not None:
        options["task_observer"] = task_observer
    if cache_limits is not None:
        options["cache_limits"] = cache_limits
    executor = CodexVisionExecutor(
        codex_path=fake_codex,
        temp_root=tmp_path / "jobs",
        trusted_image_root=tmp_path / "cache",
        timeout=max(timeout, 2),
        **options,
    )
    executor.timeout = float(timeout)
    return executor


def _exception_surface(error: BaseException) -> str:
    rendered = ["".join(traceback.format_exception(error))]
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend(
            (
                str(current),
                repr(current),
                repr(current.args),
                repr(getattr(current, "__notes__", ())),
            )
        )
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return "\n".join(rendered)


def test_codex_vision_public_error_recursively_removes_sensitive_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sensitive = "教材 SECRET-TOKEN /Users/private/book.pdf"
    source = OSError(sensitive)
    source.add_note(f"Bearer {sensitive}")
    nested = secure_codex_module.SecureCodexError(sensitive)
    nested.__context__ = source
    nested.add_note(sensitive)

    def fail_runner(*_args: object, **_kwargs: object) -> None:
        raise nested

    monkeypatch.setattr(codex_vision_module, "SecureCodexRunner", fail_runner)
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError) as error:
        CodexVisionExecutor(
            codex_path="/private/tmp/sensitive-codex",
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=1,
        )

    assert str(error.value) == "codex cli is not available"
    assert sensitive not in _exception_surface(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not getattr(error.value, "__notes__", ())


def _assert_observed_tasks_clean(paths: list[Path], tmp_path: Path) -> None:
    assert paths
    assert all(path.name.startswith("task-") for path in paths)
    assert all(not path.is_relative_to(tmp_path / "jobs") for path in paths)
    assert all(not path.exists() for path in paths)


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
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-api-key")
    monkeypatch.setenv("KEYCHAIN_PASSWORD", "secret-keychain")
    monkeypatch.setenv("PYTHONPATH", "/tmp/secret-pythonpath")
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    result = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    start = next(event for event in _events(fake_codex) if event["event"] == "start")
    prompt = next(event for event in _events(fake_codex) if event["event"] == "prompt")
    assert start["argv"] == _secure_exec_prefix(start["argv"][0]) + [
        "--image",
        "page.png",
        "--output-schema",
        "page-transcription.json",
        "--output-last-message",
        "result.json",
        "-",
    ]
    execution_cwd = Path(start["cwd"])
    assert not execution_cwd.is_relative_to(tmp_path)
    assert not execution_cwd.exists()
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
    assert "PDF2MD_HELPER_CLEANUP_TOKEN" not in start["env_keys"]
    assert result.payload["blocks"][0]["text"] == "visible text only"
    assert result.record["kind"] == "transcription"
    assert result.record["codex_version"] == _SUPPORTED_VERSION
    assert result.record["codex_sha256"]
    assert result.record["cache_key"]


def test_codex_popen_never_uses_shell(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    import parsing_core.workbench.secure_codex as secure_codex

    calls = []
    real_popen = secure_codex.subprocess.Popen

    def wrapped_popen(*args, **kwargs):
        calls.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(secure_codex.subprocess, "Popen", wrapped_popen)

    _executor(fake_codex, tmp_path, local_fake_codex_policy).transcribe_page(
        page_image, page_number=1, width=1200, height=1600
    )

    assert calls
    assert all(call.get("shell") is not True for call in calls)
    assert all(call.get("process_group") == 0 for call in calls)


def test_codex_helpers_use_dedicated_process_groups_in_caller_session(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    helpers = [
        event for event in _events(fake_codex) if event["event"] in {"version_start", "start"}
    ]
    assert {event["event"] for event in helpers} == {"version_start", "start"}
    assert all(event["pgid"] == event["pid"] for event in helpers)
    assert all(event["pgid"] != os.getpgrp() for event in helpers)
    assert all(event["sid"] == os.getsid(0) for event in helpers)
    assert len({event["pgid"] for event in helpers}) == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--resume", "old"],
        ["--session", "old"],
        ["--enable", "shell_tool"],
        ["--disable", "shell_tool"],
        ["--config", "features.shell_tool=true"],
        ["--profile", "unsafe"],
        ["--add-dir", "/tmp"],
        ["--dangerously-bypass-approvals-and-sandbox"],
    ],
)
def test_vision_argv_cannot_resume_reenable_tools_or_add_options(extra):
    argv = _secure_exec_prefix("/trusted/codex") + [
        "--image",
        "page.png",
        "--output-schema",
        "page-transcription.json",
        "--output-last-message",
        "result.json",
        *extra,
        "-",
    ]

    with pytest.raises(CodexVisionError):
        validate_codex_exec_argv(argv)


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("image", "../page.png"),
        ("image", "/tmp/page.png"),
        ("schema", "../schema.json"),
        ("schema", "other-schema.json"),
        ("output", "../result.json"),
        ("output", "/tmp/result.json"),
    ],
)
def test_vision_argv_rejects_unsafe_image_schema_and_output_paths(field, unsafe):
    image = unsafe if field == "image" else "page.png"
    schema = unsafe if field == "schema" else "page-transcription.json"
    output = unsafe if field == "output" else "result.json"
    argv = _secure_exec_prefix("/trusted/codex") + [
        "--image",
        image,
        "--output-schema",
        schema,
        "--output-last-message",
        output,
        "-",
    ]

    with pytest.raises(CodexVisionError):
        validate_codex_exec_argv(argv)


def test_adjudication_exec_uses_readonly_inputs_and_evidence_prompt(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    crops = []
    for index in range(4):
        crop = page_image.parent / f"crop-{index}.png"
        crop.write_bytes(_png(100, 100, f"crop-{index}".encode()))
        crop.chmod(0o400)
        crops.append(crop)
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

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
    assert starts[-1]["argv"] == _secure_exec_prefix(starts[-1]["argv"][0]) + [
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
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
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


def test_large_prefill_and_large_prompt_are_drained_until_deadline(
    tmp_path, page_image, monkeypatch, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="prefill_then_read")
    observed_tasks = []
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        timeout=0.5,
        task_observer=observed_tasks.append,
    )

    with pytest.raises(CodexVisionError, match="codex cli output exceeded limit"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    _assert_observed_tasks_clean(observed_tasks, tmp_path)


def test_large_stdin_and_stdout_are_drained_concurrently(tmp_path, local_fake_codex_policy):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="prefill_512_then_read")
    (tmp_path / "cache").mkdir()
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    with executor._runner.private_task() as task_dir:
        (task_dir / "page.png").write_bytes(_png(1, 1))
        (task_dir / "page-transcription.json").write_text("{}", encoding="utf-8")
        argv = _secure_exec_prefix(str(executor.codex_path)) + [
            "--image",
            "page.png",
            "--output-schema",
            "page-transcription.json",
            "--output-last-message",
            "result.json",
            "-",
        ]
        output = executor._communicate(
            argv,
            "p" * codex_vision_module._MAX_PROMPT_BYTES,
            task_dir,
        )
    assert len(output) == 512 * 1024
    assert any(event["event"] == "prompt" for event in _events(fake_codex))
    assert not task_dir.exists()


def test_timeout_kills_parent_and_child_and_cleans_tempdir(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="spawn_child_ignore_term")
    observed_tasks = []
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        timeout=0.5,
        task_observer=observed_tasks.append,
    )

    with pytest.raises(CodexVisionError, match="codex cli timed out"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    events = _events(fake_codex)
    child_pid = next(event["pid"] for event in events if event["event"] == "child_start")
    try:
        assert _wait_until_gone(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
    _assert_observed_tasks_clean(observed_tasks, tmp_path)


def test_successful_codex_calls_clean_devnull_children_that_ignore_term(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(
        tmp_path / "fake_codex.py", mode="success_child_devnull_ignore_term"
    )
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    result = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    events = _events(fake_codex)
    roots = {
        event["pid"]: event for event in events if event["event"] in {"version_start", "start"}
    }
    children = [event for event in events if event["event"] == "success_child_start"]
    child_pids = [event["pid"] for event in children]
    try:
        assert result.payload["blocks"][0]["text"] == "visible text only"
        assert {event["kind"] for event in children} == {"version", "exec"}
        assert len(children) == 2
        for child in children:
            root = roots[child["parent_pid"]]
            assert child["env_keys"] == []
            assert child["pgid"] == root["pgid"]
            assert child["sid"] == root["sid"]
            assert _wait_until_gone(child["pid"])
    finally:
        for child_pid in child_pids:
            if _pid_alive(child_pid):
                os.kill(child_pid, signal.SIGKILL)


def test_version_probe_rejects_unbounded_output(tmp_path, local_fake_codex_policy):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="version_huge_stdout")
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        _executor(fake_codex, tmp_path, local_fake_codex_policy)


def test_version_probe_timeout_before_child_spawn_is_safe(tmp_path, local_fake_codex_policy):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="version_spawn_child_timeout")
    local_fake_codex_policy.authorize(fake_codex)
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        CodexVisionExecutor(
            codex_path=fake_codex,
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=5,
            deadline=time.monotonic(),
        )

    events = _events(fake_codex)
    root_pid = next(
        (event["pid"] for event in events if event["event"] == "version_start"),
        None,
    )
    child_pid = next(
        (event["child_pid"] for event in events if event["event"] == "version_child_spawned"),
        None,
    )
    try:
        if root_pid is not None:
            assert _wait_until_gone(root_pid)
        if child_pid is not None:
            assert _wait_until_gone(child_pid)
    finally:
        if child_pid is not None and _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_version_probe_timeout_after_child_ready_reaps_descendant(
    tmp_path, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(
        tmp_path / "synchronized-timeout-codex.py",
        mode="version_spawn_child_timeout",
    )
    local_fake_codex_policy.authorize(fake_codex)
    (tmp_path / "cache").mkdir()
    outcomes = []

    def construct() -> None:
        try:
            CodexVisionExecutor(
                codex_path=fake_codex,
                temp_root=tmp_path / "jobs",
                trusted_image_root=tmp_path / "cache",
                timeout=5,
                deadline=time.monotonic() + 3,
            )
        except BaseException as error:
            outcomes.append(error)

    worker = threading.Thread(target=construct, name="codex-version-timeout-test")
    worker.start()
    child = _wait_for_event(fake_codex, "version_child_ready")
    root = _wait_for_event(fake_codex, "version_start")
    assert worker.is_alive(), "version timeout fired before the child-ready synchronization"
    worker.join(timeout=5)
    root_alive = _pid_alive(root["pid"])
    child_alive = _pid_alive(child["child_pid"])
    try:
        assert not worker.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], CodexVisionError)
        assert not root_alive, f"Codex version root PID {root['pid']} survived timeout"
        assert not child_alive, f"Codex version child PID {child['child_pid']} survived timeout"
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)
        worker.join(timeout=1)


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
    tmp_path, page_image, mode, message, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode=mode)
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    with pytest.raises(CodexVisionError) as error:
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    rendered = _exception_surface(error.value)
    assert str(error.value) == message
    assert "/Users/laoer/Documents/PDF2MD/book.pdf" not in rendered
    assert "教材" not in rendered
    assert "OPENAI_API_KEY" not in rendered


@pytest.mark.parametrize("mode", ["result_symlink", "result_fifo"])
def test_result_json_rejects_symlink_and_fifo_without_blocking(
    tmp_path, page_image, mode, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode=mode)
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy, timeout=0.5)

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


def test_image_fifo_without_writer_is_rejected_without_blocking(
    tmp_path, fake_codex, local_fake_codex_policy
):
    trusted_root = tmp_path / "cache"
    trusted_root.mkdir()
    fifo = trusted_root / "page.png"
    os.mkfifo(fifo)
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy, timeout=0.5)

    def hard_timeout(_signum, _frame):
        raise AssertionError("image input open blocked on FIFO")

    previous = signal.signal(signal.SIGALRM, hard_timeout)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    started = time.monotonic()
    try:
        with pytest.raises(CodexVisionError, match="image input is not available"):
            executor.transcribe_page(fifo, page_number=1, width=1200, height=1600)
    finally:
        elapsed = time.monotonic() - started
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert elapsed < 0.5


def test_result_json_growth_is_bounded(tmp_path, page_image, local_fake_codex_policy):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="result_growth")
    with pytest.raises(CodexVisionError, match="codex cli result exceeded limit"):
        _executor(fake_codex, tmp_path, local_fake_codex_policy).transcribe_page(
            page_image, page_number=1, width=1200, height=1600
        )


def test_result_json_rejects_same_inode_same_size_mutation(monkeypatch, tmp_path):
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    path = tmp_path / "result.json"
    path.write_bytes(b'{"value":"one"}')
    original = path.stat()
    real_read = os.read
    mutated = False

    def mutate_after_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(fd, size)
        if chunk and not mutated:
            mutated = True
            with path.open("r+b") as writer:
                writer.write(b'{"value":"two"}')
                writer.flush()
                os.fsync(writer.fileno())
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        return chunk

    monkeypatch.setattr(codex_vision, "_read_result_chunk", mutate_after_read, raising=False)

    with pytest.raises(CodexVisionError, match="codex cli returned invalid json"):
        codex_vision._read_result_json(path)


def test_schema_failure_retries_once_then_succeeds_with_clean_tempdirs(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="invalid_once")
    observed_tasks = []
    executor = _executor(
        fake_codex, tmp_path, local_fake_codex_policy, task_observer=observed_tasks.append
    )

    result = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert result.payload["blocks"][0]["text"] == "visible text only"
    assert result.record["attempts"] == 2
    assert len([event for event in _events(fake_codex) if event["event"] == "start"]) == 2
    _assert_observed_tasks_clean(observed_tasks, tmp_path)


def test_schema_failure_twice_is_recoverable_and_cleans_tempdirs(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py", mode="always_invalid")
    observed_tasks = []
    executor = _executor(
        fake_codex, tmp_path, local_fake_codex_policy, task_observer=observed_tasks.append
    )

    with pytest.raises(CodexVisionError, match="codex cli returned invalid schema"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert len([event for event in _events(fake_codex) if event["event"] == "start"]) == 2
    _assert_observed_tasks_clean(observed_tasks, tmp_path)


def test_group_writable_fake_executable_is_rejected_without_policy_bypass(
    tmp_path: Path,
) -> None:
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py")
    fake_codex.chmod(0o722)
    (tmp_path / "cache").mkdir()

    with pytest.raises(CodexVisionError, match="codex cli is not available"):
        CodexVisionExecutor(
            codex_path=fake_codex,
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=5,
        )


def test_executable_snapshot_ignores_later_source_symlink_replacement_and_env_leak(
    tmp_path, page_image, monkeypatch, local_fake_codex_policy
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-api-key")
    fake_codex = _write_fake_codex(tmp_path / "fake_codex.py")

    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    replacement = _write_fake_codex(tmp_path / "replacement.py")
    fake_codex.unlink()
    fake_codex.symlink_to(replacement)

    result = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert result.payload["blocks"][0]["text"] == "visible text only"
    start = next(event for event in _events(fake_codex) if event["event"] == "start")
    assert "OPENAI_API_KEY" not in start["env_keys"]


def test_observation_and_diff_inputs_are_strict_json_bounded_and_sanitized(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
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


def test_ordinary_mba_textbook_prose_with_product_words_is_allowed(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    apple = _sample_apple()
    apple["observations"][0]["text"] = (
        "这本 MBA 教材使用 PDF2MD 讨论平台战略，API key 是一个公开概念。"
    )

    result = executor.adjudicate_page(
        page_image,
        page_number=1,
        width=1200,
        height=1600,
        codex_observation={"text": "MBA 教材中的普通课程内容"},
        apple_observation=apple,
        diff=_sample_diff(),
    )

    assert result.payload["status"] == "accepted"


@pytest.mark.parametrize(
    "sensitive_object",
    [
        {"token": "public-looking", "password": "not-used"},
        {"metadata": {"clientSecret": "redacted", "authorization": "none"}},
        {"items": [{"source_uri": "urn:example", "callbackURL": "https://example.invalid"}]},
        {"key": "public-looking"},
        {"metadata": {"clientKey": "public-looking"}},
        {"items": [{"apiKey": "public-looking"}]},
    ],
)
def test_recursive_sensitive_keys_are_rejected_before_codex_execution(
    tmp_path, fake_codex, page_image, sensitive_object, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    with pytest.raises(CodexVisionError, match="input is invalid"):
        executor.adjudicate_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            codex_observation=sensitive_object,
            apple_observation=_sample_apple(),
            diff=_sample_diff(),
        )

    assert not _exec_starts(fake_codex, "page-adjudication.json")


def test_sensitive_key_matching_does_not_reject_benign_whole_words(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    result = executor.adjudicate_page(
        page_image,
        page_number=1,
        width=1200,
        height=1600,
        codex_observation={
            "tokenization": "教材分词练习",
            "secretary": "董事会秘书课程",
            "passwordless": "无密码登录是案例主题",
            "authoritative_model": "MBA 权限模型",
            "curriculum_urlology": "普通造词不应按 url 子串误杀",
            "keyboard": "组织行为课堂讨论键盘输入",
            "keynote": "战略演讲案例",
            "monkey": "普通英文词彙",
            "keyword": "教材关键词",
        },
        apple_observation=_sample_apple(),
        diff=_sample_diff(),
    )

    assert result.payload["status"] == "accepted"


@pytest.mark.parametrize(
    "secret_or_path",
    [
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "OPENAI_API_KEY=sk-secret-value",
        "Authorization: Bearer secret-token-value",
        "-----BEGIN PRIVATE KEY-----",
        "/Users/laoer/private/book.pdf",
        "file:///private/tmp/book.pdf",
        "../private/book.pdf",
    ],
)
def test_actual_secrets_and_unsafe_local_references_are_rejected(
    tmp_path, fake_codex, page_image, secret_or_path, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    with pytest.raises(CodexVisionError, match="input is invalid") as error:
        executor.adjudicate_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            codex_observation={"text": secret_or_path},
            apple_observation=_sample_apple(),
            diff=_sample_diff(),
        )

    assert secret_or_path not in _exception_surface(error.value)


def test_codex_result_cache_reuses_identical_validated_input_and_omits_executable_path(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)

    first = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    second = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert first.payload == second.payload
    assert len(_exec_starts(fake_codex, "page-transcription.json")) == 1
    assert first.record["cache_hit"] is False
    assert second.record["cache_hit"] is True
    assert "codex_path" not in first.record
    assert first.record["codex_sha256"]
    assert first.record["codex_version"] == _SUPPORTED_VERSION


def test_codex_cache_close_failure_never_closes_reused_fd(tmp_path, monkeypatch):
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    cache_dir = tmp_path / "result-cache"
    cache_dir.mkdir()
    directory_fd = os.open(cache_dir, os.O_RDONLY | os.O_DIRECTORY)
    reused_path = tmp_path / "reused-fd"
    close_failure = OSError("primary cache close failure")
    real_close = os.close
    reused_fd = None
    injected = False

    def release_reuse_then_fail(fd):
        nonlocal reused_fd, injected
        if fd != directory_fd and not injected:
            injected = True
            real_close(fd)
            reused_fd = os.open(reused_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            assert reused_fd == fd
            raise close_failure
        return real_close(fd)

    monkeypatch.setattr(codex_vision.os, "close", release_reuse_then_fail)
    try:
        with pytest.raises(CodexVisionError) as error:
            codex_vision._publish_cached_result(
                directory_fd,
                cache_key="a" * 64,
                kind="transcription",
                payload={"value": "bounded"},
                deadline=time.monotonic() + 2,
                cancel_event=None,
            )

        assert error.value.__context__ is close_failure
        assert reused_fd is not None
        os.fstat(reused_fd)
    finally:
        if reused_fd is not None:
            try:
                real_close(reused_fd)
            except OSError:
                pass
        real_close(directory_fd)


def test_codex_result_cache_single_file_limit_fails_closed(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        cache_limits=CacheLimits(
            max_codex_result_bytes=64,
            max_codex_total_bytes=4096,
        ),
    )

    with pytest.raises(CodexVisionError, match="result cache capacity exceeded"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert not list(executor.result_cache_root.glob("*.json"))


def test_codex_result_cache_total_limit_evicts_lru(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    second_image = page_image.with_name("second.png")
    third_image = page_image.with_name("third.png")
    second_image.write_bytes(_png(1200, 1600, b"second"))
    third_image.write_bytes(_png(1200, 1600, b"third"))
    second_image.chmod(0o400)
    third_image.chmod(0o400)
    seed = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    first = seed.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    second = seed.transcribe_page(second_image, page_number=1, width=1200, height=1600)
    first_path = seed.result_cache_root / f"{first.record['cache_key']}.json"
    second_path = seed.result_cache_root / f"{second.record['cache_key']}.json"
    first_size = first_path.stat().st_size
    second_size = second_path.stat().st_size
    now = time.time()
    os.utime(first_path, (now - 20, now - 20))
    os.utime(second_path, (now - 10, now - 10))
    total_limit = first_size + second_size + min(first_size, second_size) // 2
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        cache_limits=CacheLimits(
            max_codex_result_bytes=max(first_size, second_size) * 2,
            max_codex_total_bytes=total_limit,
        ),
    )

    cached = executor.transcribe_page(second_image, page_number=1, width=1200, height=1600)
    third = executor.transcribe_page(third_image, page_number=1, width=1200, height=1600)
    third_path = executor.result_cache_root / f"{third.record['cache_key']}.json"

    assert cached.record["cache_hit"] is True
    assert not first_path.exists()
    assert second_path.exists()
    assert third_path.exists()
    cached_bytes = sum(path.stat().st_size for path in executor.result_cache_root.glob("*.json"))
    assert cached_bytes <= total_limit


def test_codex_result_cache_fails_closed_when_only_evictable_entry_is_locked(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    seed = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    first = seed.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    first_path = seed.result_cache_root / f"{first.record['cache_key']}.json"
    first_size = first_path.stat().st_size
    lock_path = seed.result_cache_root / codex_vision_module._result_lock_name(
        first.record["cache_key"]
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys; "
                "fd=os.open(sys.argv[1], os.O_RDWR); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "print('ready', flush=True); sys.stdin.read(1)"
            ),
            os.fspath(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    try:
        changed_image = page_image.with_name("locked-capacity.png")
        changed_image.write_bytes(_png(1200, 1600, b"locked-capacity"))
        changed_image.chmod(0o400)
        executor = _executor(
            fake_codex,
            tmp_path,
            local_fake_codex_policy,
            cache_limits=CacheLimits(
                max_codex_result_bytes=first_size * 2,
                max_codex_total_bytes=first_size + first_size // 2,
            ),
        )
        with pytest.raises(CodexVisionError, match="result cache capacity exceeded"):
            executor.transcribe_page(changed_image, page_number=1, width=1200, height=1600)
    finally:
        holder.communicate(input="x", timeout=2)

    assert first_path.exists()
    assert list(executor.result_cache_root.glob("*.json")) == [first_path]


def test_codex_result_cache_scan_entry_count_is_bounded(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    cache_root = tmp_path / "jobs" / "result-cache"
    cache_root.mkdir(parents=True)
    cache_root.chmod(0o700)
    for index in range(4):
        junk = cache_root / f"junk-{index}"
        junk.write_bytes(b"x")
        junk.chmod(0o600)
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        cache_limits=CacheLimits(max_scan_entries=3),
    )

    with pytest.raises(CodexVisionError, match="result cache scan exceeded limit"):
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)


def test_codex_result_cache_quota_counts_every_directory_object_byte(tmp_path: Path) -> None:
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    cache_root = tmp_path / "result-cache"
    cache_root.mkdir()
    objects = {
        f"{'a' * 64}.json": b"r" * 11,
        f"{'a' * 64}.lock": b"l" * 13,
        ".quota-state": b"q" * 17,
        f".{'b' * 64}.1.2.tmp": b"p" * 19,
        ".cleanup-dead.tmp": b"c" * 23,
        ".evict-dead.tmp": b"e" * 29,
        "unexpected-object": b"u" * 31,
    }
    for name, payload in objects.items():
        path = cache_root / name
        path.write_bytes(payload)
        path.chmod(0o600)
    directory_fd = os.open(cache_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scanned = codex_vision_module._scan_result_cache(directory_fd, CacheLimits())
    finally:
        os.close(directory_fd)

    assert scanned.total_bytes == sum(map(len, objects.values()))
    assert scanned.total_entries == len(objects)


def test_codex_result_cache_entry_quota_fails_before_creating_publish_temp(
    tmp_path: Path,
) -> None:
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    cache_root = tmp_path / "result-cache"
    cache_root.mkdir()
    for index in range(2):
        path = cache_root / f"unknown-{index}"
        path.write_bytes(b"x")
        path.chmod(0o600)
    directory_fd = os.open(cache_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(CodexVisionError, match="result cache capacity exceeded"):
            codex_vision_module._reserve_result_cache_capacity(
                directory_fd,
                cache_key="a" * 64,
                incoming_bytes=1,
                limits=CacheLimits(max_codex_cache_entries=3),
                deadline=time.monotonic() + 1,
                cancel_event=None,
            )
    finally:
        os.close(directory_fd)

    assert not list(cache_root.glob("*.tmp"))


def test_codex_result_cache_lock_objects_are_bounded_across_unique_keys(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "result-cache"
    for index in range(256):
        cache_key = hashlib.sha256(f"cache-key-{index}".encode()).hexdigest()
        with codex_vision_module._locked_result_cache(
            cache_root,
            cache_key,
            deadline=time.monotonic() + 2,
            cancel_event=None,
        ):
            pass

    locks = list(cache_root.glob("*.lock"))
    assert 1 <= len(locks) <= 64


def test_codex_result_cache_waiter_keeps_same_lock_inode_without_unlink_split(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "result-cache"
    cache_key = "a" * 64
    acquired: list[tuple[int, int]] = []

    with codex_vision_module._locked_result_cache(
        cache_root,
        cache_key,
        deadline=time.monotonic() + 2,
        cancel_event=None,
    ):
        lock_path = cache_root / codex_vision_module._result_lock_name(cache_key)
        original = lock_path.stat()

        def wait_for_lock() -> None:
            with codex_vision_module._locked_result_cache(
                cache_root,
                cache_key,
                deadline=time.monotonic() + 2,
                cancel_event=None,
            ):
                current = lock_path.stat()
                acquired.append((current.st_dev, current.st_ino))

        waiter = threading.Thread(target=wait_for_lock)
        waiter.start()
        waiter.join(timeout=0.1)
        assert waiter.is_alive()
        assert waiter.daemon is False

    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert acquired == [(original.st_dev, original.st_ino)]
    current = lock_path.stat()
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)


def test_codex_result_cache_quota_scan_is_throttled(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    import parsing_core.workbench.ocr.codex_vision as codex_vision
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    real_scan = codex_vision._scan_result_cache
    scans = 0

    def count_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(codex_vision, "_scan_result_cache", count_scan)
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        cache_limits=CacheLimits(quota_maintenance_interval=16),
    )
    images = [page_image]
    for index in range(2):
        image = page_image.with_name(f"throttled-{index}.png")
        image.write_bytes(_png(1200, 1600, f"throttled-{index}".encode()))
        image.chmod(0o400)
        images.append(image)

    for image in images:
        executor.transcribe_page(image, page_number=1, width=1200, height=1600)

    assert scans == 1


def test_codex_result_cache_total_includes_and_evicts_stale_publish_temporary(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    seed = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    first = seed.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    first_path = seed.result_cache_root / f"{first.record['cache_key']}.json"
    first_size = first_path.stat().st_size
    stale_key = "d" * 64
    stale = seed.result_cache_root / f".{stale_key}.123.456.tmp"
    stale.write_bytes(b"x" * first_size)
    stale.chmod(0o600)
    now = time.time()
    os.utime(stale, (now - 30, now - 30))
    os.utime(first_path, (now - 10, now - 10))
    changed_image = page_image.with_name("stale-temp-capacity.png")
    changed_image.write_bytes(_png(1200, 1600, b"stale-temp-capacity"))
    changed_image.chmod(0o400)
    total_limit = first_size * 2 + 32
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        cache_limits=CacheLimits(
            max_codex_result_bytes=first_size * 2,
            max_codex_total_bytes=total_limit,
            quota_maintenance_interval=1,
        ),
    )

    executor.transcribe_page(
        changed_image,
        page_number=1,
        width=1200,
        height=1600,
    )

    assert not stale.exists()
    payload_bytes = sum(
        path.stat().st_size
        for path in executor.result_cache_root.iterdir()
        if path.is_file() and (path.suffix == ".json" or path.suffix == ".tmp")
    )
    assert payload_bytes <= total_limit


def test_codex_cache_directory_fsync_failure_reports_committed_state(tmp_path, monkeypatch):
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    cache_dir = tmp_path / "result-cache"
    cache_dir.mkdir()
    directory_fd = os.open(cache_dir, os.O_RDONLY | os.O_DIRECTORY)
    real_fsync = os.fsync
    durability_failure = OSError("directory durability failed")

    def fail_directory_fsync(fd):
        if fd == directory_fd:
            raise durability_failure
        return real_fsync(fd)

    monkeypatch.setattr(codex_vision.os, "fsync", fail_directory_fsync)
    try:
        with pytest.raises(codex_vision.CodexCacheCommitError) as error:
            codex_vision._publish_cached_result(
                directory_fd,
                cache_key="b" * 64,
                kind="transcription",
                payload={"value": "bounded"},
                deadline=time.monotonic() + 2,
                cancel_event=None,
            )

        assert error.value.committed is True
        assert error.value.durability_uncertain is True
        assert error.value.__cause__ is durability_failure
        assert (cache_dir / f"{'b' * 64}.json").is_file()
        assert not list(cache_dir.glob("*.tmp"))
    finally:
        os.close(directory_fd)


def test_codex_cache_committed_error_reaches_executor_caller(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    committed_error = codex_vision.CodexCacheCommitError(
        Path("committed.json"), durability_uncertain=True
    )

    def fail_after_commit(*_args, **_kwargs):
        raise committed_error

    monkeypatch.setattr(codex_vision, "_publish_cached_result", fail_after_commit)

    with pytest.raises(codex_vision.CodexCacheCommitError) as error:
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert error.value is committed_error


def test_codex_cache_cleanup_failure_does_not_mask_publish_error(tmp_path, monkeypatch):
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    cache_dir = tmp_path / "result-cache"
    cache_dir.mkdir()
    directory_fd = os.open(cache_dir, os.O_RDONLY | os.O_DIRECTORY)
    publish_failure = OSError("publish failed")
    cleanup_failure = OSError("cleanup failed")
    real_unlink = os.unlink

    def fail_publish(*_args, **_kwargs):
        raise publish_failure

    def fail_cleanup(*_args, **_kwargs):
        raise cleanup_failure

    monkeypatch.setattr(codex_vision.os, "replace", fail_publish)
    monkeypatch.setattr(codex_vision.os, "unlink", fail_cleanup)
    try:
        with pytest.raises(CodexVisionError) as error:
            codex_vision._publish_cached_result(
                directory_fd,
                cache_key="c" * 64,
                kind="transcription",
                payload={"value": "bounded"},
                deadline=time.monotonic() + 2,
                cancel_event=None,
            )

        assert error.value.__context__ is publish_failure
        assert cleanup_failure not in (error.value.__context__, error.value.__cause__)
    finally:
        monkeypatch.undo()
        for path in cache_dir.iterdir():
            real_unlink(path)
        os.close(directory_fd)


def test_codex_result_cache_never_reuses_across_changed_image_or_evidence(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    changed_image = page_image.with_name("changed.png")
    changed_image.write_bytes(_png(1200, 1600, b"different-image"))
    changed_image.chmod(0o400)
    executor.transcribe_page(changed_image, page_number=1, width=1200, height=1600)
    assert len(_exec_starts(fake_codex, "page-transcription.json")) == 2

    executor.adjudicate_page(
        page_image,
        page_number=1,
        width=1200,
        height=1600,
        codex_observation={"text": "same"},
        apple_observation=_sample_apple(),
        diff={"conflicts": [{"id": "one"}]},
    )
    executor.adjudicate_page(
        page_image,
        page_number=1,
        width=1200,
        height=1600,
        codex_observation={"text": "same"},
        apple_observation=_sample_apple(),
        diff={"conflicts": [{"id": "two"}]},
    )
    assert len(_exec_starts(fake_codex, "page-adjudication.json")) == 2


def test_codex_result_cache_never_reuses_across_changed_executable_or_schema(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    replacement = _write_fake_codex(tmp_path / "replacement_codex.py")
    replacement.write_text(
        replacement.read_text(encoding="utf-8") + "\n# distinct executable identity\n",
        encoding="utf-8",
    )
    replacement.chmod(0o700)
    local_fake_codex_policy.authorize(replacement)
    replacement_executor = CodexVisionExecutor(
        codex_path=replacement,
        temp_root=tmp_path / "jobs",
        trusted_image_root=tmp_path / "cache",
        timeout=2,
    )
    replacement_executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    assert len(_exec_starts(replacement, "page-transcription.json")) == 1

    import parsing_core.workbench.ocr.codex_vision as codex_module

    changed_schema_dir = tmp_path / "changed-schemas"
    shutil.copytree(codex_module._SCHEMA_DIR, changed_schema_dir)
    transcription_schema = changed_schema_dir / "page-transcription.json"
    transcription_schema.write_text(
        transcription_schema.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module, "_SCHEMA_DIR", changed_schema_dir)
    executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    assert len(_exec_starts(fake_codex, "page-transcription.json")) == 2


def test_codex_cache_reuses_output_after_crash_before_batch_state_persistence(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    class SimulatedProcessCrash(BaseException):
        pass

    cancel = threading.Event()
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    orchestrator, pdf = _codex_orchestrator(tmp_path, executor, page_image, cancel)
    persist = orchestrator._persist

    def crash_after_codex_output(state):
        page_state = state["pages"]["1"]
        if "codex" in page_state and "decision" not in page_state:
            raise SimulatedProcessCrash
        persist(state)

    monkeypatch.setattr(orchestrator, "_persist", crash_after_codex_output)
    with pytest.raises(SimulatedProcessCrash):
        orchestrator.run_batch(pdf, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0)

    resumed, _pdf = _codex_orchestrator(tmp_path, executor, page_image, cancel)
    resumed.run_batch(pdf, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0)

    assert len(_exec_starts(fake_codex, "page-transcription.json")) == 1


@pytest.mark.parametrize("attack", ["symlink", "oversize"])
def test_codex_result_cache_rejects_untrusted_entries_without_reuse(
    tmp_path, fake_codex, page_image, attack, local_fake_codex_policy
):
    import parsing_core.workbench.ocr.codex_vision as codex_module

    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    first = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    cache_path = executor.result_cache_root / f"{first.record['cache_key']}.json"
    cache_path.unlink()
    if attack == "symlink":
        outside = tmp_path / "outside-cache.json"
        outside.write_text(json.dumps({"payload": {"forged": True}}), encoding="utf-8")
        cache_path.symlink_to(outside)
    else:
        cache_path.write_bytes(b"x" * (codex_module._MAX_RESULT_CACHE_BYTES + 1))
        cache_path.chmod(0o600)

    second = executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)

    assert second.payload == first.payload
    assert second.record["cache_hit"] is False
    assert len(_exec_starts(fake_codex, "page-transcription.json")) == 2
    assert cache_path.is_file() and not cache_path.is_symlink()


def test_codex_result_cache_key_binds_crop_bytes(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    crop_one = page_image.with_name("crop-one.png")
    crop_two = page_image.with_name("crop-two.png")
    crop_one.write_bytes(_png(100, 100, b"crop-one"))
    crop_two.write_bytes(_png(100, 100, b"crop-two"))
    crop_one.chmod(0o400)
    crop_two.chmod(0o400)
    arguments = {
        "page_number": 1,
        "width": 1200,
        "height": 1600,
        "codex_observation": {"text": "same"},
        "apple_observation": _sample_apple(),
        "diff": _sample_diff(),
    }

    executor.adjudicate_page(page_image, crop_images=[crop_one], **arguments)
    executor.adjudicate_page(page_image, crop_images=[crop_two], **arguments)

    assert len(_exec_starts(fake_codex, "page-adjudication.json")) == 2


def test_image_root_hash_and_format_are_enforced(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
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


def test_verified_image_copy_preserves_all_source_bytes(
    tmp_path, fake_codex, page_image, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    prompt_event = [event for event in _events(fake_codex) if event["event"] == "prompt"][-1]
    assert prompt_event["image_size"] == page_image.stat().st_size
    assert prompt_event["image_sha256"] == hashlib.sha256(page_image.read_bytes()).hexdigest()


def test_jsonschema_validator_is_used_and_validator_errors_are_sanitized(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
    import parsing_core.workbench.ocr.codex_vision as codex_vision

    class BrokenValidator:
        def iter_errors(self, value):
            raise TypeError("secret /Users/laoer/Documents/PDF2MD")

    monkeypatch.setattr(codex_vision, "_SCHEMA_VALIDATORS", {"transcription": BrokenValidator()})
    with pytest.raises(CodexVisionError, match="codex cli returned invalid schema") as error:
        executor.transcribe_page(page_image, page_number=1, width=1200, height=1600)
    assert "secret /Users/laoer/Documents/PDF2MD" not in str(error.value)


def test_non_dict_page_and_path_like_observation_are_wrapped(
    tmp_path, fake_codex, page_image, monkeypatch, local_fake_codex_policy
):
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy)
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


def test_real_codex_cli_resolves_native_and_cancels_before_network(tmp_path, page_image):
    wrapper = shutil.which("codex")
    if not wrapper:
        pytest.skip("real codex cli is not installed")
    native = resolve_codex_path(wrapper)
    executor = CodexVisionExecutor(
        codex_path=native,
        temp_root=tmp_path / "jobs",
        trusted_image_root=tmp_path / "cache",
        timeout=5,
    )
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(CodexVisionError, match="codex cli cancelled"):
        executor.transcribe_page(
            page_image,
            page_number=1,
            width=1200,
            height=1600,
            cancel_event=cancel,
        )

    assert Path(native).name == "codex"
    assert executor.codex_path != Path(wrapper)


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


def _waitpid_with_kill(pid: int, *, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed, status = os.waitpid(pid, os.WNOHANG)
        if observed == pid:
            return status
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    _observed, status = os.waitpid(pid, 0)
    pytest.fail(f"forked cache child exceeded hard deadline: status={status}")


@pytest.mark.parametrize(
    ("limit_overrides", "incoming_bytes"),
    [
        ({"max_codex_total_bytes": 100, "max_codex_cache_entries": 128}, 60),
        ({"max_codex_total_bytes": 4096, "max_codex_cache_entries": 2}, 1),
    ],
)
def test_codex_result_cache_parallel_reservations_preserve_bytes_and_entries(
    tmp_path: Path,
    limit_overrides: dict[str, int],
    incoming_bytes: int,
) -> None:
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    root = tmp_path / "result-cache"
    start = threading.Barrier(3)
    release = threading.Event()
    outcomes: list[object] = []
    outcomes_guard = threading.Lock()
    limits = CacheLimits(**limit_overrides)
    with codex_vision_module._locked_result_cache(
        root,
        "f" * 64,
        deadline=time.monotonic() + 2,
        cancel_event=None,
    ):
        pass

    def reserve(cache_key: str) -> None:
        try:
            with codex_vision_module._locked_result_cache(
                root,
                cache_key,
                deadline=time.monotonic() + 3,
                cancel_event=None,
            ) as directory_fd:
                start.wait(timeout=2)
                try:
                    reservation = codex_vision_module._reserve_result_cache_capacity(
                        directory_fd,
                        cache_key=cache_key,
                        incoming_bytes=incoming_bytes,
                        limits=limits,
                        deadline=time.monotonic() + 2,
                        cancel_event=None,
                    )
                except CodexVisionError as error:
                    with outcomes_guard:
                        outcomes.append(error)
                    return
                with outcomes_guard:
                    outcomes.append(reservation)
                assert release.wait(timeout=2)
                codex_vision_module._release_result_cache_capacity(
                    directory_fd,
                    reservation,
                    limits=limits,
                    deadline=time.monotonic() + 2,
                    cancel_event=None,
                )
        except BaseException as error:
            with outcomes_guard:
                outcomes.append(error)

    workers = [
        threading.Thread(target=reserve, args=(hashlib.sha256(str(index).encode()).hexdigest(),))
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    start.wait(timeout=2)
    deadline = time.monotonic() + 2
    try:
        while len(outcomes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(outcomes) == 2
        assert (
            sum(isinstance(item, codex_vision_module._ResultCacheReservation) for item in outcomes)
            == 1
        )
        assert sum(isinstance(item, CodexVisionError) for item in outcomes) == 1
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()


def test_codex_result_cache_full_directory_scan_is_not_per_write(
    tmp_path: Path,
    fake_codex: Path,
    page_image: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_fake_codex_policy,
) -> None:
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    real_inspect = codex_vision_module._inspect_result_cache
    inspections = 0

    def count_inspection(*args, **kwargs):
        nonlocal inspections
        inspections += 1
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(codex_vision_module, "_inspect_result_cache", count_inspection)
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        cache_limits=CacheLimits(quota_maintenance_interval=1),
    )
    images = []
    for index in range(8):
        image = page_image.with_name(f"linear-scan-{index}.png")
        image.write_bytes(_png(1200, 1600, f"linear-{index}".encode()))
        image.chmod(0o400)
        images.append(image)

    for image in images:
        executor.transcribe_page(image, page_number=1, width=1200, height=1600)

    assert inspections == 1


def test_codex_result_cache_stale_reservation_is_recovered_by_slot_identity(
    tmp_path: Path,
) -> None:
    from parsing_core.workbench.ocr.page_cache import CacheLimits

    root = tmp_path / "result-cache"
    limits = CacheLimits(max_codex_total_bytes=100, max_codex_cache_entries=128)
    first_key = "a" * 64
    second_key = "b" * 64
    with codex_vision_module._locked_result_cache(
        root,
        first_key,
        deadline=time.monotonic() + 2,
        cancel_event=None,
    ) as directory_fd:
        stale = codex_vision_module._reserve_result_cache_capacity(
            directory_fd,
            cache_key=first_key,
            incoming_bytes=60,
            limits=limits,
            deadline=time.monotonic() + 1,
            cancel_event=None,
        )

    with codex_vision_module._locked_result_cache(
        root,
        second_key,
        deadline=time.monotonic() + 2,
        cancel_event=None,
    ) as directory_fd:
        replacement = codex_vision_module._reserve_result_cache_capacity(
            directory_fd,
            cache_key=second_key,
            incoming_bytes=60,
            limits=limits,
            deadline=time.monotonic() + 1,
            cancel_event=None,
        )
        try:
            with codex_vision_module._locked_result_quota(
                directory_fd,
                deadline=time.monotonic() + 1,
                cancel_event=None,
            ) as state_fd:
                state = codex_vision_module._read_result_quota_state(state_fd)
            assert state is not None
            assert state.reserved_bytes == 60
            assert state.reserved_entries == 1
            assert replacement.reservation_id != stale.reservation_id
        finally:
            codex_vision_module._release_result_cache_capacity(
                directory_fd,
                replacement,
                limits=limits,
                deadline=time.monotonic() + 1,
                cancel_event=None,
            )


@pytest.mark.parametrize(
    ("limit_overrides", "amount", "entries"),
    [
        ({"max_source_total_bytes": 100, "max_page_cache_bytes": 100}, 60, 1),
        ({"max_page_cache_entries": 1}, 1, 1),
    ],
)
def test_page_cache_parallel_reservations_preserve_bytes_and_entries(
    tmp_path: Path,
    limit_overrides: dict[str, int],
    amount: int,
    entries: int,
) -> None:
    import parsing_core.workbench.ocr.page_cache as page_cache_module

    cache = page_cache_module.PageCache(
        tmp_path / "page-cache",
        limits=page_cache_module.CacheLimits(**limit_overrides),
    )
    start = threading.Barrier(3)
    release = threading.Event()
    outcomes: list[object] = []
    outcomes_guard = threading.Lock()

    def reserve() -> None:
        try:
            start.wait(timeout=2)
            reservation = cache._reserve_capacity("source", amount, entries=entries)
        except BaseException as error:
            with outcomes_guard:
                outcomes.append(error)
            return
        with outcomes_guard:
            outcomes.append(reservation)
        assert release.wait(timeout=2)
        cache._release_capacity(reservation)

    workers = [threading.Thread(target=reserve) for _index in range(2)]
    for worker in workers:
        worker.start()
    start.wait(timeout=2)
    deadline = time.monotonic() + 2
    try:
        while len(outcomes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(outcomes) == 2
        assert sum(isinstance(item, page_cache_module._QuotaReservation) for item in outcomes) == 1
        assert sum(isinstance(item, page_cache_module.PageCacheError) for item in outcomes) == 1
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()


def test_page_cache_reservations_do_not_trigger_periodic_full_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import parsing_core.workbench.ocr.page_cache as page_cache_module

    cache = page_cache_module.PageCache(
        tmp_path / "page-cache",
        limits=page_cache_module.CacheLimits(quota_maintenance_interval=1),
    )
    real_scandir = page_cache_module.os.scandir
    scans = 0

    def count_scandir(path):
        nonlocal scans
        scans += 1
        return real_scandir(path)

    monkeypatch.setattr(page_cache_module.os, "scandir", count_scandir)
    for _index in range(16):
        reservation = cache._reserve_capacity("source", 1)
        cache._release_capacity(reservation)

    assert scans == 0


def test_page_cache_stale_reservation_recovers_without_releasing_live_owner(tmp_path: Path) -> None:
    import parsing_core.workbench.ocr.page_cache as page_cache_module

    cache = page_cache_module.PageCache(
        tmp_path / "page-cache",
        limits=page_cache_module.CacheLimits(
            max_source_total_bytes=100,
            max_page_cache_bytes=100,
        ),
    )
    stale = cache._reserve_capacity("source", 60)
    os.close(stale.lock_fd)
    stale.lock_fd = -1

    replacement = cache._reserve_capacity("source", 60)
    try:
        with cache.lock(page_cache_module._QUOTA_LOCK_KEY):
            usage = cache._read_usage_locked()
        assert usage.reserved.source == 60
        assert len(usage.reservations) == 1
        assert usage.reservations[0].reservation_id == replacement.reservation_id
    finally:
        cache._release_capacity(replacement)


def test_page_cache_uses_fixed_lock_slots_and_serializes_collisions(tmp_path: Path) -> None:
    import parsing_core.workbench.ocr.page_cache as page_cache_module

    cache = page_cache_module.PageCache(tmp_path / "page-cache")
    for index in range(256):
        with cache.lock(hashlib.sha256(f"key-{index}".encode()).hexdigest()):
            pass

    lock_files = list(cache.locks_dir.iterdir())
    assert len(lock_files) == page_cache_module._PAGE_CACHE_LOCK_SLOTS
    assert all(path.stat().st_size == 0 for path in lock_files)

    first = "0" * 64
    target_slot = page_cache_module._entry_lock_slot(first)
    second = next(
        hashlib.sha256(f"collision-{index}".encode()).hexdigest()
        for index in range(10_000)
        if page_cache_module._entry_lock_slot(
            hashlib.sha256(f"collision-{index}".encode()).hexdigest()
        )
        == target_slot
    )
    with cache.lock(first):
        with cache.lock(second, deadline=time.monotonic() + 0.25):
            pass
    acquired = threading.Event()

    def waiter() -> None:
        with cache.lock(second, deadline=time.monotonic() + 2):
            acquired.set()

    with cache.lock(first):
        worker = threading.Thread(target=waiter)
        worker.start()
        assert not acquired.wait(timeout=0.1)
    worker.join(timeout=2)
    assert acquired.is_set()
    assert not worker.is_alive()


def test_page_cache_fixed_slot_collision_serializes_across_processes(tmp_path: Path) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    import parsing_core.workbench.ocr.page_cache as page_cache_module

    root = tmp_path / "page-cache"
    cache = page_cache_module.PageCache(root)
    first = "1" * 64
    target_slot = page_cache_module._entry_lock_slot(first)
    second = next(
        hashlib.sha256(f"process-collision-{index}".encode()).hexdigest()
        for index in range(10_000)
        if page_cache_module._entry_lock_slot(
            hashlib.sha256(f"process-collision-{index}".encode()).hexdigest()
        )
        == target_slot
    )

    with cache.lock(first):
        pid = os.fork()
        if pid == 0:
            try:
                child_cache = page_cache_module.PageCache(root)
                with child_cache.lock(second, deadline=time.monotonic() + 0.25):
                    os._exit(7)
            except TimeoutError:
                os._exit(0)
            except BaseException:
                os._exit(8)
        status = _waitpid_with_kill(pid)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0


def test_inherited_page_cache_rejects_after_fork_without_inherited_lock_deadlock(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    import parsing_core.workbench.ocr.page_cache as page_cache_module

    cache = page_cache_module.PageCache(tmp_path / "page-cache")
    page_cache_module._THREAD_LOCKS_GUARD.acquire()
    cache._active_jobs_guard.acquire()
    try:
        pid = os.fork()
        if pid == 0:
            try:
                with cache.lock("child", deadline=time.monotonic() + 0.25):
                    os._exit(7)
            except page_cache_module.PageCacheError:
                os._exit(0)
            except BaseException:
                os._exit(8)
        status = _waitpid_with_kill(pid)
    finally:
        cache._active_jobs_guard.release()
        page_cache_module._THREAD_LOCKS_GUARD.release()

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0


def test_orchestrator_cancel_waits_for_codex_root_child_and_engine_thread(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "cancel_codex.py", mode="spawn_child_ignore_term")
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy, timeout=5)
    cancel = threading.Event()
    orchestrator, pdf = _codex_orchestrator(tmp_path, executor, page_image, cancel)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            orchestrator.run_batch(
                pdf, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0, timeout=4
            )
        ),
        name="codex-orchestrator-cancel-test",
    )
    worker.start()
    child = _wait_for_event(fake_codex, "child_start")
    root = _wait_for_event(fake_codex, "start")
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
        assert not root_alive, f"Codex root PID {root['pid']} survived CANCELLED"
        assert not child_alive, f"Codex child PID {child['pid']} survived CANCELLED"
        assert leaked_engine_threads == []

        _set_fake_codex_mode(fake_codex, "success")
        assert (
            executor.transcribe_page(page_image, page_number=1, width=1200, height=1600).payload[
                "blocks"
            ][0]["text"]
            == "visible text only"
        )
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)
        worker.join(timeout=1)


def test_orchestrator_deadline_waits_for_codex_root_child_and_engine_thread(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(tmp_path / "deadline_codex.py", mode="spawn_child_ignore_term")
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy, timeout=5)
    orchestrator, pdf = _codex_orchestrator(tmp_path, executor, page_image, threading.Event())

    result = orchestrator.run_batch(
        pdf, pages=[1], dpi=300, languages=["zh-Hans"], sample_rate=0, timeout=0.5
    )
    child = _wait_for_event(fake_codex, "child_start")
    root = _wait_for_event(fake_codex, "start")
    leaked_engine_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.name == "ocr-engine-call" and thread.is_alive()
    ]
    root_alive = _pid_alive(root["pid"])
    child_alive = _pid_alive(child["pid"])
    try:
        assert result.status is BatchStatus.FAILED
        assert result.error == "ocr_timeout"
        assert result.pages[1].error == "ocr_timeout"
        assert not root_alive, f"Codex root PID {root['pid']} survived timeout"
        assert not child_alive, f"Codex child PID {child['pid']} survived timeout"
        assert leaked_engine_threads == []
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)


def test_codex_adjudication_cancel_kills_root_and_child(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(
        tmp_path / "adjudicate_cancel_codex.py", mode="spawn_child_ignore_term"
    )
    executor = _executor(fake_codex, tmp_path, local_fake_codex_policy, timeout=5)
    cancel = threading.Event()
    outcomes = []

    def adjudicate():
        try:
            executor.adjudicate_page(
                page_image,
                page_number=1,
                width=1200,
                height=1600,
                codex_observation=_sample_transcription(),
                apple_observation=_sample_apple(),
                diff=_sample_diff(),
                deadline=time.monotonic() + 4,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=adjudicate, name="codex-adjudicate-cancel-test")
    worker.start()
    child = _wait_for_event(fake_codex, "child_start")
    root = _wait_for_event(fake_codex, "start")
    cancel.set()
    worker.join(timeout=3)
    root_alive = _pid_alive(root["pid"])
    child_alive = _pid_alive(child["pid"])
    try:
        assert not worker.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], CodexVisionError)
        assert "cancelled" in str(outcomes[0])
        assert not root_alive, f"Codex root PID {root['pid']} survived adjudication cancel"
        assert not child_alive, f"Codex child PID {child['pid']} survived adjudication cancel"
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)


def test_codex_version_probe_cancel_kills_root_and_child(tmp_path, local_fake_codex_policy):
    fake_codex = _write_fake_codex(
        tmp_path / "version_cancel_codex.py", mode="version_spawn_child_timeout"
    )
    local_fake_codex_policy.authorize(fake_codex)
    (tmp_path / "cache").mkdir()
    cancel = threading.Event()
    outcomes = []

    def construct():
        try:
            CodexVisionExecutor(
                codex_path=fake_codex,
                temp_root=tmp_path / "jobs",
                trusted_image_root=tmp_path / "cache",
                timeout=5,
                deadline=time.monotonic() + 4,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=construct, name="codex-version-cancel-test")
    worker.start()
    child = _wait_for_event(fake_codex, "version_child_ready")
    root = _wait_for_event(fake_codex, "version_start")
    cancel.set()
    worker.join(timeout=3)
    root_alive = _pid_alive(root["pid"])
    child_alive = _pid_alive(child["child_pid"])
    try:
        assert not worker.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], CodexVisionError)
        assert not root_alive, f"Codex version root PID {root['pid']} survived cancel"
        assert not child_alive, f"Codex version child PID {child['child_pid']} survived cancel"
    finally:
        if root_alive or child_alive:
            _kill_fixture_group(root)


def test_codex_version_probe_uses_remaining_absolute_deadline(tmp_path, local_fake_codex_policy):
    fake_codex = _write_fake_codex(
        tmp_path / "version_deadline_codex.py", mode="version_spawn_child_timeout"
    )
    local_fake_codex_policy.authorize(fake_codex)
    (tmp_path / "cache").mkdir()
    began = time.monotonic()

    with pytest.raises(CodexVisionError, match="not available"):
        CodexVisionExecutor(
            codex_path=fake_codex,
            temp_root=tmp_path / "jobs",
            trusted_image_root=tmp_path / "cache",
            timeout=5,
            deadline=time.monotonic() + 0.3,
            cancel_event=threading.Event(),
        )

    elapsed = time.monotonic() - began
    events = _events(fake_codex)
    root_pid = next(
        (event["pid"] for event in events if event["event"] == "version_start"),
        None,
    )
    child_pid = next(
        (event["child_pid"] for event in events if event["event"] == "version_child_spawned"),
        None,
    )
    assert elapsed < 2
    if root_pid is not None:
        assert _wait_until_gone(root_pid)
    if child_pid is not None:
        assert _wait_until_gone(child_pid)


def test_codex_cancel_is_polled_after_helper_closes_all_pipes(
    tmp_path, page_image, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(
        tmp_path / "closed_pipes_codex.py", mode="close_pipes_ignore_term"
    )
    observed_tasks = []
    executor = _executor(
        fake_codex,
        tmp_path,
        local_fake_codex_policy,
        timeout=5,
        task_observer=observed_tasks.append,
    )
    cancel = threading.Event()
    outcomes = []

    def transcribe():
        try:
            executor.transcribe_page(
                page_image,
                page_number=1,
                width=1200,
                height=1600,
                deadline=time.monotonic() + 4,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=transcribe, name="codex-closed-pipes-cancel-test")
    worker.start()
    _wait_for_event(fake_codex, "pipes_closed")
    root = _wait_for_event(fake_codex, "start")
    cancel.set()
    worker.join(timeout=1)
    worker_alive = worker.is_alive()
    root_alive = _pid_alive(root["pid"])
    try:
        assert not worker_alive
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], CodexVisionError)
        assert "cancelled" in str(outcomes[0])
        assert not root_alive
        _assert_observed_tasks_clean(observed_tasks, tmp_path)
    finally:
        if root_alive:
            _kill_fixture_group(root)
        worker.join(timeout=1)


def test_codex_version_cancel_is_polled_after_helper_closes_all_pipes(
    tmp_path, local_fake_codex_policy
):
    fake_codex = _write_fake_codex(
        tmp_path / "version_closed_pipes_codex.py",
        mode="version_close_pipes_ignore_term",
    )
    local_fake_codex_policy.authorize(fake_codex)
    (tmp_path / "cache").mkdir()
    cancel = threading.Event()
    outcomes = []

    def construct():
        try:
            CodexVisionExecutor(
                codex_path=fake_codex,
                temp_root=tmp_path / "jobs",
                trusted_image_root=tmp_path / "cache",
                timeout=5,
                deadline=time.monotonic() + 4,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=construct, name="codex-version-closed-pipes-cancel-test")
    worker.start()
    _wait_for_event(fake_codex, "version_pipes_closed")
    root = _wait_for_event(fake_codex, "version_start")
    cancel.set()
    worker.join(timeout=1)
    worker_alive = worker.is_alive()
    root_alive = _pid_alive(root["pid"])
    try:
        assert not worker_alive
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], CodexVisionError)
        assert not root_alive
    finally:
        if root_alive:
            _kill_fixture_group(root)
        worker.join(timeout=1)
