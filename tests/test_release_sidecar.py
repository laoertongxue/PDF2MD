import http.client
import io
import json
import os
import plistlib
import re
import runpy
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import parsing_core.serving.lifecycle as lifecycle
from parsing_core.serving.lifecycle import (
    DarwinProcessTable,
    ScanResult,
)
from parsing_core.serving.lifecycle import (
    ProcessIdentity as LifecycleProcessIdentity,
)

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/check-release-sidecar.sh"
RELEASE_TEST = ROOT / "scripts/test-release-sidecar.sh"
RELEASE_HARNESS = ROOT / "scripts/release_sidecar_harness.py"
SIDECAR_RUNTIME = ROOT / "parsing-core-app/scripts/sidecar_runtime.py"


def _launcher_template() -> str:
    source = SIDECAR_RUNTIME.read_text(encoding="utf-8")
    match = re.search(r"LAUNCHER = r(?P<quote>'''|\"\"\")(?P<body>.*?)(?P=quote)", source, re.S)
    assert match is not None
    return match.group("body").rstrip("\n") + "\n"


def _fake_bundle(tmp_path: Path, source: str) -> Path:
    app = tmp_path / "PDF2MD.app"
    contents = app / "Contents"
    launcher = contents / "MacOS/python3"
    runtime_bin = contents / "Resources/python-runtime/bin"
    runtime_python = runtime_bin / "python3.12"
    launcher.parent.mkdir(parents=True)
    runtime_python.parent.mkdir(parents=True)
    (contents / "Resources/src/parsing_core").mkdir(parents=True)
    launcher.write_text("#!/bin/bash\n", encoding="utf-8")
    trusted_executable = shutil.which("true")
    assert trusted_executable is not None
    shutil.copyfile(trusted_executable, runtime_python)
    (runtime_bin / "python").symlink_to("python3.12")
    (runtime_bin / "python3").symlink_to("python3.12")
    (contents / "Resources/src/parsing_core/module.py").write_text(source, encoding="utf-8")
    launcher.chmod(0o755)
    runtime_python.chmod(0o755)

    return app


def _run(app: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHECK), str(app)],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def test_release_scan_ignores_business_regex_but_rejects_real_user_path(tmp_path):
    core = runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))
    inspect_patterns = core["_inspect_patterns"]
    gate_error = core["GateError"]

    inspect_patterns(b'PATTERN = r"/Users/[^\\s]+"\n')
    with pytest.raises(gate_error) as captured:
        inspect_patterns(b'SOURCE = "/Users/laoer/Documents/PDF2MD/book.pdf"\n')
    assert captured.value.code == "PDF2MD_BUNDLE_E_DEVELOPMENT_PATH"


def test_release_scan_only_allows_paths_in_pinned_third_party_runtime():
    core = runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))
    inspect_patterns = core["_inspect_patterns"]
    stream_inspector = core["StreamInspector"]
    gate_error = core["GateError"]
    allows_upstream_paths = core["_allows_upstream_build_paths"]

    cargo_path = b"/Users/runner/.cargo/registry/src/index/crates/pyo3/src/lib.rs"
    workflow_path = b"/Users/runner/work/onnx/onnx/runtime/session.cc"
    python_build_path = b"/private/var/folders/ab/cd/T/build/Python-3.12.13/Modules/main.c"
    runtime_path = b"/tmp/perf-%jd.map"
    local_path = b"/Users/laoer/Documents/PDF2MD/source.py"
    for content in (
        cargo_path,
        workflow_path,
        python_build_path,
        runtime_path,
        local_path,
    ):
        inspect_patterns(content, allow_upstream_build_paths=True)
        for split_at in range(1, len(content)):
            inspector = stream_inspector(allow_upstream_build_paths=True)
            inspector.feed(content[:split_at])
            inspector.feed(content[split_at:])

    assert allows_upstream_paths(
        "Resources/python-runtime/lib/python3.12/site-packages/native/module.so"
    )
    assert allows_upstream_paths(
        "Resources/python-runtime/lib/python3.12/site-packages/native/module.dylib"
    )
    assert allows_upstream_paths("Resources/python-runtime/bin/python")
    assert allows_upstream_paths("Resources/python-runtime/bin/python3.12")
    assert allows_upstream_paths("Resources/python-runtime/lib/libpython3.12.dylib")
    assert allows_upstream_paths("Resources/python-runtime/lib/python3.12/lib-dynload/native.so")
    assert allows_upstream_paths("Resources/python-runtime/lib/python3.12/os.py")
    assert not allows_upstream_paths("Resources/src/parsing_core/native.so")
    assert not allows_upstream_paths(
        "Resources/python-runtime/lib/python3.12/site-packages/parsing_core/module.py"
    )

    for content in (
        cargo_path,
        workflow_path,
        b"/Users/runner/Documents/PDF2MD/source.py",
        local_path,
    ):
        with pytest.raises(gate_error) as captured:
            inspect_patterns(content)
        assert captured.value.code == "PDF2MD_BUNDLE_E_DEVELOPMENT_PATH"


def test_release_scan_still_rejects_credentials_in_pinned_third_party_runtime():
    core = runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))
    inspect_patterns = core["_inspect_patterns"]
    stream_inspector = core["StreamInspector"]
    gate_error = core["GateError"]
    credential = b"sk-" + b"Ab9_" * 8

    with pytest.raises(gate_error) as captured:
        inspect_patterns(credential, allow_upstream_build_paths=True)
    assert captured.value.code == "PDF2MD_BUNDLE_E_CREDENTIAL"

    for split_at in range(1, len(credential)):
        inspector = stream_inspector(allow_upstream_build_paths=True)
        with pytest.raises(gate_error) as streamed:
            inspector.feed(credential[:split_at])
            inspector.feed(credential[split_at:])
        assert streamed.value.code == "PDF2MD_BUNDLE_E_CREDENTIAL"


def test_release_scan_rejects_unpinned_runtime_link(tmp_path):
    app = _fake_bundle(tmp_path, "SOURCE = 'safe'\n")
    runtime_python = app / "Contents/Resources/python-runtime/bin/python3"
    runtime_python.unlink()
    runtime_python.symlink_to("python")

    result = _run(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_RUNTIME_LINK"


def test_release_and_dmg_verifiers_use_clean_direct_gate_invocation():
    release_source = RELEASE_TEST.read_text(encoding="utf-8")
    dmg_source = (ROOT / "scripts/verify-release-dmg.sh").read_text(encoding="utf-8")

    for source in (release_source, dmg_source):
        assert source.startswith("#!/usr/bin/env -S -i ")
        assert "HOME=/var/empty" in source
        assert "TMPDIR=/tmp" in source
    assert '"$script_dir/check-release-sidecar.sh" "$app"' in release_source
    assert release_source.count('"$script_dir/check-release-sidecar.sh" "$app"') == 2
    assert "bash " not in release_source
    assert '"$check_sidecar_bin" --dmg-volume "$mountpoint"' in dmg_source
    assert '"$check_sidecar_bin" "$mounted_app"' in dmg_source
    assert '"$test_sidecar_bin" "$mounted_app"' in dmg_source
    assert 'bash "$check_sidecar_bin"' not in dmg_source
    assert 'bash "$test_sidecar_bin"' not in dmg_source


@pytest.mark.parametrize(
    ("script", "arguments", "expected_error"),
    [
        (RELEASE_TEST, ("missing.app",), "PDF2MD_RELEASE_TEST_E_REQUIRED_PATH"),
        (
            ROOT / "scripts/verify-release-dmg.sh",
            ("missing.dmg", "0.1.2"),
            "PDF2MD_DMG_E_REQUIRED_PATH",
        ),
    ],
)
def test_release_verifier_shebang_ignores_bash_env_exit_zero(
    tmp_path: Path,
    script: Path,
    arguments: tuple[str, ...],
    expected_error: str,
):
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("exit 0\n", encoding="utf-8")

    result = subprocess.run(
        [str(script), *arguments],
        cwd=tmp_path,
        env=os.environ | {"BASH_ENV": str(bash_env)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr.strip() == expected_error


def test_forced_hostile_bash_is_outside_the_trusted_invocation_boundary(tmp_path: Path):
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("exec() { return 0; }\n", encoding="utf-8")

    forced = subprocess.run(
        ["/bin/bash", str(CHECK), str(tmp_path / "missing.app")],
        cwd=tmp_path,
        env=os.environ | {"BASH_ENV": str(bash_env)},
        text=True,
        capture_output=True,
        check=False,
    )
    direct = subprocess.run(
        [str(CHECK), str(tmp_path / "missing.app")],
        cwd=tmp_path,
        env=os.environ | {"BASH_ENV": str(bash_env)},
        text=True,
        capture_output=True,
        check=False,
    )

    # A caller that explicitly chooses a hostile parent interpreter has already
    # crossed the trust boundary before the script starts. Production callers
    # below are required to execute each gate directly so its clean shebang runs.
    assert forced.returncode == 0
    assert direct.returncode != 0
    production_sources = {
        "sidecar cold-start gate": RELEASE_TEST.read_text(encoding="utf-8"),
        "DMG gate": (ROOT / "scripts/verify-release-dmg.sh").read_text(encoding="utf-8"),
        "Release workflow": (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
    }
    for name, source in production_sources.items():
        assert "/bin/bash scripts/check-release-sidecar.sh" not in source, name
        assert "bash ./scripts/check-release-sidecar.sh" not in source, name
        assert 'bash "$check_sidecar_bin"' not in source, name


def _executable_bundle(tmp_path: Path) -> Path:
    app = tmp_path / "PDF2MD.app"
    contents = app / "Contents"
    launcher = contents / "MacOS/python3"
    runtime_bin = contents / "Resources/python-runtime/bin"
    runtime_lib = contents / "Resources/python-runtime/lib/python3.12"
    launcher.parent.mkdir(parents=True)
    runtime_bin.mkdir(parents=True)
    runtime_lib.mkdir(parents=True)
    launcher.write_text(_launcher_template(), encoding="utf-8")
    launcher.chmod(0o755)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "python3",
                "CFBundleIdentifier": "com.pdf2md.release-sidecar-fixture",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1.2",
            },
            handle,
        )

    serving = contents / "Resources/src/parsing_core/serving"
    serving.mkdir(parents=True)
    (serving.parent / "__init__.py").write_text("", encoding="utf-8")
    (serving / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(
        ROOT / "src/parsing_core/serving/lifecycle.py",
        serving / "lifecycle.py",
    )
    fixture_server = serving / "serve.py"
    fixture_server.write_text(
        """import argparse
import json
import os
import socket
import time
from pathlib import Path

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--parent-pid", type=int)
parser.add_argument("--socket-fd", type=int, required=True)
parser.add_argument("--session-token-fd", type=int, required=True)
args, _ = parser.parse_known_args()
if "PDF2MD_SESSION_TOKEN" in os.environ:
    raise RuntimeError("session token must not be inherited through the environment")
token_bytes = b""
while True:
    chunk = os.read(args.session_token_fd, 256)
    if not chunk:
        break
    token_bytes += chunk
os.close(args.session_token_fd)
token = token_bytes.decode("ascii")
listener = socket.socket(fileno=args.socket_fd)
host, port = listener.getsockname()
intermediate = os.fork()
if intermediate == 0:
    os.setsid()
    session_leader = os.getpid()
    grandchild = os.fork()
    if grandchild != 0:
        os._exit(0)
    os.close(args.socket_fd)
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    detached_path = Path(os.environ["HOME"]) / "fixture-detached.json"
    temporary_path = detached_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps({
        "pid": os.getpid(),
        "sid": os.getsid(0),
        "pgid": os.getpgid(0),
        "intermediate": session_leader,
    }, separators=(",", ":")))
    os.replace(temporary_path, detached_path)
    while True:
        time.sleep(30)
os.waitpid(intermediate, 0)
if os.environ.get("PDF2MD_FIXTURE_SIDECAR_PID"):
    Path(os.environ["PDF2MD_FIXTURE_SIDECAR_PID"]).write_text(str(os.getpid()))
if os.environ.get("PDF2MD_FIXTURE_DESCENDANT_PID"):
    detached = Path(os.environ["HOME"]) / "fixture-detached.json"
    deadline = time.monotonic() + 5
    while not detached.exists() and time.monotonic() < deadline:
        time.sleep(0.001)
    Path(os.environ["PDF2MD_FIXTURE_DESCENDANT_PID"]).write_text(detached.read_text())
ready = {"schema": "pdf2md.sidecar.ready.v1", "host": host, "port": port}
print(json.dumps(ready, separators=(",", ":")), flush=True)
while True:
    connection, _ = listener.accept()
    with connection:
        request = b""
        while b"\\r\\n\\r\\n" not in request:
            request += connection.recv(4096)
        lines = request.decode("latin-1").split("\\r\\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = dict(
            (name.strip().lower(), value.strip())
            for line in lines[1:]
            if (separator := line.partition(":"))[1]
            for name, _, value in [separator]
        )
        authorized = headers.get("x-pdf2md-session") == token
        if not authorized:
            status = "401 Unauthorized"
            body = b'{"detail":{"code":"session_required"}}'
        elif method == "GET" and path == "/health":
            status = "200 OK"
            body = b'{"status":"ok"}'
        elif method == "POST" and path == "/shutdown":
            status = "200 OK"
            body = b'{"status":"shutting_down"}'
        else:
            status = "404 Not Found"
            body = b'{"detail":"not_found"}'
        response_head = (
            f"HTTP/1.1 {status}\\r\\nContent-Type: application/json\\r\\n"
            f"Content-Length: {len(body)}\\r\\nConnection: close\\r\\n\\r\\n"
        )
        connection.sendall(response_head.encode("ascii") + body)
    if authorized and method == "POST" and path == "/shutdown":
        break
""",
        encoding="utf-8",
    )
    runtime_python = runtime_bin / "python3.12"
    wrapper_source = tmp_path / "python-wrapper.c"
    wrapper_source.write_text(
        """#include <stddef.h>
#include <unistd.h>
extern char **environ;
int main(int argc, char **argv) {
    const unsigned char encoded[] = {
        0x7a, 0x20, 0x26, 0x27, 0x7a, 0x37, 0x3c, 0x3b,
        0x7a, 0x25, 0x2c, 0x21, 0x3d, 0x3a, 0x3b, 0x66
    };
    char executable[sizeof(encoded) + 1];
    for (size_t index = 0; index < sizeof(encoded); index++) {
        executable[index] = (char)(encoded[index] ^ 0x55);
    }
    executable[sizeof(encoded)] = 0;
    char *arguments[argc + 1];
    arguments[0] = executable;
    for (int index = 1; index < argc; index++) arguments[index] = argv[index];
    arguments[argc] = 0;
    execve(executable, arguments, environ);
    return 127;
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "/usr/bin/clang",
            "-arch",
            "arm64",
            str(wrapper_source),
            "-o",
            str(runtime_python),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_python.chmod(0o755)
    (runtime_bin / "python").symlink_to("python3.12")
    (runtime_bin / "python3").symlink_to("python3.12")

    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--options",
            "runtime",
            "--sign",
            "-",
            str(runtime_python),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--deep",
            "--options",
            "runtime",
            "--sign",
            "-",
            str(app),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return app


def test_release_sidecar_script_executes_inherited_socket_contract(tmp_path):
    app = _executable_bundle(tmp_path)

    result = subprocess.run(
        [str(RELEASE_TEST), str(app)],
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("_repeat", range(10))
def test_release_harness_requires_application_to_reap_detached_grandchild(tmp_path, _repeat):
    app = _executable_bundle(tmp_path)
    home = tmp_path / "harness-home"

    result = subprocess.run(
        [sys.executable, str(RELEASE_HARNESS), str(app), "--home", str(home)],
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    detached = json.loads((home / "fixture-detached.json").read_text())
    assert not _pid_exists(int(detached["pid"]))


def test_release_harness_ready_read_has_one_deadline_after_partial_byte(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    read_ready_line = core["_read_ready_line"]
    monkeypatch.setitem(
        read_ready_line.__globals__,
        "STARTUP_TIMEOUT_SECONDS",
        0.05,
    )
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)

    class Process:
        stdout = stream

    outcomes = []
    elapsed = []

    def read_partial_line():
        started = time.monotonic()
        try:
            read_ready_line(Process(), "test-session-token")
        except BaseException as error:
            outcomes.append(error)
        finally:
            elapsed.append(time.monotonic() - started)

    os.write(write_fd, b"{")
    thread = threading.Thread(target=read_partial_line, daemon=True)
    thread.start()
    thread.join(timeout=0.4)
    finished_within_deadline = not thread.is_alive()
    try:
        os.close(write_fd)
        thread.join(timeout=1)
    finally:
        stream.close()

    assert finished_within_deadline
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RuntimeError)
    assert elapsed and elapsed[0] < 0.3


def test_release_harness_ready_read_rejects_eof_without_newline(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    read_ready_line = core["_read_ready_line"]
    monkeypatch.setitem(
        read_ready_line.__globals__,
        "STARTUP_TIMEOUT_SECONDS",
        0.05,
    )
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    os.write(write_fd, b"{")
    os.close(write_fd)

    class Process:
        stdout = stream

    try:
        with pytest.raises(RuntimeError, match="invalid ready line"):
            read_ready_line(Process(), "test-session-token")
    finally:
        stream.close()


def _configured_harness_run(
    monkeypatch,
    *,
    ready_error=None,
    marked_cleanup=True,
    tree_error=None,
    stderr=b"",
    stderr_error=None,
    session_token="test-session-token-0123456789",
    harness_marker="f" * 64,
    root_marker=True,
    marker_scan=None,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    run = core["run"]
    calls = []

    class Baseline:
        def close(self):
            calls.append("baseline_close")

    baseline = Baseline()
    root_identity = LifecycleProcessIdentity(
        pid=91_001,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=123_456,
    )

    class ProcessTable:
        def uninspectable_pid_baseline(self):
            return ScanResult(baseline, complete=True)

        def identity(self, pid):
            assert pid == root_identity.pid
            return ScanResult(root_identity, complete=True)

        def marker_identities(self, *args, **kwargs):
            if marker_scan is not None:
                calls.append("marker_scan")
                return marker_scan()
            return ScanResult(set(), complete=True)

        def same_process_with_marker(self, identity, marker, marker_environment):
            assert identity == root_identity
            return ScanResult(root_marker, complete=True)

    process_table = ProcessTable()

    class Process:
        pid = root_identity.pid
        stdout = None
        stderr = None

        def __init__(self, listener_fd):
            with socket.fromfd(listener_fd, socket.AF_INET, socket.SOCK_STREAM) as listener:
                self.port = listener.getsockname()[1]

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def popen(*args, **kwargs):
        return Process(kwargs["pass_fds"][0])

    def read_ready_line(process, token, marker=None, on_poll=None):
        if on_poll is not None:
            on_poll()
        if ready_error is not None:
            raise ready_error
        return {"port": process.port}

    def cleanup_marked_processes(*args, **kwargs):
        calls.append("marker_cleanup")
        return marked_cleanup

    def cleanup_process_tree(*args, **kwargs):
        calls.append("tree_cleanup")
        if tree_error is not None:
            raise tree_error

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", lambda: process_table)
    monkeypatch.setattr(core["subprocess"], "Popen", popen)
    monkeypatch.setattr(core["secrets"], "token_urlsafe", lambda size: session_token)
    monkeypatch.setattr(core["secrets"], "token_hex", lambda size: harness_marker)
    monkeypatch.setitem(run.__globals__, "_read_ready_line", read_ready_line)
    monkeypatch.setitem(run.__globals__, "_request_json", lambda *args: None)
    monkeypatch.setitem(run.__globals__, "_capture_descendants", lambda *args: set())
    if marker_scan is not None:
        monkeypatch.setitem(run.__globals__, "_bind_marked_identities", lambda *args: None)

    def read_available(*args):
        if stderr_error is not None:
            raise stderr_error
        return stderr

    monkeypatch.setitem(run.__globals__, "_read_available", read_available)
    monkeypatch.setitem(
        run.__globals__,
        "_cleanup_marked_processes",
        cleanup_marked_processes,
    )
    monkeypatch.setitem(
        run.__globals__,
        "_cleanup_process_tree",
        cleanup_process_tree,
    )
    return run, calls


def test_release_harness_binds_root_marker_before_reading_ready(monkeypatch, tmp_path):
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=AssertionError("ready read was reached"),
        root_marker=False,
    )

    with pytest.raises(RuntimeError, match="root marker"):
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_shutdown_scan_waits_for_two_stable_empty_results(
    monkeypatch,
    tmp_path,
):
    transient_identity = LifecycleProcessIdentity(
        pid=91_002,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=654_321,
    )
    scans = iter(
        (
            ScanResult(set(), complete=False),
            ScanResult({transient_identity}, complete=True),
            ScanResult(set(), complete=True),
            ScanResult(set(), complete=True),
        )
    )
    run, calls = _configured_harness_run(
        monkeypatch,
        marker_scan=lambda: next(scans),
    )
    now = 0.0

    def monotonic():
        return now

    def sleep(delay):
        nonlocal now
        assert 0 < delay <= 0.05
        now += delay

    monkeypatch.setitem(
        run.__globals__,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=sleep),
    )
    monkeypatch.setitem(run.__globals__, "CLEAN_SCAN_TIMEOUT_SECONDS", 0.2)

    run(tmp_path / "fixture.app", tmp_path / "home")

    assert calls.count("marker_scan") == 4


@pytest.mark.parametrize(
    "scan_result",
    [
        ScanResult(set(), complete=False),
        ScanResult(
            {
                LifecycleProcessIdentity(
                    pid=91_003,
                    uid=os.getuid(),
                    started_seconds=1_700_000_000,
                    started_microseconds=987_654,
                )
            },
            complete=True,
        ),
    ],
)
def test_release_harness_shutdown_scan_fails_after_bounded_nonconvergence(
    monkeypatch,
    tmp_path,
    scan_result,
):
    run, calls = _configured_harness_run(
        monkeypatch,
        marker_scan=lambda: scan_result,
    )
    now = 0.0

    def monotonic():
        return now

    def sleep(delay):
        nonlocal now
        assert 0 < delay <= 0.05
        now += delay

    monkeypatch.setitem(
        run.__globals__,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=sleep),
    )
    monkeypatch.setitem(run.__globals__, "CLEAN_SCAN_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError, match="clean its own process tree"):
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert calls.count("marker_scan") >= 3
    assert now <= 0.06


def test_release_harness_redacts_nested_exception_args_without_repr(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    redact_exception = core["_redact_exception"]
    token = "nested-session-token-0123456789"
    marker = "abcdef0123456789" * 4
    repr_calls = []

    class SecretObject:
        def __repr__(self):
            repr_calls.append("repr")
            return marker

        def __str__(self):
            repr_calls.append("str")
            return token

    cause = ValueError({"cause": (token, SecretObject())})
    error = RuntimeError(
        {
            "list": [token, marker, SecretObject()],
            "tuple": (f"{core['HARNESS_MARKER_ENV']}={marker}",),
            "set": {token, marker},
        }
    )
    error.__cause__ = cause
    error.__context__ = cause
    error.add_note(f"note {token} {marker}")

    redact_exception(error, token, marker)

    nested = error.args[0]
    assert nested["list"] == ["[REDACTED]", "[REDACTED]", "[REDACTED]"]
    assert nested["tuple"] == ("[REDACTED]",)
    assert nested["set"] == {"[REDACTED]"}
    assert cause.args[0]["cause"] == ("[REDACTED]", "[REDACTED]")
    assert error.__notes__ == ["note [REDACTED] [REDACTED]"]
    assert repr_calls == []


def test_release_harness_redaction_bounds_cycles_and_nested_causes():
    core = runpy.run_path(str(RELEASE_HARNESS))
    redact_exception = core["_redact_exception"]
    token = "cyclic-session-token-0123456789"
    marker = "1234567890abcdef" * 4
    cycle = []
    cycle.append(cycle)
    cause = ValueError(token)
    error = RuntimeError(
        {
            "cycle": cycle,
            "oversized": [marker] * (core["MAX_REDACTION_ITEMS"] + 5),
        }
    )
    error.__cause__ = cause
    cause.__context__ = error

    redact_exception(error, token, marker)

    rendered = BaseException.__str__(error) + BaseException.__str__(cause)
    assert token not in rendered
    assert marker not in rendered
    assert error.args[0]["cycle"] == ["[REDACTED]"]
    assert len(error.args[0]["oversized"]) <= core["MAX_REDACTION_ITEMS"]


def test_release_harness_redaction_severs_deep_base_exception_graph():
    core = runpy.run_path(str(RELEASE_HARNESS))
    redact_exception = core["_redact_exception"]
    token = "deep-session-token-0123456789"
    marker = "fedcba9876543210" * 4
    absolute_path = "/opt/vendor/private-book.pdf"
    stderr_secret = "stderr contained private credentials"
    current: BaseException = SystemExit(f"{token} {marker} {absolute_path} {stderr_secret}")
    for index in range(core["MAX_REDACTION_DEPTH"] + 4):
        parent: BaseException
        if index % 2:
            parent = KeyboardInterrupt(f"layer {index} {token} {absolute_path}")
            parent.__cause__ = current
        else:
            parent = SystemExit(f"layer {index} {marker} {stderr_secret}")
            parent.__context__ = current
        parent.__suppress_context__ = False
        parent.add_note(f"note {token} {marker} {absolute_path} {stderr_secret}")
        current = parent

    redact_exception(current, token, marker)

    pending = [current]
    seen = set()
    rendered = []
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        rendered.append(BaseException.__str__(error))
        rendered.extend(getattr(error, "__notes__", ()))
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)

    output = "\n".join(rendered)
    for secret in (token, marker, absolute_path, stderr_secret):
        assert secret not in output
    assert len(seen) <= core["MAX_REDACTION_DEPTH"] + 2


def test_release_harness_redaction_bounds_text_bytes_and_ignores_unknown_iterables():
    core = runpy.run_path(str(RELEASE_HARNESS))
    redact_exception = core["_redact_exception"]
    token = "bounded-session-token-0123456789"
    marker = "0123456789abcdef" * 4
    calls = 0

    class BlockingIterable:
        def __iter__(self):
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            yield token

    huge_text = token + marker + ("x" * 100_000)
    huge_bytes = huge_text.encode("ascii")
    error = RuntimeError(
        BlockingIterable(),
        huge_text,
        huge_bytes,
        [huge_text] * core["MAX_REDACTION_ITEMS"],
    )

    redact_exception(error, token, marker)

    redacted_iterable, redacted_text, redacted_bytes, _items = error.args
    assert redacted_iterable == "[REDACTED]"
    assert calls == 0
    assert len(redacted_text) <= core.get("MAX_REDACTION_TEXT_CHARS", 4096)
    assert len(redacted_bytes) <= core.get("MAX_REDACTION_BINARY_BYTES", 4096)
    rendered = BaseException.__str__(error).encode("utf-8", errors="replace")
    assert len(rendered) <= core.get("MAX_REDACTION_OUTPUT_BYTES", 16 * 1024)
    assert token.encode() not in rendered
    assert marker.encode() not in rendered


def test_release_harness_redaction_magic_failure_is_fail_safe():
    core = runpy.run_path(str(RELEASE_HARNESS))
    redact_exception = core["_redact_exception"]
    attempted = False

    class ExplodingIterable:
        def __iter__(self):
            nonlocal attempted
            attempted = True
            raise MemoryError("must not escape redaction")

    error = RuntimeError(ExplodingIterable())

    redact_exception(error, "token", "marker")

    assert not attempted
    assert error.args == ("[REDACTED]",)


def test_release_harness_hostile_exception_descriptor_is_replaced_without_magic_calls(
    monkeypatch,
    tmp_path,
):
    token = "descriptor-secret-token-9274"
    calls = []

    class HostileError(RuntimeError):
        @property
        def args(self):
            calls.append("args_get")
            raise MemoryError("args read must not run")

        @args.setter
        def args(self, _value):
            calls.append("args_set")
            raise MemoryError("args write must not run")

        def __str__(self):
            calls.append("str")
            return token

        def __repr__(self):
            calls.append("repr")
            return token

    primary = HostileError(token)
    run, cleanup_calls = _configured_harness_run(
        monkeypatch,
        ready_error=primary,
        stderr=b"diagnostic",
    )

    with pytest.raises(RuntimeError) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert type(captured.value) is RuntimeError
    assert captured.value.args == ("release sidecar smoke failed",)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert getattr(captured.value, "__notes__", []) == []
    assert token not in BaseException.__str__(captured.value)
    assert calls == []
    assert cleanup_calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_redaction_failure_never_masks_primary_or_cleanup(
    monkeypatch,
    tmp_path,
):
    primary = KeyboardInterrupt("primary cancellation")
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=primary,
        marked_cleanup=False,
    )

    def fail_redaction(*args, **kwargs):
        raise MemoryError("injected redaction failure")

    monkeypatch.setitem(run.__globals__, "re", SimpleNamespace(sub=fail_redaction))

    with pytest.raises(KeyboardInterrupt) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert captured.value is primary
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_stderr_read_failure_preserves_primary_and_cleanup(
    monkeypatch,
    tmp_path,
):
    primary = ValueError("ready primary failure")
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=primary,
        stderr_error=MemoryError("injected stderr read failure"),
    )

    with pytest.raises(ValueError) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert captured.value is primary
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_overridden_add_note_cannot_replace_primary_or_cleanup(
    monkeypatch,
    tmp_path,
):
    primary = ValueError("ready primary failure")
    note_calls = []

    def hostile_add_note(_note):
        note_calls.append("add_note")
        raise MemoryError("instance add_note must not run")

    primary.add_note = hostile_add_note
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=primary,
        marked_cleanup=False,
    )

    with pytest.raises(ValueError) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert captured.value is primary
    assert note_calls == []
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_redaction_enforces_one_global_output_budget():
    core = runpy.run_path(str(RELEASE_HARNESS))
    redact_exception = core["_redact_exception"]
    token = "global-budget-secret-token"
    huge = token + ("x" * core["MAX_REDACTION_TEXT_CHARS"])
    error = RuntimeError([[huge] * core["MAX_REDACTION_ITEMS"]] * 64)

    redact_exception(error, token, "marker")

    rendered = BaseException.__str__(error).encode("utf-8", errors="replace")
    assert len(rendered) <= core["MAX_REDACTION_OUTPUT_BYTES"]
    assert token.encode() not in rendered


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(SystemExit(23), 23), (KeyboardInterrupt("secret interrupt"), 130)],
)
def test_release_harness_safe_main_contains_base_exception_tracebacks(
    monkeypatch,
    failure,
    expected_status,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    safe_main = core["_safe_main"]
    token = "top-level-secret-token"
    marker = "top-level-secret-marker"
    deepest = RuntimeError(f"{token} {marker} /Users/private/book.pdf stderr secret")
    failure.__cause__ = deepest
    audit = io.StringIO()
    monkeypatch.setattr(core["sys"], "stderr", audit)

    def entrypoint():
        raise failure

    assert safe_main(entrypoint) == expected_status
    output = audit.getvalue()
    assert "release sidecar smoke failed" in output
    assert "Traceback" not in output
    assert token not in output
    assert marker not in output
    assert "/Users/private/book.pdf" not in output
    assert "stderr secret" not in output


def test_release_harness_python39_note_fallback_does_not_mutate_primary_args(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    append_exception_note = core["_append_exception_note"]
    audit = io.StringIO()
    fake_sys = SimpleNamespace(version_info=(3, 9), stderr=audit)
    monkeypatch.setitem(append_exception_note.__globals__, "sys", fake_sys)
    error = KeyboardInterrupt("original cancellation")
    original_args = error.args

    append_exception_note(error, "release sidecar cleanup was incomplete")

    assert error.args == original_args
    assert audit.getvalue() == "release sidecar cleanup was incomplete\n"


@pytest.mark.parametrize("failure_point", ["set_inheritable", "write", "close"])
def test_session_token_pipe_closes_all_owned_fds_on_failure(monkeypatch, failure_point):
    core = runpy.run_path(str(RELEASE_HARNESS))
    session_token_pipe = core["_session_token_pipe"]
    injected = OSError(f"injected {failure_point} failure")
    close_calls = []

    monkeypatch.setattr(core["os"], "pipe", lambda: (101, 102))

    def set_inheritable(fd, inheritable):
        if failure_point == "set_inheritable":
            raise injected

    def write(fd, value):
        if failure_point == "write":
            raise injected
        return len(value)

    def close(fd):
        close_calls.append(fd)
        if failure_point == "close" and fd == 102:
            raise injected

    monkeypatch.setattr(core["os"], "set_inheritable", set_inheritable)
    monkeypatch.setattr(core["os"], "write", write)
    monkeypatch.setattr(core["os"], "close", close)

    with pytest.raises(OSError) as captured:
        session_token_pipe("test-session-token")

    assert captured.value is injected
    assert close_calls == [102, 101]


def test_release_harness_popen_base_exception_closes_setup_resources(
    monkeypatch,
    tmp_path,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    run = core["run"]
    calls = []
    failure = KeyboardInterrupt("injected Popen cancellation")
    token_fd, token_write = os.pipe()
    os.close(token_write)

    class Baseline:
        def close(self):
            calls.append("baseline_close")

    class ProcessTable:
        def uninspectable_pid_baseline(self):
            return ScanResult(Baseline(), complete=True)

        def marker_identities(self, *args, **kwargs):
            return ScanResult(set(), complete=True)

    class Listener:
        def bind(self, address):
            calls.append("listener_bind")

        def listen(self):
            calls.append("listener_listen")

        def set_inheritable(self, inheritable):
            calls.append("listener_inheritable")

        def getsockname(self):
            return "127.0.0.1", 32001

        def fileno(self):
            return 101

        def close(self):
            calls.append("listener_close")

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", ProcessTable)
    monkeypatch.setattr(core["socket"], "socket", lambda *args, **kwargs: Listener())
    monkeypatch.setitem(run.__globals__, "_session_token_pipe", lambda token: token_fd)
    monkeypatch.setattr(
        core["subprocess"],
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setitem(
        run.__globals__,
        "_cleanup_marked_processes",
        lambda *args, **kwargs: calls.append("marker_cleanup") or True,
    )

    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            run(tmp_path / "fixture.app", tmp_path / "home")

        assert captured.value is failure
        assert "listener_close" in calls
        assert "marker_cleanup" in calls
        assert "baseline_close" in calls
        with pytest.raises(OSError):
            os.fstat(token_fd)
    finally:
        try:
            os.close(token_fd)
        except OSError:
            pass


def test_release_harness_process_snapshot_uses_bounded_absolute_ps(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    process_snapshot = core["_process_snapshot"]
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"101 1\n")
    os.close(write_fd)
    stream = os.fdopen(read_fd, "rb", buffering=0)
    calls = []

    class Process:
        stdout = stream

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pytest.fail("completed ps process must not be killed")

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(core["subprocess"], "Popen", popen)
    try:
        assert process_snapshot() == {101: 1}
    finally:
        stream.close()

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["/bin/ps", "-axo", "pid=,ppid="]
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_release_harness_process_snapshot_timeout_kills_and_reaps_ps(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    process_snapshot = core["_process_snapshot"]
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)

    class Process:
        stdout = stream

        def __init__(self):
            self.killed = False
            self.reaped = False

        def poll(self):
            return -signal.SIGKILL if self.killed else None

        def wait(self, timeout=None):
            self.reaped = True
            return -signal.SIGKILL

        def kill(self):
            self.killed = True
            os.close(write_fd)

    process = Process()
    monkeypatch.setattr(core["subprocess"], "Popen", lambda *args, **kwargs: process)
    monkeypatch.setitem(process_snapshot.__globals__, "PS_TIMEOUT_SECONDS", 0.03)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process_snapshot()
    finally:
        if not process.killed:
            os.close(write_fd)

    assert time.monotonic() - started < 0.3
    assert process.killed
    assert process.reaped
    assert stream.closed


def test_release_harness_process_snapshot_rejects_oversized_output(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    process_snapshot = core["_process_snapshot"]
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"1" * 17)
    os.close(write_fd)
    stream = os.fdopen(read_fd, "rb", buffering=0)

    class Process:
        stdout = stream

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pytest.fail("completed ps process must not be killed")

    monkeypatch.setattr(core["subprocess"], "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setitem(process_snapshot.__globals__, "MAX_PS_OUTPUT_BYTES", 16)

    with pytest.raises(subprocess.SubprocessError, match="output exceeded"):
        process_snapshot()

    assert stream.closed


@pytest.mark.parametrize("failure_stage", ["constructor", "register"])
def test_release_harness_process_snapshot_owns_resources_from_first_allocation(
    monkeypatch,
    failure_stage,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    process_snapshot = core["_process_snapshot"]
    primary = KeyboardInterrupt(f"injected selector {failure_stage} failure")
    events = []

    class Stream:
        closed = False

        def fileno(self):
            return 101

        def close(self):
            self.closed = True
            events.append("stdout_close")

    stream = Stream()

    class Process:
        stdout = stream

        def poll(self):
            return None

        def kill(self):
            events.append("kill")

        def wait(self, timeout=None):
            events.append("wait")
            return -signal.SIGKILL

    class Selector:
        def register(self, *args):
            events.append("register")
            if failure_stage == "register":
                raise primary

        def close(self):
            events.append("selector_close")

    def selector_factory():
        events.append("selector_construct")
        if failure_stage == "constructor":
            raise primary
        return Selector()

    monkeypatch.setattr(core["subprocess"], "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(core["selectors"], "DefaultSelector", selector_factory)

    with pytest.raises(KeyboardInterrupt) as captured:
        process_snapshot()

    assert captured.value is primary
    assert "kill" in events
    assert "wait" in events
    assert events.index("kill") < events.index("wait") < events.index("stdout_close")
    if failure_stage == "register":
        assert "selector_close" in events
    assert stream.closed


@pytest.mark.parametrize(
    "failure_stage",
    ["kill", "wait", "selector_close", "stdout_close"],
)
def test_release_harness_process_snapshot_cleanup_failures_preserve_primary(
    monkeypatch,
    failure_stage,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    process_snapshot = core["_process_snapshot"]
    primary = KeyboardInterrupt("primary process-table failure")
    events = []

    class Stream:
        def fileno(self):
            return 102

        def close(self):
            events.append("stdout_close")
            if failure_stage == "stdout_close":
                raise OSError("injected stdout close failure")

    class Process:
        stdout = Stream()

        def poll(self):
            return None

        def kill(self):
            events.append("kill")
            if failure_stage == "kill":
                raise OSError("injected kill failure")

        def wait(self, timeout=None):
            events.append("wait")
            if failure_stage == "wait":
                raise OSError("injected wait failure")
            return -signal.SIGKILL

    class Selector:
        def register(self, *args):
            events.append("register")

        def select(self, timeout):
            raise primary

        def close(self):
            events.append("selector_close")
            if failure_stage == "selector_close":
                raise OSError("injected selector close failure")

    monkeypatch.setattr(core["subprocess"], "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(core["selectors"], "DefaultSelector", Selector)

    with pytest.raises(KeyboardInterrupt) as captured:
        process_snapshot()

    assert captured.value is primary
    assert events == ["register", "kill", "wait", "selector_close", "stdout_close"]


def test_release_harness_process_snapshot_cleanup_failure_is_safe_without_primary(
    monkeypatch,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    process_snapshot = core["_process_snapshot"]
    events = []

    class Stream:
        def fileno(self):
            return 103

        def close(self):
            events.append("stdout_close")

    class Process:
        stdout = Stream()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            events.append("wait")
            return 0

        def kill(self):
            pytest.fail("completed process must not be killed")

    class Selector:
        def register(self, *args):
            events.append("register")

        def select(self, timeout):
            return [(object(), selectors.EVENT_READ)]

        def close(self):
            events.append("selector_close")
            raise OSError("injected selector close failure")

    monkeypatch.setattr(core["subprocess"], "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(core["selectors"], "DefaultSelector", Selector)
    monkeypatch.setattr(core["os"], "read", lambda *args: b"")

    with pytest.raises(RuntimeError, match="process table cleanup failed"):
        process_snapshot()

    assert events == ["register", "wait", "selector_close", "stdout_close"]


def test_release_harness_ready_reader_close_failure_preserves_primary_and_state(
    monkeypatch,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    read_ready_line = core["_read_ready_line"]
    primary = KeyboardInterrupt("primary ready read failure")
    blocking = []
    events = []

    class Stream:
        def fileno(self):
            return 104

    class Process:
        stdout = Stream()

    class Selector:
        def register(self, *args):
            events.append("register")

        def select(self, timeout):
            raise primary

        def close(self):
            events.append("selector_close")
            raise OSError("injected selector close failure")

    monkeypatch.setattr(core["selectors"], "DefaultSelector", Selector)
    monkeypatch.setattr(core["os"], "get_blocking", lambda fd: True)
    monkeypatch.setattr(
        core["os"],
        "set_blocking",
        lambda fd, value: blocking.append(value),
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        read_ready_line(Process(), "test-session-token")

    assert captured.value is primary
    assert events == ["register", "selector_close"]
    assert blocking == [False, True]


def test_release_harness_ready_reader_register_failure_closes_selector(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    read_ready_line = core["_read_ready_line"]
    primary = KeyboardInterrupt("primary selector register failure")
    events = []

    class Stream:
        def fileno(self):
            return 105

    class Process:
        stdout = Stream()

    class Selector:
        def register(self, *args):
            events.append("register")
            raise primary

        def close(self):
            events.append("selector_close")

    monkeypatch.setattr(core["selectors"], "DefaultSelector", Selector)

    with pytest.raises(KeyboardInterrupt) as captured:
        read_ready_line(Process(), "test-session-token")

    assert captured.value is primary
    assert events == ["register", "selector_close"]


def test_release_harness_available_reader_cleanup_preserves_primary(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    read_available = core["_read_available"]
    primary = KeyboardInterrupt("primary available read failure")
    events = []

    class Stream:
        def fileno(self):
            return 106

    class Selector:
        def register(self, *args):
            events.append("register")

        def select(self, timeout):
            return [(object(), selectors.EVENT_READ)]

        def close(self):
            events.append("selector_close")
            raise OSError("injected selector close failure")

    monkeypatch.setattr(core["selectors"], "DefaultSelector", Selector)
    monkeypatch.setattr(core["os"], "read", lambda *args: (_ for _ in ()).throw(primary))

    with pytest.raises(KeyboardInterrupt) as captured:
        read_available(Stream(), 10)

    assert captured.value is primary
    assert events == ["register", "selector_close"]


def test_release_harness_available_reader_register_failure_closes_selector(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    read_available = core["_read_available"]
    primary = KeyboardInterrupt("primary available register failure")
    events = []

    class Stream:
        def fileno(self):
            return 107

    class Selector:
        def register(self, *args):
            events.append("register")
            raise primary

        def close(self):
            events.append("selector_close")

    monkeypatch.setattr(core["selectors"], "DefaultSelector", Selector)

    with pytest.raises(KeyboardInterrupt) as captured:
        read_available(Stream(), 10)

    assert captured.value is primary
    assert events == ["register", "selector_close"]


def test_release_harness_fails_closed_when_marker_cleanup_returns_false(
    monkeypatch,
    tmp_path,
):
    run, calls = _configured_harness_run(monkeypatch, marked_cleanup=False)

    with pytest.raises(RuntimeError, match="marker cleanup was incomplete"):
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_reports_marker_and_tree_cleanup_failures(
    monkeypatch,
    tmp_path,
):
    run, calls = _configured_harness_run(
        monkeypatch,
        marked_cleanup=False,
        tree_error=RuntimeError("tree cleanup exploded"),
    )

    with pytest.raises(RuntimeError) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    detail = str(captured.value) + "\n" + "\n".join(getattr(captured.value, "__notes__", ()))
    assert "marker cleanup was incomplete" in detail
    assert "tree cleanup exploded" in detail
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_preserves_primary_error_when_cleanup_also_fails(
    monkeypatch,
    tmp_path,
):
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=ValueError("ready primary failure"),
        marked_cleanup=False,
        tree_error=RuntimeError("tree cleanup exploded"),
    )

    with pytest.raises(ValueError, match="ready primary failure") as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert "marker cleanup was incomplete" in notes
    assert "tree cleanup exploded" in notes
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_preserves_keyboard_interrupt_when_cleanup_also_fails(
    monkeypatch,
    tmp_path,
):
    interruption = KeyboardInterrupt("cancel release smoke")
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=interruption,
        marked_cleanup=False,
        tree_error=RuntimeError("tree cleanup exploded"),
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert captured.value is interruption
    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert "marker cleanup was incomplete" in notes
    assert "tree cleanup exploded" in notes
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_preserves_system_exit_when_cleanup_also_fails(
    monkeypatch,
    tmp_path,
):
    requested_exit = SystemExit(23)
    run, calls = _configured_harness_run(
        monkeypatch,
        ready_error=requested_exit,
        marked_cleanup=False,
        tree_error=RuntimeError("tree cleanup exploded"),
    )

    with pytest.raises(SystemExit) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    assert captured.value is requested_exit
    assert captured.value.code == 23
    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert "marker cleanup was incomplete" in notes
    assert "tree cleanup exploded" in notes
    assert calls == ["marker_cleanup", "tree_cleanup", "baseline_close"]


def test_release_harness_redacts_session_and_marker_from_errors_and_notes(
    monkeypatch,
    tmp_path,
):
    session_token = "fixed-session-token-0123456789"
    harness_marker = "0123456789abcdef" * 4
    marker_assignment = f"PDF2MD_RELEASE_HARNESS_MARKER={harness_marker}"
    ready_error = ValueError(f"ready leaked {session_token} {harness_marker} {marker_assignment}")
    stderr = (f"stderr leaked {session_token} {harness_marker} {marker_assignment}").encode()
    run, _calls = _configured_harness_run(
        monkeypatch,
        ready_error=ready_error,
        marked_cleanup=False,
        tree_error=RuntimeError(f"tree leaked {marker_assignment}"),
        stderr=stderr,
        session_token=session_token,
        harness_marker=harness_marker,
    )

    with pytest.raises(RuntimeError) as captured:
        run(tmp_path / "fixture.app", tmp_path / "home")

    errors = []
    pending = [captured.value]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        errors.append(str(error))
        errors.extend(getattr(error, "__notes__", ()))
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)
    rendered = "\n".join(errors)
    assert session_token not in rendered
    assert harness_marker not in rendered
    assert marker_assignment not in rendered
    assert "[REDACTED]" in rendered


def test_release_harness_fallback_cleanup_reuses_pre_marker_pid_baseline():
    cleanup_marked_processes = runpy.run_path(str(RELEASE_HARNESS))["_cleanup_marked_processes"]
    baseline = frozenset({102})
    observed_baselines = []

    class CompleteEmptyScan:
        value = set()
        complete = True

    class ProcessTable:
        def marker_identities(
            self,
            marker,
            excluded_pid,
            marker_environment,
            *,
            uninspectable_pid_baseline,
        ):
            observed_baselines.append(uninspectable_pid_baseline)
            return CompleteEmptyScan()

        def same_process_with_marker(self, identity, marker, marker_environment):
            return ScanResult(False, complete=True)

    cleanup_marked_processes(ProcessTable(), "a" * 64, baseline)

    assert observed_baselines
    assert set(observed_baselines) == {baseline}


def test_release_harness_fallback_retains_known_target_until_term_kill(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    cleanup_marked_processes = core["_cleanup_marked_processes"]
    target = LifecycleProcessIdentity(
        pid=os.getpid() + 20_001,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=654_321,
    )
    alive = True
    scans = 0
    signals = []

    class ProcessTable:
        def marker_identities(self, *args, **kwargs):
            nonlocal scans
            scans += 1
            return ScanResult({target} if scans == 1 else set(), complete=True)

        def same_process_with_marker(self, identity, marker, marker_environment):
            assert identity == target
            return ScanResult(alive, complete=True)

        def identity(self, pid):
            assert pid == target.pid
            return ScanResult(target if alive else None, complete=True)

    def signal_process(pid, sig):
        nonlocal alive
        assert pid == target.pid
        signals.append(sig)
        if sig == signal.SIGKILL:
            alive = False

    monkeypatch.setattr(core["os"], "kill", signal_process)

    assert cleanup_marked_processes(ProcessTable(), "a" * 64, object())
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert scans >= 4


def test_release_harness_bound_marker_identity_fails_closed_when_identity_is_incomplete(
    monkeypatch,
):
    core = runpy.run_path(str(RELEASE_HARNESS))
    cleanup_marked_processes = core["_cleanup_marked_processes"]
    target = LifecycleProcessIdentity(
        pid=os.getpid() + 20_007,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=654_321,
    )
    marker_scans = 0
    now = 0.0
    signals = []

    class ProcessTable:
        def marker_identities(self, *args, **kwargs):
            nonlocal marker_scans
            marker_scans += 1
            return ScanResult({target} if marker_scans == 1 else set(), complete=True)

        def same_process_with_marker(self, identity, marker, marker_environment):
            assert identity == target
            return ScanResult(False, complete=True)

        def identity(self, pid):
            assert pid == target.pid
            return ScanResult(target, complete=False)

    def monotonic():
        return now

    def sleep(_seconds):
        nonlocal now
        now += 0.25

    monkeypatch.setattr(core["time"], "monotonic", monotonic)
    monkeypatch.setattr(core["time"], "sleep", sleep)
    monkeypatch.setattr(
        core["os"],
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert not cleanup_marked_processes(ProcessTable(), "a" * 64, object())
    assert signals == []


def test_release_harness_reaps_identity_that_clears_marker_before_cleanup(tmp_path):
    app = _executable_bundle(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launcher = app / "Contents/MacOS/python3"
    real_launcher = launcher.with_name("python3-real")
    launcher.rename(real_launcher)
    target_state_path = home / "precleanup-exec-target.json"
    target_exec_path = home / "precleanup-exec-target.exec"
    helper = tmp_path / "detach-and-clear-marker.py"
    helper.write_text(
        """import json
import os
import signal
import sys
import time
from pathlib import Path

if os.fork() != 0:
    os._exit(0)
os.setsid()
if os.fork() != 0:
    os._exit(0)
state_path = Path(os.environ["HOME"]) / "precleanup-exec-target.json"
temporary_path = state_path.with_suffix(".tmp")
temporary_path.write_text(json.dumps({
    "pid": os.getpid(),
    "marker": os.environ["PDF2MD_RELEASE_HARNESS_MARKER"],
}), encoding="utf-8")
os.replace(temporary_path, state_path)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(0.2)
environment = os.environ.copy()
environment.pop("PDF2MD_RELEASE_HARNESS_MARKER", None)
environment.pop("PDF2MD_OWNER_MARKER", None)
os.execve(sys.executable, [
    sys.executable,
    "-c",
    "import os,signal,time; from pathlib import Path; "
    "Path(os.environ['HOME']).joinpath('precleanup-exec-target.exec').touch(); "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
], environment)
""",
        encoding="utf-8",
    )
    launcher.write_text(
        "#!/bin/bash\n"
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(helper))
        + " &\nexec /bin/bash "
        + shlex.quote(str(real_launcher))
        + ' "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    run = runpy.run_path(str(RELEASE_HARNESS))["run"]
    outcome = []

    def invoke_harness():
        try:
            run(app, home)
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=invoke_harness)
    worker.start()
    process_table = DarwinProcessTable()
    target_identity = None
    try:
        deadline = time.monotonic() + 5
        while not target_state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert target_state_path.exists()
        target_state = json.loads(target_state_path.read_text(encoding="utf-8"))
        target_identity = _strong_identity(
            process_table,
            int(target_state["pid"]),
        )

        while not target_exec_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert target_exec_path.exists()
        assert process_table.identity(target_identity.pid) == ScanResult(
            target_identity,
            complete=True,
        )
        assert process_table.has_exact_marker(
            target_identity.pid,
            target_state["marker"],
            "PDF2MD_RELEASE_HARNESS_MARKER",
        ) == ScanResult(False, complete=True)
        assert worker.is_alive()

        worker.join(timeout=15)
        assert not worker.is_alive()
        assert outcome == []
        assert _wait_strong_identity_gone(process_table, target_identity)
    finally:
        if worker.is_alive():
            worker.join(timeout=1)
        if target_identity is not None:
            scan = process_table.identity(target_identity.pid)
            if scan.complete and scan.value == target_identity:
                os.kill(target_identity.pid, signal.SIGKILL)


def test_release_harness_never_signals_a_reused_pid(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    identity_type = core["ProcessIdentity"]
    signal_identities = core["_signal_identities"]
    pid = os.getpid()
    captured = identity_type(
        pid=pid,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=123_456,
        depth=1,
    )
    replacement = LifecycleProcessIdentity(
        pid=pid,
        uid=os.getuid(),
        started_seconds=1_700_000_001,
        started_microseconds=123_456,
    )

    class ProcessTable:
        def identity(self, requested_pid):
            assert requested_pid == pid
            return ScanResult(replacement, complete=True)

    signals = []
    monkeypatch.setattr(core["os"], "kill", lambda target, sig: signals.append((target, sig)))

    signal_identities(ProcessTable(), {captured}, signal.SIGTERM)

    assert signals == []


def test_release_harness_rechecks_identity_before_and_after_signal(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    identity_type = core["ProcessIdentity"]
    signal_identities = core["_signal_identities"]
    captured = identity_type(
        pid=os.getpid() + 20_002,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=123_456,
        depth=1,
    )
    matching = LifecycleProcessIdentity(
        captured.pid,
        captured.uid,
        captured.started_seconds,
        captured.started_microseconds,
    )
    replacement = LifecycleProcessIdentity(
        captured.pid,
        captured.uid,
        captured.started_seconds + 1,
        captured.started_microseconds,
    )
    observations = iter((matching, matching, replacement))
    identity_calls = []
    signals = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == captured.pid
            value = next(observations)
            identity_calls.append(value)
            return ScanResult(value, complete=True)

    monkeypatch.setattr(
        core["os"],
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    signal_identities(ProcessTable(), {captured}, signal.SIGTERM)

    assert identity_calls == [matching, matching, replacement]
    assert signals == [(captured.pid, signal.SIGTERM)]


def test_release_harness_aborts_signal_when_second_identity_check_sees_reuse(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    identity_type = core["ProcessIdentity"]
    signal_identities = core["_signal_identities"]
    captured = identity_type(
        pid=os.getpid() + 20_003,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=123_456,
        depth=1,
    )
    matching = LifecycleProcessIdentity(
        captured.pid,
        captured.uid,
        captured.started_seconds,
        captured.started_microseconds,
    )
    replacement = LifecycleProcessIdentity(
        captured.pid,
        captured.uid,
        captured.started_seconds + 1,
        captured.started_microseconds,
    )
    observations = iter((matching, replacement))
    signals = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == captured.pid
            return ScanResult(next(observations), complete=True)

    monkeypatch.setattr(
        core["os"],
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    signal_identities(ProcessTable(), {captured}, signal.SIGTERM)

    assert signals == []


def test_release_harness_rejects_relationship_changed_during_capture(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    identity_type = core["ProcessIdentity"]
    capture_descendants = core["_capture_descendants"]
    root_pid = os.getpid()
    first_parent = root_pid + 10_001
    second_parent = root_pid + 10_002
    child_pid = root_pid + 10_003
    identities = {
        pid: LifecycleProcessIdentity(pid, os.getuid(), 1_700_000_000, pid)
        for pid in (root_pid, first_parent, second_parent, child_pid)
    }
    root = identity_type(
        pid=root_pid,
        uid=os.getuid(),
        started_seconds=1_700_000_000,
        started_microseconds=root_pid,
        depth=0,
    )
    snapshots = iter(
        (
            {
                root_pid: 1,
                first_parent: root_pid,
                second_parent: root_pid,
                child_pid: first_parent,
            },
            {
                root_pid: 1,
                first_parent: root_pid,
                second_parent: root_pid,
                child_pid: second_parent,
            },
        )
    )

    class ProcessTable:
        def identity(self, pid):
            return ScanResult(identities[pid], complete=True)

    monkeypatch.setitem(
        capture_descendants.__globals__,
        "_process_snapshot",
        lambda: next(snapshots),
    )

    captured = capture_descendants(ProcessTable(), root)

    assert child_pid not in {identity.pid for identity in captured}


def test_release_harness_ps_failure_still_terminates_strong_root(monkeypatch):
    core = runpy.run_path(str(RELEASE_HARNESS))
    identity_type = core["ProcessIdentity"]
    cleanup_process_tree = core["_cleanup_process_tree"]
    root_pid = os.getpid() + 30_001
    root_lifecycle_identity = LifecycleProcessIdentity(
        root_pid,
        os.getuid(),
        1_700_000_000,
        777_777,
    )
    root = identity_type(
        pid=root_pid,
        uid=root_lifecycle_identity.uid,
        started_seconds=root_lifecycle_identity.started_seconds,
        started_microseconds=root_lifecycle_identity.started_microseconds,
        depth=0,
    )
    alive = True
    signals = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == root_pid
            return ScanResult(root_lifecycle_identity if alive else None, complete=True)

    class Process:
        pid = root_pid

        def poll(self):
            return None if alive else 0

        def wait(self, timeout=None):
            assert not alive
            return 0

    def fail_snapshot():
        raise subprocess.SubprocessError("enumeration unavailable")

    def signal_process(pid, sig):
        nonlocal alive
        assert pid == root_pid
        signals.append(sig)
        alive = False

    monkeypatch.setitem(
        cleanup_process_tree.__globals__,
        "_process_snapshot",
        fail_snapshot,
    )
    monkeypatch.setattr(core["os"], "kill", signal_process)

    cleanup_process_tree(Process(), ProcessTable(), root, set())

    assert signals == [signal.SIGTERM]


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _strong_identity(process_table: DarwinProcessTable, pid: int) -> LifecycleProcessIdentity:
    scan = process_table.identity(pid)
    assert scan.complete and scan.value is not None
    return scan.value


def _wait_strong_identity_gone(
    process_table: DarwinProcessTable,
    identity: LifecycleProcessIdentity,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        scan = process_table.identity(identity.pid)
        if scan.complete and scan.value != identity:
            return True
        time.sleep(0.025)
    return False


def _token_pipe(token: str) -> int:
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    try:
        os.write(write_fd, token.encode("ascii"))
    finally:
        os.close(write_fd)
    return read_fd


def _request_json(port: int, method: str, path: str, token: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request(
            method,
            path,
            body=b"" if method == "POST" else None,
            headers={"X-PDF2MD-Session": token, "Connection": "close"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_generated_launcher_normal_shutdown_reaps_detached_grandchild(tmp_path):
    app = _executable_bundle(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.set_inheritable(True)
    port = listener.getsockname()[1]
    token = "normal-shutdown-session-token-0123456789abcdef"
    token_fd = _token_pipe(token)
    process = subprocess.Popen(
        [
            "/bin/bash",
            str(app / "Contents/MacOS/python3"),
            "--parent-pid",
            str(os.getpid()),
            "--socket-fd",
            str(listener.fileno()),
            "--session-token-fd",
            str(token_fd),
        ],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(listener.fileno(), token_fd),
        start_new_session=True,
    )
    listener.close()
    os.close(token_fd)
    detached_pid = None
    try:
        ready = json.loads(process.stdout.readline())
        assert ready["port"] == port
        detached_path = home / "fixture-detached.json"
        deadline = time.monotonic() + 5
        while not detached_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        detached_pid = int(json.loads(detached_path.read_text())["pid"])
        assert _request_json(port, "GET", "/health", token) == (200, {"status": "ok"})
        assert _request_json(port, "POST", "/shutdown", token) == (
            200,
            {"status": "shutting_down"},
        )
        return_code = process.wait(timeout=5)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        assert return_code == 0, stderr
        assert not _pid_exists(detached_pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        if detached_pid is not None and _pid_exists(detached_pid):
            os.kill(detached_pid, signal.SIGKILL)


@pytest.mark.parametrize("phase", ["before_ack", "after_ack"])
def test_watchdog_exit_around_arm_reaps_bound_sidecar(tmp_path, phase):
    source_root = ROOT / "src"
    supervisor_script = tmp_path / "arm-supervisor.py"
    owner_script = tmp_path / "arm-owner.py"
    supervisor_pid_path = tmp_path / "supervisor.pid"
    watchdog_pid_path = tmp_path / "watchdog.pid"
    sidecar_pid_path = tmp_path / "sidecar.pid"
    arm_hook_path = tmp_path / "arm.hook"
    sidecar_exec_path = tmp_path / "sidecar.exec"
    owner_ready_path = tmp_path / "owner.ready"
    supervisor_exit_path = tmp_path / "supervisor.exit"
    release_hook_path = tmp_path / "release.hook"
    supervisor_script.write_text(
        """import os
import signal
import sys
import time
from pathlib import Path

(
    source_root,
    parent_pid,
    socket_fd,
    token_fd,
    phase,
    watchdog_path,
    sidecar_path,
    arm_hook_path,
    exec_path,
    release_path,
) = sys.argv[1:]
sys.path.insert(0, source_root)
import parsing_core.serving.lifecycle as lifecycle

real_watchdog = lifecycle._watchdog
def instrumented_watchdog(
    parent_identity,
    supervisor_identity,
    marker,
    control_fd,
    ready_fd,
    acknowledgement_fd,
):
    real_write = lifecycle.os.write
    def instrumented_write(fd, value):
        if fd == acknowledgement_fd and value == b"A":
            if phase == "before_ack":
                Path(arm_hook_path).touch()
            else:
                written = real_write(fd, value)
                Path(arm_hook_path).touch()
            while not Path(release_path).exists():
                time.sleep(0.001)
            if phase == "before_ack":
                return real_write(fd, value)
            return written
        return real_write(fd, value)
    lifecycle.os.write = instrumented_write
    return real_watchdog(
        parent_identity,
        supervisor_identity,
        marker,
        control_fd,
        ready_fd,
        acknowledgement_fd,
    )

real_fork = lifecycle.os.fork
fork_count = 0
def tracked_fork():
    global fork_count
    fork_count += 1
    pid = real_fork()
    if pid > 0 and fork_count == 1:
        Path(watchdog_path).write_text(str(pid), encoding="utf-8")
    elif pid > 0 and fork_count == 2:
        Path(sidecar_path).write_text(str(pid), encoding="utf-8")
    return pid

real_execve = lifecycle.os.execve
def fixture_execve(path, arguments, environment):
    Path(exec_path).touch()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    real_execve("/bin/sleep", ["sleep", "30"], environment)

lifecycle._watchdog = instrumented_watchdog
lifecycle.os.fork = tracked_fork
lifecycle.os.execve = fixture_execve
raise SystemExit(lifecycle.supervise(
    parent_pid=int(parent_pid),
    socket_fd=int(socket_fd),
    session_token_fd=int(token_fd),
    serve_arguments=[],
))
""",
        encoding="utf-8",
    )
    owner_script.write_text(
        """import os
import subprocess
import sys
import time
from pathlib import Path

(
    supervisor_script,
    source_root,
    phase,
    supervisor_path,
    watchdog_path,
    sidecar_path,
    arm_hook_path,
    exec_path,
    ready_path,
    exit_path,
    release_path,
) = sys.argv[1:]
socket_read, socket_write = os.pipe()
token_read, token_write = os.pipe()
os.set_inheritable(socket_read, True)
os.set_inheritable(token_read, True)
os.write(token_write, b"arm-race-session-token")
os.close(token_write)
os.close(socket_write)
child = subprocess.Popen(
    [
        sys.executable,
        supervisor_script,
        source_root,
        str(os.getpid()),
        str(socket_read),
        str(token_read),
        phase,
        watchdog_path,
        sidecar_path,
        arm_hook_path,
        exec_path,
        release_path,
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    pass_fds=(socket_read, token_read),
)
os.close(socket_read)
os.close(token_read)
Path(supervisor_path).write_text(str(child.pid), encoding="utf-8")
deadline = time.monotonic() + 6
required = [Path(watchdog_path), Path(sidecar_path), Path(arm_hook_path)]
if phase == "after_ack":
    required.append(Path(exec_path))
while time.monotonic() < deadline:
    if all(path.exists() for path in required):
        Path(ready_path).touch()
        break
    if child.poll() is not None:
        raise SystemExit(2)
    time.sleep(0.005)
else:
    child.kill()
    raise SystemExit(3)
try:
    return_code = child.wait(timeout=10)
except subprocess.TimeoutExpired:
    child.kill()
    child.wait(timeout=5)
    raise SystemExit(4)
Path(exit_path).write_text(str(return_code), encoding="utf-8")
""",
        encoding="utf-8",
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            str(owner_script),
            str(supervisor_script),
            str(source_root),
            phase,
            str(supervisor_pid_path),
            str(watchdog_pid_path),
            str(sidecar_pid_path),
            str(arm_hook_path),
            str(sidecar_exec_path),
            str(owner_ready_path),
            str(supervisor_exit_path),
            str(release_hook_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process_table = DarwinProcessTable()
    identities = []
    try:
        deadline = time.monotonic() + 7
        while not owner_ready_path.exists() and time.monotonic() < deadline:
            if owner.poll() is not None:
                pytest.fail("owner exited before the watchdog ARM race was reached")
            time.sleep(0.005)
        assert owner_ready_path.exists()
        supervisor_identity = _strong_identity(
            process_table,
            int(supervisor_pid_path.read_text(encoding="utf-8")),
        )
        watchdog_identity = _strong_identity(
            process_table,
            int(watchdog_pid_path.read_text(encoding="utf-8")),
        )
        sidecar_identity = _strong_identity(
            process_table,
            int(sidecar_pid_path.read_text(encoding="utf-8")),
        )
        identities = [sidecar_identity, watchdog_identity, supervisor_identity]

        if phase == "before_ack":
            assert not sidecar_exec_path.exists()
        else:
            assert sidecar_exec_path.exists()
        os.kill(watchdog_identity.pid, signal.SIGKILL)

        assert owner.wait(timeout=10) == 0
        assert supervisor_exit_path.read_text(encoding="utf-8") == "70"
        assert _wait_strong_identity_gone(process_table, sidecar_identity)
        assert _wait_strong_identity_gone(process_table, watchdog_identity)
        assert _wait_strong_identity_gone(process_table, supervisor_identity)
    finally:
        release_hook_path.touch()
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        for identity in identities:
            scan = process_table.identity(identity.pid)
            if not scan.complete or scan.value != identity:
                continue
            try:
                os.kill(identity.pid, signal.SIGCONT)
                os.kill(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_parent_death_reaps_sidecar_paused_between_fork_and_exec(tmp_path):
    source_root = ROOT / "src"
    marker = "f" * 64
    previous_marker = "preexisting-owner-marker"
    supervisor_script = tmp_path / "paused-supervisor.py"
    owner_script = tmp_path / "paused-owner.py"
    supervisor_pid_path = tmp_path / "supervisor.pid"
    watchdog_pid_path = tmp_path / "watchdog.pid"
    sidecar_pid_path = tmp_path / "sidecar.pid"
    child_marker_path = tmp_path / "sidecar.marker"
    child_ready_path = tmp_path / "sidecar.ready"
    owner_ready_path = tmp_path / "owner.ready"
    release_child_path = tmp_path / "release-child"
    supervisor_script.write_text(
        """import os
import sys
import time
from pathlib import Path

(
    source_root,
    parent_pid,
    socket_fd,
    token_fd,
    marker,
    watchdog_path,
    sidecar_path,
    child_marker_path,
    child_ready_path,
    release_child_path,
) = sys.argv[1:]
sys.path.insert(0, source_root)
import parsing_core.serving.lifecycle as lifecycle

lifecycle.secrets.token_hex = lambda size: marker
real_has_exact_marker = lifecycle.DarwinProcessTable.has_exact_marker

def hide_generated_marker(self, pid, value, marker_environment=lifecycle.OWNER_MARKER_ENV):
    if value == marker and marker_environment == lifecycle.OWNER_MARKER_ENV:
        return lifecycle.ScanResult(False, complete=True)
    return real_has_exact_marker(self, pid, value, marker_environment)

lifecycle.DarwinProcessTable.has_exact_marker = hide_generated_marker
real_fork = lifecycle.os.fork
fork_count = 0

def tracked_fork():
    global fork_count
    fork_count += 1
    pid = real_fork()
    if fork_count == 1 and pid > 0:
        Path(watchdog_path).write_text(str(pid), encoding="utf-8")
    elif fork_count == 2 and pid == 0:
        Path(sidecar_path).write_text(str(os.getpid()), encoding="utf-8")
        Path(child_marker_path).write_text(
            os.environ.get(lifecycle.OWNER_MARKER_ENV, ""),
            encoding="utf-8",
        )
        Path(child_ready_path).touch()
        while not Path(release_child_path).exists():
            time.sleep(0.001)
    return pid

lifecycle.os.fork = tracked_fork
raise SystemExit(lifecycle.supervise(
    parent_pid=int(parent_pid),
    socket_fd=int(socket_fd),
    session_token_fd=int(token_fd),
    serve_arguments=[],
))
""",
        encoding="utf-8",
    )
    owner_script.write_text(
        """import os
import subprocess
import sys
import time
from pathlib import Path

(
    supervisor_script,
    source_root,
    marker,
    supervisor_path,
    watchdog_path,
    sidecar_path,
    child_marker_path,
    child_ready_path,
    owner_ready_path,
    release_child_path,
    previous_marker,
) = sys.argv[1:]
socket_read, socket_write = os.pipe()
token_read, token_write = os.pipe()
os.set_inheritable(socket_read, True)
os.set_inheritable(token_read, True)
os.write(token_write, b"fork-exec-race-token")
os.close(token_write)
os.close(socket_write)
child = subprocess.Popen(
    [
        sys.executable,
        supervisor_script,
        source_root,
        str(os.getpid()),
        str(socket_read),
        str(token_read),
        marker,
        watchdog_path,
        sidecar_path,
        child_marker_path,
        child_ready_path,
        release_child_path,
    ],
    env=os.environ | {"PDF2MD_OWNER_MARKER": previous_marker},
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    pass_fds=(socket_read, token_read),
)
os.close(socket_read)
os.close(token_read)
Path(supervisor_path).write_text(str(child.pid), encoding="utf-8")
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if Path(watchdog_path).exists() and Path(child_ready_path).exists():
        break
    if child.poll() is not None:
        raise SystemExit(2)
    time.sleep(0.005)
else:
    raise SystemExit(3)
Path(owner_ready_path).touch()
while True:
    time.sleep(30)
""",
        encoding="utf-8",
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            str(owner_script),
            str(supervisor_script),
            str(source_root),
            marker,
            str(supervisor_pid_path),
            str(watchdog_pid_path),
            str(sidecar_pid_path),
            str(child_marker_path),
            str(child_ready_path),
            str(owner_ready_path),
            str(release_child_path),
            previous_marker,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    process_table = DarwinProcessTable()
    identities = []
    marker_scans = {}
    gone = {}
    try:
        deadline = time.monotonic() + 5
        while not owner_ready_path.exists() and time.monotonic() < deadline:
            if owner.poll() is not None:
                detail = owner.stderr.read().decode("utf-8", errors="replace")
                pytest.fail(f"paused sidecar owner exited before readiness: {detail}")
            time.sleep(0.005)
        assert owner_ready_path.exists()
        supervisor_identity = _strong_identity(
            process_table,
            int(supervisor_pid_path.read_text(encoding="utf-8")),
        )
        watchdog_identity = _strong_identity(
            process_table,
            int(watchdog_pid_path.read_text(encoding="utf-8")),
        )
        sidecar_identity = _strong_identity(
            process_table,
            int(sidecar_pid_path.read_text(encoding="utf-8")),
        )
        identities = [sidecar_identity, watchdog_identity, supervisor_identity]
        marker_scans = {
            "sidecar": process_table.has_exact_marker(sidecar_identity.pid, marker),
            "supervisor": process_table.has_exact_marker(supervisor_identity.pid, marker),
            "restored": process_table.has_exact_marker(
                supervisor_identity.pid,
                previous_marker,
            ),
        }

        owner.kill()
        assert owner.wait(timeout=5) == -signal.SIGKILL
        gone = {
            "sidecar": _wait_strong_identity_gone(process_table, sidecar_identity),
            "watchdog": _wait_strong_identity_gone(process_table, watchdog_identity),
            "supervisor": _wait_strong_identity_gone(process_table, supervisor_identity),
        }

        assert child_marker_path.read_text(encoding="utf-8") == marker
        assert marker_scans["sidecar"].complete
        assert marker_scans["supervisor"] == ScanResult(False, complete=True)
        assert marker_scans["restored"] == ScanResult(True, complete=True)
        assert gone == {"sidecar": True, "watchdog": True, "supervisor": True}
    finally:
        release_child_path.touch()
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        for identity in identities:
            scan = process_table.identity(identity.pid)
            if not scan.complete or scan.value != identity:
                continue
            try:
                os.kill(identity.pid, signal.SIGCONT)
                os.kill(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if owner.stderr is not None:
            owner.stderr.close()


@pytest.mark.parametrize("round_number", range(3))
def test_generated_launcher_reaps_fast_double_fork_after_parent_death(tmp_path, round_number):
    app = _executable_bundle(tmp_path)
    wrapper_pid_path = tmp_path / "wrapper.pid"
    sidecar_pid_path = tmp_path / "sidecar.pid"
    descendant_pid_path = tmp_path / "descendant.json"
    owner = tmp_path / "owner.py"
    owner.write_text(
        """import os
import socket
import subprocess
import sys
import time
from pathlib import Path

launcher, wrapper_path, sidecar_path, descendant_path, home = sys.argv[1:]
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen()
listener.set_inheritable(True)
environment = {
    "HOME": home,
    "PATH": "/usr/bin:/bin",
    "PDF2MD_FIXTURE_SIDECAR_PID": sidecar_path,
    "PDF2MD_FIXTURE_DESCENDANT_PID": descendant_path,
}
read_fd, write_fd = os.pipe()
os.set_inheritable(read_fd, True)
os.write(write_fd, b"watchdog-session-token-0123456789abcdef")
os.close(write_fd)
child = subprocess.Popen(
    [
        "/bin/bash",
        launcher,
        "--parent-pid",
        str(os.getpid()),
        "--socket-fd",
        str(listener.fileno()),
        "--session-token-fd",
        str(read_fd),
    ],
    env=environment,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    pass_fds=(listener.fileno(), read_fd),
    start_new_session=True,
)
listener.close()
os.close(read_fd)
Path(wrapper_path).write_text(str(child.pid))
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if Path(sidecar_path).exists() and Path(descendant_path).exists():
        break
    if child.poll() is not None:
        raise SystemExit(2)
    time.sleep(0.01)
else:
    child.kill()
    raise SystemExit(3)
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(owner),
            str(app / "Contents/MacOS/python3"),
            str(wrapper_pid_path),
            str(sidecar_pid_path),
            str(descendant_pid_path),
            str(home),
        ],
        timeout=10,
    )
    assert result.returncode == 0
    wrapper_pid = int(wrapper_pid_path.read_text(encoding="utf-8"))
    sidecar_pid = int(sidecar_pid_path.read_text(encoding="utf-8"))
    descendant = json.loads(descendant_pid_path.read_text(encoding="utf-8"))
    descendant_pid = int(descendant["pid"])
    try:
        assert descendant["sid"] == descendant["intermediate"]
        assert descendant["pgid"] == descendant["intermediate"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            _pid_exists(pid) for pid in (wrapper_pid, sidecar_pid, descendant_pid)
        ):
            time.sleep(0.05)
        assert not _pid_exists(wrapper_pid)
        assert not _pid_exists(sidecar_pid)
        assert not _pid_exists(descendant_pid)
    finally:
        for pid in (descendant_pid, sidecar_pid, wrapper_pid):
            if _pid_exists(pid):
                os.kill(pid, signal.SIGKILL)
