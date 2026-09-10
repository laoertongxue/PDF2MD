import ctypes
import errno
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

import parsing_core.serving.lifecycle as lifecycle
from parsing_core.serving.lifecycle import (
    DarwinProcessTable,
    ProcessIdentity,
    ScanResult,
    cleanup_marked_processes,
    supervise,
)


def _table_without_initialization() -> DarwinProcessTable:
    return object.__new__(DarwinProcessTable)


def _identity(pid: int) -> ProcessIdentity:
    return ProcessIdentity(pid, 501, 1_700_000_000, pid)


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(KeyboardInterrupt("interrupt"), 130), (SystemExit(23), 23)],
)
def test_lifecycle_safe_main_contains_base_exception_without_traceback(
    monkeypatch,
    failure,
    expected_status,
):
    audit = []

    class Stderr:
        def write(self, value):
            audit.append(value)

        def flush(self):
            audit.append("flush")

    monkeypatch.setattr(lifecycle.sys, "stderr", Stderr())

    def entrypoint():
        raise failure

    assert lifecycle._safe_main(entrypoint) == expected_status
    output = "".join(value for value in audit if value != "flush")
    assert output == "sidecar lifecycle failed\n"
    assert "Traceback" not in output
    assert "KeyboardInterrupt" not in output


def test_lifecycle_module_sigint_exits_130_without_traceback_or_absolute_path():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.set_inheritable(True)
    token_read_fd, token_write_fd = os.pipe()
    os.set_inheritable(token_read_fd, True)
    os.write(token_write_fd, b"lifecycle-sigint-session-token-0123456789abcdef")
    os.close(token_write_fd)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "parsing_core.serving.lifecycle",
            "--parent-pid",
            str(os.getpid()),
            "--socket-fd",
            str(listener.fileno()),
            "--session-token-fd",
            str(token_read_fd),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(listener.fileno(), token_read_fd),
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    listener.close()
    os.close(token_read_fd)
    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 10)
        assert readable, "lifecycle sidecar did not emit ready output"
        ready = json.loads(process.stdout.readline())
        assert ready["host"] == "127.0.0.1"

        os.kill(process.pid, signal.SIGINT)
        _stdout, stderr = process.communicate(timeout=10)

        assert process.returncode == 130
        output = stderr.decode("utf-8", errors="replace")
        assert output == "sidecar lifecycle failed\n"
        assert "Traceback" not in output
        assert "KeyboardInterrupt" not in output
        assert str(lifecycle.__file__) not in output
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()


def _run_fake_supervisor(
    monkeypatch,
    fork,
    *,
    acknowledgement: bytes = b"C",
    sidecar_status: int = 0,
    watchdog_status: int = 0,
    on_waitpid=None,
):
    sidecar_pid = 802
    sidecar_has_reaped = False

    class SupervisorTable:
        def identity(self, pid):
            if pid == sidecar_pid and sidecar_has_reaped:
                return ScanResult(None, complete=True)
            return ScanResult(_identity(pid), complete=True)

        def child_identities(self, parent_identity, excluded_pid):
            assert parent_identity == _identity(456)
            assert excluded_pid == 801
            return ScanResult({_identity(sidecar_pid)}, complete=True)

        def same_direct_child(self, parent_identity, child_identity):
            assert parent_identity == _identity(456)
            assert child_identity == _identity(sidecar_pid)
            return ScanResult(True, complete=True)

        def marker_identities(self, *args, **kwargs):
            return ScanResult(set(), complete=True)

    pipe_pairs = iter(((10, 11), (12, 13), (14, 15), (16, 17)))
    replies = iter((b"R", b"A", acknowledgement))
    watchdog_polls = 0

    def waitpid(pid, options):
        nonlocal sidecar_has_reaped, watchdog_polls
        assert options == os.WNOHANG
        if on_waitpid is not None:
            on_waitpid(pid)
        if pid == sidecar_pid:
            sidecar_has_reaped = True
            return pid, sidecar_status
        assert pid == 801
        watchdog_polls += 1
        if watchdog_polls < 3:
            return 0, 0
        return pid, watchdog_status

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", SupervisorTable)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 456)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 123)
    monkeypatch.setattr(lifecycle.os, "pipe", lambda: next(pipe_pairs))
    monkeypatch.setattr(lifecycle.os, "fork", fork)
    monkeypatch.setattr(lifecycle.os, "close", lambda fd: None)
    monkeypatch.setattr(lifecycle.os, "write", lambda fd, value: len(value))
    monkeypatch.setattr(lifecycle.os, "waitpid", waitpid)
    monkeypatch.setattr(lifecycle, "_wait_for_byte", lambda fd, timeout: next(replies))
    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda size: "f" * 64)

    return supervise(
        parent_pid=123,
        socket_fd=7,
        session_token_fd=8,
        serve_arguments=[],
    )


def test_all_process_queries_failing_is_an_incomplete_scan(monkeypatch):
    table = _table_without_initialization()
    monkeypatch.setattr(table, "pids", lambda: ScanResult([], complete=False))

    result = table.marker_identities("a" * 64, excluded_pid=999)

    assert result == ScanResult(set(), complete=False)


def test_one_pid_query_failure_keeps_matches_but_marks_scan_incomplete(monkeypatch):
    table = _table_without_initialization()
    first = _identity(101)
    calls = {101: 0}
    monkeypatch.setattr(table, "pids", lambda: ScanResult([101, 102], complete=True))
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", lambda pid, sig: None)

    def identity(pid):
        if pid == 102:
            return ScanResult(None, complete=False)
        calls[pid] += 1
        return ScanResult(first, complete=True)

    monkeypatch.setattr(table, "identity", identity)
    monkeypatch.setattr(
        table,
        "has_exact_marker",
        lambda pid, marker, marker_environment: ScanResult(True, complete=True),
    )
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.getuid", lambda: 501)

    result = table.marker_identities("b" * 64, excluded_pid=999)

    assert result == ScanResult({first}, complete=False)
    assert calls == {101: 2}


def test_permission_denied_unowned_pid_is_not_a_marker_candidate(monkeypatch):
    table = _table_without_initialization()
    monkeypatch.setattr(table, "pids", lambda: ScanResult([102], complete=True))
    monkeypatch.setattr(table, "identity", lambda pid: ScanResult(None, complete=False))

    def deny_signal(_pid, _signal):
        raise PermissionError

    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", deny_signal)

    assert table.marker_identities("b" * 64, excluded_pid=999) == ScanResult(set(), complete=True)


def test_preexisting_uninspectable_pid_baseline_does_not_block_marker_discovery(monkeypatch):
    table = _table_without_initialization()
    protected_pid = 102
    queue = _FakeKqueue()
    monkeypatch.setattr(table, "pids", lambda: ScanResult([protected_pid], complete=True))
    monkeypatch.setattr(table, "identity", lambda pid: ScanResult(None, complete=False))
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", lambda pid, sig: None)
    monkeypatch.setattr("parsing_core.serving.lifecycle.select.kqueue", lambda: queue)
    monkeypatch.setattr(
        table,
        "has_exact_marker",
        lambda *args: pytest.fail("an uninspectable baseline PID must not expose procargs"),
    )

    baseline = table.uninspectable_pid_baseline()
    result = table.marker_identities(
        "b" * 64,
        excluded_pid=999,
        uninspectable_pid_baseline=baseline.value,
    )

    assert baseline.complete
    assert baseline.value is not None
    assert baseline.value.active_pids() == ScanResult(
        frozenset({protected_pid}),
        complete=True,
    )
    assert result == ScanResult(set(), complete=True)
    baseline.value.close()


def test_new_uninspectable_pid_after_baseline_keeps_scan_incomplete(monkeypatch):
    table = _table_without_initialization()
    baseline_pid = 102
    new_pid = 103
    monkeypatch.setattr(
        table,
        "pids",
        lambda: ScanResult([baseline_pid, new_pid], complete=True),
    )
    monkeypatch.setattr(table, "identity", lambda pid: ScanResult(None, complete=False))
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", lambda pid, sig: None)

    class Baseline:
        def active_pids(self):
            return ScanResult(frozenset({baseline_pid}), complete=True)

    result = table.marker_identities(
        "b" * 64,
        excluded_pid=999,
        uninspectable_pid_baseline=Baseline(),
    )

    assert result == ScanResult(set(), complete=False)


def test_baseline_pid_that_becomes_inspectable_is_checked_for_marker(monkeypatch):
    table = _table_without_initialization()
    candidate = _identity(102)
    identity_calls = 0
    monkeypatch.setattr(table, "pids", lambda: ScanResult([candidate.pid], complete=True))

    def identity(pid):
        nonlocal identity_calls
        identity_calls += 1
        assert pid == candidate.pid
        return ScanResult(candidate, complete=True)

    monkeypatch.setattr(table, "identity", identity)
    monkeypatch.setattr(
        table,
        "has_exact_marker",
        lambda pid, marker, marker_environment: ScanResult(True, complete=True),
    )
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.getuid", lambda: 501)

    class Baseline:
        def active_pids(self):
            return ScanResult(frozenset({candidate.pid}), complete=True)

    result = table.marker_identities(
        "b" * 64,
        excluded_pid=999,
        uninspectable_pid_baseline=Baseline(),
    )

    assert result == ScanResult({candidate}, complete=True)
    assert identity_calls == 2


class _FakeKqueue:
    def __init__(self):
        self.events = []
        self.registrations = []
        self.closed = False

    def control(self, changes, max_events, timeout):
        if changes is not None:
            self.registrations.extend(changes)
            return []
        events, self.events = self.events, []
        return events[:max_events]

    def close(self):
        self.closed = True


def test_uninspectable_baseline_close_is_retryable_after_close_failure():
    class RetryableQueue:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("injected queue close failure")

    queue = RetryableQueue()
    baseline = lifecycle.UninspectableProcessBaseline(queue, set())

    with pytest.raises(OSError, match="injected queue close failure"):
        baseline.close()

    assert not baseline._closed
    baseline.close()
    assert baseline._closed
    assert queue.close_calls == 2


def test_baseline_exit_event_revokes_pid_exemption_before_reuse(monkeypatch):
    table = _table_without_initialization()
    candidate_pid = 10_000 + (id(table) % 10_000)
    queue = _FakeKqueue()
    monkeypatch.setattr(table, "pids", lambda: ScanResult([candidate_pid], complete=True))
    monkeypatch.setattr(table, "identity", lambda pid: ScanResult(None, complete=False))
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", lambda pid, sig: None)
    monkeypatch.setattr("parsing_core.serving.lifecycle.select.kqueue", lambda: queue)

    baseline_scan = table.uninspectable_pid_baseline()
    baseline = baseline_scan.value

    assert baseline_scan.complete
    assert baseline.active_pids() == ScanResult(frozenset({candidate_pid}), complete=True)
    assert table.marker_identities(
        "b" * 64,
        excluded_pid=999,
        uninspectable_pid_baseline=baseline,
    ) == ScanResult(set(), complete=True)

    queue.events.append(
        SimpleNamespace(
            ident=candidate_pid,
            filter=select.KQ_FILTER_PROC,
            fflags=select.KQ_NOTE_EXIT,
            flags=0,
            data=0,
        )
    )

    assert table.marker_identities(
        "b" * 64,
        excluded_pid=999,
        uninspectable_pid_baseline=baseline,
    ) == ScanResult(set(), complete=False)
    baseline.close()
    assert queue.closed


def test_incomplete_pid_list_prevents_baseline_capture(monkeypatch):
    table = _table_without_initialization()
    monkeypatch.setattr(table, "pids", lambda: ScanResult([102], complete=False))
    monkeypatch.setattr(
        table,
        "identity",
        lambda pid: pytest.fail("an incomplete PID list must not be trusted"),
    )

    assert table.uninspectable_pid_baseline() == ScanResult(None, complete=False)


def test_child_scan_is_incomplete_when_parent_identity_has_changed(monkeypatch):
    table = _table_without_initialization()
    parent = _identity(123)
    replacement = ProcessIdentity(
        parent.pid,
        parent.uid,
        parent.started_seconds + 1,
        parent.started_microseconds,
    )
    monkeypatch.setattr(
        table,
        "identity",
        lambda pid: ScanResult(replacement, complete=True),
    )
    monkeypatch.setattr(
        table,
        "pids",
        lambda: pytest.fail("a changed parent must stop relationship capture"),
    )

    assert table.child_identities(parent, excluded_pid=999) == ScanResult(
        set(),
        complete=False,
    )


def test_supervise_does_not_inherit_a_pre_fork_process_baseline(monkeypatch):
    class SupervisorTable:
        def uninspectable_pid_baseline(self):
            pytest.fail("the kqueue baseline must be created inside the watchdog")

        def identity(self, pid):
            return ScanResult(_identity(pid), complete=True)

    monkeypatch.setattr(
        "parsing_core.serving.lifecycle.DarwinProcessTable",
        SupervisorTable,
    )
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.getpid", lambda: 456)
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.getppid", lambda: 123)
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.pipe", lambda: (10, 11))
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.close", lambda fd: None)
    monkeypatch.setattr(
        "parsing_core.serving.lifecycle.os.fork",
        lambda: (_ for _ in ()).throw(RuntimeError("watchdog fork reached")),
    )

    with pytest.raises(RuntimeError, match="watchdog fork reached"):
        supervise(
            parent_pid=123,
            socket_fd=7,
            session_token_fd=8,
            serve_arguments=[],
        )


@pytest.mark.parametrize("previous_marker", [None, "preexisting-owner-marker"])
def test_sidecar_marker_exists_during_fork_and_supervisor_restores_environment(
    monkeypatch,
    previous_marker,
):
    marker = "f" * 64
    if previous_marker is None:
        monkeypatch.delenv(lifecycle.OWNER_MARKER_ENV, raising=False)
    else:
        monkeypatch.setenv(lifecycle.OWNER_MARKER_ENV, previous_marker)
    observed_at_fork = []
    fork_results = iter((801, 802))

    def fork():
        observed_at_fork.append(os.environ.get(lifecycle.OWNER_MARKER_ENV))
        return next(fork_results)

    def assert_parent_environment_restored(pid):
        assert pid in {801, 802}
        assert os.environ.get(lifecycle.OWNER_MARKER_ENV) == previous_marker

    assert (
        _run_fake_supervisor(
            monkeypatch,
            fork,
            on_waitpid=assert_parent_environment_restored,
        )
        == 0
    )
    assert observed_at_fork == [previous_marker, marker]
    assert os.environ.get(lifecycle.OWNER_MARKER_ENV) == previous_marker


@pytest.mark.parametrize("previous_marker", [None, "preexisting-owner-marker"])
def test_sidecar_fork_failure_restores_owner_marker(monkeypatch, previous_marker):
    marker = "f" * 64
    if previous_marker is None:
        monkeypatch.delenv(lifecycle.OWNER_MARKER_ENV, raising=False)
    else:
        monkeypatch.setenv(lifecycle.OWNER_MARKER_ENV, previous_marker)
    observed_at_fork = []

    def fork():
        observed_at_fork.append(os.environ.get(lifecycle.OWNER_MARKER_ENV))
        if len(observed_at_fork) == 1:
            return 801
        raise RuntimeError("sidecar fork failed")

    with pytest.raises(RuntimeError, match="sidecar fork failed"):
        _run_fake_supervisor(monkeypatch, fork)

    assert observed_at_fork == [previous_marker, marker]
    assert os.environ.get(lifecycle.OWNER_MARKER_ENV) == previous_marker


@pytest.mark.parametrize("sidecar_status", [17 << 8, int(signal.SIGTERM)])
@pytest.mark.parametrize(
    ("acknowledgement", "watchdog_status"),
    [(b"E", 0), (b"C", 70 << 8)],
)
def test_watchdog_cleanup_failure_overrides_nonzero_or_signaled_sidecar_status(
    monkeypatch,
    sidecar_status,
    acknowledgement,
    watchdog_status,
):
    fork_results = iter((801, 802))

    assert (
        _run_fake_supervisor(
            monkeypatch,
            lambda: next(fork_results),
            acknowledgement=acknowledgement,
            sidecar_status=sidecar_status,
            watchdog_status=watchdog_status,
        )
        == 70
    )


def test_watchdog_creates_and_closes_its_own_process_baseline(monkeypatch):
    calls = []

    class Baseline:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            calls.append("baseline_closed")

    baseline = Baseline()
    parent = _identity(123)
    supervisor = _identity(456)

    class WatchdogTable:
        def uninspectable_pid_baseline(self):
            calls.append("baseline_created")
            return ScanResult(baseline, complete=True)

        def identity(self, pid):
            assert pid == parent.pid
            return ScanResult(parent, complete=True)

    class Queue:
        def control(self, changes, max_events, timeout):
            if changes is not None:
                calls.append("watch_registered")
                return []
            return [
                SimpleNamespace(
                    ident=20,
                    filter=select.KQ_FILTER_READ,
                    fflags=0,
                    flags=0,
                )
            ]

        def close(self):
            calls.append("queue_closed")

    class WatchdogExit(Exception):
        pass

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", WatchdogTable)
    monkeypatch.setattr(lifecycle.select, "kqueue", Queue)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda process_table, marker, watchdog_pid, **kwargs: (
            calls.append("cleanup"),
            kwargs["uninspectable_pid_baseline"] is baseline,
        )[1],
    )
    monkeypatch.setattr(
        lifecycle.os,
        "write",
        lambda fd, value: calls.append(("write", fd, value)) or len(value),
    )
    monkeypatch.setattr(lifecycle.os, "read", lambda fd, size: b"")
    monkeypatch.setattr(lifecycle.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(
        lifecycle.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(WatchdogExit(code)),
    )

    with pytest.raises(WatchdogExit):
        lifecycle._watchdog(
            parent,
            supervisor,
            "a" * 64,
            control_fd=20,
            ready_fd=21,
            acknowledgement_fd=22,
        )

    assert baseline.closed
    assert calls.index("baseline_created") < calls.index(("write", 21, b"R"))
    assert "cleanup" in calls


def test_watchdog_closes_local_baseline_if_kqueue_creation_fails(monkeypatch):
    class Baseline:
        closed = False

        def close(self):
            self.closed = True

    baseline = Baseline()

    class WatchdogTable:
        def uninspectable_pid_baseline(self):
            return ScanResult(baseline, complete=True)

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", WatchdogTable)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        lifecycle.select,
        "kqueue",
        lambda: (_ for _ in ()).throw(OSError("kqueue unavailable")),
    )

    with pytest.raises(OSError, match="kqueue unavailable"):
        lifecycle._watchdog(
            _identity(123),
            _identity(456),
            "a" * 64,
            control_fd=20,
            ready_fd=21,
            acknowledgement_fd=22,
        )

    assert baseline.closed


def test_watchdog_requires_arm_before_normal_shutdown(monkeypatch):
    calls = []
    parent = _identity(123)
    sidecar = _identity(789)

    class Baseline:
        def close(self):
            calls.append("baseline_closed")

    class WatchdogTable:
        def uninspectable_pid_baseline(self):
            return ScanResult(Baseline(), complete=True)

        def identity(self, pid):
            if pid == parent.pid:
                return ScanResult(parent, complete=True)
            assert pid == sidecar.pid
            return ScanResult(sidecar, complete=True)

        def marker_identities(self, *args, **kwargs):
            return ScanResult({sidecar}, complete=True)

        def same_process_with_marker(self, identity, marker):
            assert identity == sidecar
            return ScanResult(True, complete=True)

        def child_identities(self, parent_identity, excluded_pid):
            assert parent_identity == _identity(456)
            return ScanResult({sidecar}, complete=True)

        def same_direct_child(self, parent_identity, child_identity):
            assert parent_identity == _identity(456)
            assert child_identity == sidecar
            return ScanResult(True, complete=True)

    control_event = SimpleNamespace(
        ident=20,
        filter=select.KQ_FILTER_READ,
        fflags=0,
        flags=0,
    )

    class Queue:
        def __init__(self):
            self.events = iter(([control_event], [control_event]))

        def control(self, changes, max_events, timeout):
            if changes is not None:
                return []
            return next(self.events)

        def close(self):
            calls.append("queue_closed")

    class WatchdogExit(Exception):
        pass

    control_messages = iter((lifecycle._encode_watchdog_arm(sidecar), b"N"))
    monkeypatch.setattr(lifecycle, "DarwinProcessTable", WatchdogTable)
    monkeypatch.setattr(lifecycle.select, "kqueue", Queue)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda *args: None)
    monkeypatch.setattr(lifecycle.os, "read", lambda fd, size: next(control_messages))
    monkeypatch.setattr(
        lifecycle.os,
        "write",
        lambda fd, value: calls.append(("write", fd, value)) or len(value),
    )
    monkeypatch.setattr(lifecycle.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda *args, **kwargs: calls.append("cleanup") or True,
    )
    monkeypatch.setattr(
        lifecycle.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(WatchdogExit(code)),
    )

    with pytest.raises(WatchdogExit) as captured:
        lifecycle._watchdog(
            parent,
            _identity(456),
            "a" * 64,
            control_fd=20,
            ready_fd=21,
            acknowledgement_fd=22,
        )

    assert captured.value.args == (0,)
    assert [call for call in calls if isinstance(call, tuple) and call[0] == "write"] == [
        ("write", 21, b"R"),
        ("write", 22, b"A"),
        ("write", 22, b"C"),
    ]
    assert "cleanup" in calls


def test_watchdog_control_eof_triggers_cleanup_and_failure(monkeypatch):
    calls = []
    parent = _identity(123)

    class Baseline:
        def close(self):
            calls.append("baseline_closed")

    class WatchdogTable:
        def uninspectable_pid_baseline(self):
            return ScanResult(Baseline(), complete=True)

        def identity(self, pid):
            assert pid == parent.pid
            return ScanResult(parent, complete=True)

    class Queue:
        def control(self, changes, max_events, timeout):
            if changes is not None:
                return []
            return [
                SimpleNamespace(
                    ident=20,
                    filter=select.KQ_FILTER_READ,
                    fflags=0,
                    flags=0,
                )
            ]

        def close(self):
            calls.append("queue_closed")

    class WatchdogExit(Exception):
        pass

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", WatchdogTable)
    monkeypatch.setattr(lifecycle.select, "kqueue", Queue)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda *args: None)
    monkeypatch.setattr(lifecycle.os, "read", lambda fd, size: b"")
    monkeypatch.setattr(
        lifecycle.os,
        "write",
        lambda fd, value: calls.append(("write", fd, value)) or len(value),
    )
    monkeypatch.setattr(lifecycle.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda *args, **kwargs: calls.append("cleanup") or True,
    )
    monkeypatch.setattr(
        lifecycle.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(WatchdogExit(code)),
    )

    with pytest.raises(WatchdogExit) as captured:
        lifecycle._watchdog(
            parent,
            _identity(456),
            "a" * 64,
            control_fd=20,
            ready_fd=21,
            acknowledgement_fd=22,
        )

    assert captured.value.args == (70,)
    assert "cleanup" in calls
    assert ("write", 22, b"C") not in calls


def test_watchdog_preserves_primary_failure_and_closes_every_resource(monkeypatch):
    calls = []
    primary = KeyboardInterrupt("primary registration failure")

    class Baseline:
        def close(self):
            calls.append("baseline_close")
            raise OSError("baseline close failure")

    class WatchdogTable:
        def uninspectable_pid_baseline(self):
            return ScanResult(Baseline(), complete=True)

    class Queue:
        def control(self, changes, max_events, timeout):
            raise primary

        def close(self):
            calls.append("queue_close")
            raise OSError("queue close failure")

    def close(fd):
        calls.append(("fd_close", fd))
        if fd == 20:
            raise OSError("control close failure")

    monkeypatch.setattr(lifecycle, "DarwinProcessTable", WatchdogTable)
    monkeypatch.setattr(lifecycle.select, "kqueue", Queue)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda *args: None)
    monkeypatch.setattr(lifecycle.os, "close", close)

    with pytest.raises(KeyboardInterrupt) as captured:
        lifecycle._watchdog(
            _identity(123),
            _identity(456),
            "a" * 64,
            control_fd=20,
            ready_fd=21,
            acknowledgement_fd=22,
        )

    assert captured.value is primary
    assert calls == [
        "queue_close",
        "baseline_close",
        ("fd_close", 20),
        ("fd_close", 21),
        ("fd_close", 22),
    ]


def test_transient_incomplete_scan_never_counts_as_stable_empty():
    class RecoveringTable:
        def __init__(self):
            self.scans = 0

        def marker_identities(
            self,
            marker,
            excluded_pid,
            *,
            uninspectable_pid_baseline=frozenset(),
        ):
            self.scans += 1
            return ScanResult(set(), complete=self.scans > 1)

        def identity(self, pid):
            raise AssertionError("no known identities should be queried")

    table = RecoveringTable()

    assert cleanup_marked_processes(table, "c" * 64, 999, timeout=2.0)
    assert table.scans == 3


def test_persistent_incomplete_scan_fails_cleanup():
    class FailingTable:
        def marker_identities(
            self,
            marker,
            excluded_pid,
            *,
            uninspectable_pid_baseline=frozenset(),
        ):
            return ScanResult(set(), complete=False)

        def identity(self, pid):
            raise AssertionError("no known identities should be queried")

    assert not cleanup_marked_processes(FailingTable(), "d" * 64, 999, timeout=0.02)


def test_signal_identity_rechecks_identity_before_and_after_signal(monkeypatch):
    target = _identity(501)
    replacement = ProcessIdentity(
        target.pid,
        target.uid,
        target.started_seconds + 1,
        target.started_microseconds,
    )
    observations = iter((target, target, replacement))
    identity_calls = []
    signals = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == target.pid
            value = next(observations)
            identity_calls.append(value)
            return ScanResult(value, complete=True)

    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert lifecycle._signal_identity(ProcessTable(), target, signal.SIGTERM)
    assert identity_calls == [target, target, replacement]
    assert signals == [(target.pid, signal.SIGTERM)]


def test_signal_identity_does_not_signal_if_second_precheck_detects_pid_reuse(monkeypatch):
    target = _identity(501)
    replacement = ProcessIdentity(
        target.pid,
        target.uid,
        target.started_seconds + 1,
        target.started_microseconds,
    )
    observations = iter((target, replacement))
    signals = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == target.pid
            return ScanResult(next(observations), complete=True)

    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert lifecycle._signal_identity(ProcessTable(), target, signal.SIGTERM)
    assert signals == []


def test_signal_identity_still_postchecks_after_esrch(monkeypatch):
    target = _identity(501)
    observations = iter((target, target, None))
    identity_calls = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == target.pid
            value = next(observations)
            identity_calls.append(value)
            return ScanResult(value, complete=True)

    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError),
    )

    assert lifecycle._signal_identity(ProcessTable(), target, signal.SIGTERM)
    assert identity_calls == [target, target, None]


def test_cleanup_keeps_bound_identity_after_target_exec_clears_marker(monkeypatch):
    target = _identity(501)
    marker_present = True
    target_alive = True
    marker_scans = 0
    signals = []

    class ProcessTable:
        def marker_identities(
            self,
            marker,
            excluded_pid,
            *,
            uninspectable_pid_baseline=None,
        ):
            nonlocal marker_scans
            marker_scans += 1
            return ScanResult({target} if marker_scans == 1 else set(), complete=True)

        def same_process_with_marker(self, identity, marker):
            assert identity == target
            return ScanResult(marker_present and target_alive, complete=True)

        def identity(self, pid):
            assert pid == target.pid
            return ScanResult(target if target_alive else None, complete=True)

    def kill(pid, sig):
        nonlocal marker_present, target_alive
        assert pid == target.pid
        signals.append(sig)
        if sig == signal.SIGTERM:
            marker_present = False
        elif sig == signal.SIGKILL:
            target_alive = False

    monkeypatch.setattr(lifecycle.os, "kill", kill)

    assert lifecycle.cleanup_marked_processes(
        ProcessTable(),
        "a" * 64,
        999,
        timeout=2.0,
    )
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_parent_exit_stops_and_reaps_supervisor_before_marker_cleanup(monkeypatch):
    supervisor = _identity(501)
    calls = []
    gone_results = iter((False, True))

    class ParentExitTable:
        def child_identities(self, parent_identity, excluded_pid):
            assert parent_identity == supervisor
            calls.append(("capture_children", excluded_pid))
            return ScanResult(set(), complete=True)

    process_table = ParentExitTable()

    def signal_identity(process_table, identity, sig):
        assert identity == supervisor
        calls.append(("signal", sig))
        return True

    def wait_identity_gone(process_table, identity, timeout):
        assert identity == supervisor
        calls.append(("wait", timeout))
        return next(gone_results)

    def cleanup(process_table, marker, watchdog_pid, **kwargs):
        calls.append(("cleanup", watchdog_pid))
        return True

    def cleanup_captured(process_table, identities):
        calls.append(("cleanup_captured", identities))
        return True

    monkeypatch.setattr(lifecycle, "_signal_identity", signal_identity)
    monkeypatch.setattr(lifecycle, "_wait_identity_gone", wait_identity_gone, raising=False)
    monkeypatch.setattr(lifecycle, "cleanup_marked_processes", cleanup)
    monkeypatch.setattr(
        lifecycle,
        "_cleanup_captured_processes",
        cleanup_captured,
        raising=False,
    )
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 777)

    assert lifecycle._handle_parent_exit(
        process_table,
        supervisor,
        "a" * 64,
        object(),
    )
    assert calls == [
        ("signal", signal.SIGSTOP),
        ("capture_children", 777),
        ("signal", signal.SIGTERM),
        ("signal", signal.SIGCONT),
        ("wait", 0.25),
        ("signal", signal.SIGKILL),
        ("wait", lifecycle.WATCHDOG_CLEANUP_TIMEOUT),
        ("cleanup_captured", set()),
        ("cleanup", 777),
    ]


def test_parent_exit_term_gone_outcome_skips_cont_but_finishes_cleanup(monkeypatch):
    supervisor = _identity(501)
    calls = []

    class ProcessTable:
        def child_identities(self, parent_identity, excluded_pid):
            calls.append(("capture", parent_identity, excluded_pid))
            return ScanResult(set(), complete=True)

    def signal_identity(_table, identity, sig):
        assert identity == supervisor
        calls.append(("signal", sig))
        if sig == signal.SIGSTOP:
            return lifecycle.SignalOutcome(lifecycle.SignalState.SENT, True, True)
        if sig == signal.SIGTERM:
            return lifecycle.SignalOutcome(lifecycle.SignalState.GONE, True, True)
        raise AssertionError(f"unexpected signal: {sig}")

    monkeypatch.setattr(lifecycle, "_signal_identity", signal_identity)
    monkeypatch.setattr(
        lifecycle,
        "_wait_identity_gone",
        lambda *args: calls.append("wait") or True,
    )
    monkeypatch.setattr(
        lifecycle,
        "_cleanup_captured_processes",
        lambda *args: calls.append("captured_cleanup") or True,
    )
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda *args, **kwargs: calls.append("marker_cleanup") or True,
    )
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 777)

    assert lifecycle._handle_parent_exit(
        ProcessTable(),
        supervisor,
        "a" * 64,
        object(),
    )

    assert calls == [
        ("signal", signal.SIGSTOP),
        ("capture", supervisor, 777),
        ("signal", signal.SIGTERM),
        "wait",
        "captured_cleanup",
        "marker_cleanup",
    ]


def test_parent_exit_secondary_error_never_calls_instance_add_note_or_stops_cleanup(
    monkeypatch,
):
    supervisor = _identity(501)
    calls = []
    primary = MemoryError("primary STOP post-check failure")
    note_calls = []

    def hostile_add_note(_note):
        note_calls.append("add_note")
        raise RuntimeError("instance add_note must not run")

    primary.add_note = hostile_add_note
    secondary = OSError("secondary child scan failure")

    class ProcessTable:
        def child_identities(self, parent_identity, excluded_pid):
            calls.append(("capture", parent_identity, excluded_pid))
            raise secondary

    def signal_identity(_table, identity, sig):
        assert identity == supervisor
        calls.append(("signal", sig))
        if sig == signal.SIGSTOP:
            return lifecycle.SignalOutcome(
                lifecycle.SignalState.SENT,
                True,
                False,
                primary,
            )
        if sig == signal.SIGTERM:
            return lifecycle.SignalOutcome(lifecycle.SignalState.GONE, True, True)
        raise AssertionError(f"unexpected signal: {sig}")

    monkeypatch.setattr(lifecycle, "_signal_identity", signal_identity)
    monkeypatch.setattr(
        lifecycle,
        "_wait_identity_gone",
        lambda *args: calls.append("wait") or True,
    )
    monkeypatch.setattr(
        lifecycle,
        "_cleanup_captured_processes",
        lambda *args: calls.append("captured_cleanup") or True,
    )
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda *args, **kwargs: calls.append("marker_cleanup") or True,
    )
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 777)

    with pytest.raises(MemoryError) as captured:
        lifecycle._handle_parent_exit(
            ProcessTable(),
            supervisor,
            "a" * 64,
            object(),
        )

    assert captured.value is primary
    assert note_calls == []
    assert calls == [
        ("signal", signal.SIGSTOP),
        ("capture", supervisor, 777),
        ("signal", signal.SIGTERM),
        "wait",
        "captured_cleanup",
        "marker_cleanup",
    ]


def test_parent_exit_does_not_cont_when_stop_was_never_sent(monkeypatch):
    supervisor = _identity(501)
    signals = []

    class IncompleteTable:
        def __init__(self):
            self.identity_calls = 0

        def identity(self, pid):
            assert pid == supervisor.pid
            self.identity_calls += 1
            if self.identity_calls == 1:
                return ScanResult(supervisor, complete=False)
            return ScanResult(None, complete=True)

        def marker_identities(
            self,
            marker,
            excluded_pid,
            *,
            uninspectable_pid_baseline=None,
        ):
            return ScanResult(set(), complete=True)

    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert not lifecycle._handle_parent_exit(
        IncompleteTable(),
        supervisor,
        "a" * 64,
        object(),
    )
    assert signals == []


def test_parent_exit_resumes_supervisor_after_incomplete_identity_read(monkeypatch):
    process = subprocess.Popen(
        ["/bin/sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process_table = DarwinProcessTable()
    identity_scan = process_table.identity(process.pid)
    assert identity_scan.complete and identity_scan.value is not None
    supervisor_identity = identity_scan.value
    real_proc_pidinfo = process_table._proc_pidinfo
    real_signal_identity = lifecycle._signal_identity
    incomplete_identity = False
    attempted_signals = []

    def proc_pidinfo(pid, info):
        if incomplete_identity:
            return 0, 0
        return real_proc_pidinfo(pid, info)

    def signal_identity(table, identity, sig):
        nonlocal incomplete_identity
        attempted_signals.append(sig)
        if sig == signal.SIGTERM:
            incomplete_identity = True
            try:
                return real_signal_identity(table, identity, sig)
            finally:
                incomplete_identity = False
        return real_signal_identity(table, identity, sig)

    monkeypatch.setattr(process_table, "_proc_pidinfo", proc_pidinfo)
    monkeypatch.setattr(lifecycle, "_signal_identity", signal_identity)
    monkeypatch.setattr(lifecycle, "cleanup_marked_processes", lambda *args, **kwargs: True)
    resumed = False
    result = None
    try:
        result = lifecycle._handle_parent_exit(
            process_table,
            supervisor_identity,
            "a" * 64,
            object(),
        )
        try:
            process.wait(timeout=0.75)
            resumed = True
        except subprocess.TimeoutExpired:
            pass
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGCONT)
            process.kill()
            process.wait(timeout=2)

    assert result is False
    assert resumed
    assert attempted_signals == [
        signal.SIGSTOP,
        signal.SIGTERM,
        signal.SIGCONT,
        signal.SIGKILL,
    ]


def test_parent_exit_resumes_when_stop_postcheck_is_incomplete(monkeypatch):
    process = subprocess.Popen(
        ["/bin/sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process_table = DarwinProcessTable()
    identity_scan = process_table.identity(process.pid)
    assert identity_scan.complete and identity_scan.value is not None
    supervisor_identity = identity_scan.value
    real_proc_pidinfo = process_table._proc_pidinfo
    real_signal_identity = lifecycle._signal_identity
    supervisor_reads = 0
    attempted_signals = []
    signal_outcomes = {}

    def proc_pidinfo(pid, info):
        nonlocal supervisor_reads
        if pid == supervisor_identity.pid:
            supervisor_reads += 1
            if supervisor_reads == 3:
                return 0, 0
        return real_proc_pidinfo(pid, info)

    def signal_identity(table, identity, sig):
        attempted_signals.append(sig)
        outcome = real_signal_identity(table, identity, sig)
        signal_outcomes.setdefault(sig, []).append(outcome)
        return outcome

    monkeypatch.setattr(process_table, "_proc_pidinfo", proc_pidinfo)
    monkeypatch.setattr(lifecycle, "_signal_identity", signal_identity)
    monkeypatch.setattr(lifecycle, "_wait_identity_gone", lambda *args: True)
    monkeypatch.setattr(lifecycle, "cleanup_marked_processes", lambda *args, **kwargs: True)
    resumed = False
    try:
        lifecycle._handle_parent_exit(
            process_table,
            supervisor_identity,
            "a" * 64,
            object(),
        )
        try:
            process.wait(timeout=0.75)
            resumed = True
        except subprocess.TimeoutExpired:
            pass
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGCONT)
            process.kill()
            process.wait(timeout=2)

    assert resumed
    assert attempted_signals[:2] == [signal.SIGSTOP, signal.SIGTERM]
    term_outcome = signal_outcomes[signal.SIGTERM][0]
    if term_outcome.state in {lifecycle.SignalState.GONE, lifecycle.SignalState.REUSED}:
        assert signal.SIGCONT not in attempted_signals
    else:
        assert signal.SIGCONT in attempted_signals


def test_parent_exit_resumes_when_stop_postcheck_raises_base_exception(monkeypatch):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.write(1, b'R\\n'); time.sleep(30)"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"R\n"
    process_table = DarwinProcessTable()
    identity_scan = process_table.identity(process.pid)
    assert identity_scan.complete and identity_scan.value is not None
    supervisor_identity = identity_scan.value
    real_identity = process_table.identity
    real_kill = os.kill
    identity_reads = 0
    signals = []

    class StopPostcheckFailure(BaseException):
        pass

    failure = StopPostcheckFailure("injected STOP post-check failure")

    def identity(pid):
        nonlocal identity_reads
        identity_reads += 1
        if identity_reads == 3:
            raise failure
        return real_identity(pid)

    def kill(pid, sig):
        if pid == supervisor_identity.pid:
            signals.append(sig)
        return real_kill(pid, sig)

    monkeypatch.setattr(process_table, "identity", identity)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    cleanup_calls = []
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda *args, **kwargs: cleanup_calls.append("marker") or True,
    )
    try:
        with pytest.raises(StopPostcheckFailure) as captured:
            lifecycle._handle_parent_exit(
                process_table,
                supervisor_identity,
                "a" * 64,
                object(),
            )

        assert captured.value is failure
        process.wait(timeout=0.75)
        assert signals[0] == signal.SIGSTOP
        assert signal.SIGTERM in signals
        assert signal.SIGCONT in signals
        assert process.returncode is not None
        assert cleanup_calls == ["marker"]
    finally:
        if process.poll() is None:
            real_kill(process.pid, signal.SIGCONT)
            real_kill(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        process.stdout.close()


@pytest.mark.parametrize("failure_phase", ["child_scan", "term_postcheck", "wait"])
def test_parent_exit_base_exception_still_completes_cleanup(monkeypatch, failure_phase):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.write(1, b'R\\n'); time.sleep(30)"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"R\n"
    process_table = DarwinProcessTable()
    identity_scan = process_table.identity(process.pid)
    assert identity_scan.complete and identity_scan.value is not None
    supervisor_identity = identity_scan.value
    real_signal_identity = lifecycle._signal_identity
    real_wait_identity_gone = lifecycle._wait_identity_gone
    cleanup_calls = []

    class InjectedCleanupFailure(BaseException):
        pass

    failure = InjectedCleanupFailure(f"injected {failure_phase} failure")

    if failure_phase == "child_scan":

        def fail_child_scan(parent_identity, excluded_pid):
            raise failure

        monkeypatch.setattr(process_table, "child_identities", fail_child_scan)

    def signal_identity(table, identity, sig):
        outcome = real_signal_identity(table, identity, sig)
        if failure_phase == "term_postcheck" and sig == signal.SIGTERM:
            return lifecycle.SignalOutcome(
                outcome.state,
                outcome.signal_sent,
                False,
                failure,
            )
        return outcome

    wait_calls = 0

    def wait_identity_gone(table, identity, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if failure_phase == "wait" and wait_calls == 1:
            raise failure
        return real_wait_identity_gone(table, identity, timeout)

    monkeypatch.setattr(lifecycle, "_signal_identity", signal_identity)
    monkeypatch.setattr(lifecycle, "_wait_identity_gone", wait_identity_gone)
    monkeypatch.setattr(
        lifecycle,
        "cleanup_marked_processes",
        lambda *args, **kwargs: cleanup_calls.append("marker") or True,
    )
    try:
        with pytest.raises(InjectedCleanupFailure) as captured:
            lifecycle._handle_parent_exit(
                process_table,
                supervisor_identity,
                "a" * 64,
                object(),
            )

        assert captured.value is failure
        process.wait(timeout=0.75)
        assert cleanup_calls == ["marker"]
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGCONT)
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        process.stdout.close()


def test_parent_exit_audits_cont_failure_without_obscuring_stop_failure(monkeypatch):
    supervisor_identity = _identity(501)
    identity_reads = 0
    signals = []

    class StopPostcheckFailure(BaseException):
        pass

    failure = StopPostcheckFailure("primary STOP post-check failure")

    class ProcessTable:
        def identity(self, pid):
            nonlocal identity_reads
            assert pid == supervisor_identity.pid
            identity_reads += 1
            if identity_reads == 3:
                raise failure
            return ScanResult(supervisor_identity, complete=True)

    def kill(pid, sig):
        assert pid == supervisor_identity.pid
        signals.append(sig)
        if sig == signal.SIGCONT:
            raise PermissionError("injected CONT failure")

    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle, "_wait_identity_gone", lambda *args: False)
    monkeypatch.setattr(lifecycle, "cleanup_marked_processes", lambda *args, **kwargs: False)

    with pytest.raises(StopPostcheckFailure) as captured:
        lifecycle._handle_parent_exit(
            ProcessTable(),
            supervisor_identity,
            "a" * 64,
            object(),
        )

    assert captured.value is failure
    assert signals == [
        signal.SIGSTOP,
        signal.SIGTERM,
        signal.SIGCONT,
        signal.SIGCONT,
        signal.SIGKILL,
        signal.SIGCONT,
        signal.SIGCONT,
    ]
    assert "SIGCONT recovery was incomplete" in "\n".join(getattr(captured.value, "__notes__", ()))


def test_parent_exit_does_not_cont_after_stop_signal_was_denied(monkeypatch):
    supervisor_identity = _identity(501)
    signals = []

    class ProcessTable:
        def identity(self, pid):
            assert pid == supervisor_identity.pid
            return ScanResult(supervisor_identity, complete=True)

    def kill(pid, sig):
        assert pid == supervisor_identity.pid
        signals.append(sig)
        raise PermissionError(f"injected {sig.name} failure")

    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle, "_wait_identity_gone", lambda *args: False)
    monkeypatch.setattr(lifecycle, "cleanup_marked_processes", lambda *args, **kwargs: False)

    assert not lifecycle._handle_parent_exit(
        ProcessTable(),
        supervisor_identity,
        "a" * 64,
        object(),
    )

    assert signals == [signal.SIGSTOP, signal.SIGTERM, signal.SIGKILL]


def test_parent_exit_does_not_cont_a_reused_pid_after_stop_failure(monkeypatch):
    supervisor_identity = _identity(501)
    replacement = ProcessIdentity(
        supervisor_identity.pid,
        supervisor_identity.uid,
        supervisor_identity.started_seconds + 1,
        supervisor_identity.started_microseconds,
    )
    identity_reads = 0
    signals = []

    class StopPostcheckFailure(BaseException):
        pass

    failure = StopPostcheckFailure("primary STOP post-check failure")

    class ProcessTable:
        def identity(self, pid):
            nonlocal identity_reads
            assert pid == supervisor_identity.pid
            identity_reads += 1
            if identity_reads == 3:
                raise failure
            if identity_reads > 3:
                return ScanResult(replacement, complete=True)
            return ScanResult(supervisor_identity, complete=True)

    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(StopPostcheckFailure) as captured:
        lifecycle._handle_parent_exit(
            ProcessTable(),
            supervisor_identity,
            "a" * 64,
            object(),
        )

    assert captured.value is failure
    assert signals == [(supervisor_identity.pid, signal.SIGSTOP)]


def test_watchdog_event_requires_matching_filter_and_note_exit():
    shared_identifier = 321

    assert lifecycle._watchdog_event_is_parent_exit(
        SimpleNamespace(
            ident=shared_identifier,
            filter=select.KQ_FILTER_PROC,
            fflags=select.KQ_NOTE_EXIT,
            flags=0,
        ),
        parent_pid=shared_identifier,
        control_fd=shared_identifier,
    )
    assert not lifecycle._watchdog_event_is_parent_exit(
        SimpleNamespace(
            ident=shared_identifier,
            filter=select.KQ_FILTER_READ,
            fflags=0,
            flags=0,
        ),
        parent_pid=shared_identifier,
        control_fd=shared_identifier,
    )
    with pytest.raises(RuntimeError, match="unexpected watchdog event"):
        lifecycle._watchdog_event_is_parent_exit(
            SimpleNamespace(
                ident=shared_identifier,
                filter=select.KQ_FILTER_PROC,
                fflags=0,
                flags=0,
            ),
            parent_pid=shared_identifier,
            control_fd=shared_identifier,
        )


def test_known_marker_target_becoming_uninspectable_fails_cleanup_closed():
    target = _identity(101)

    class TargetBecomesUninspectableTable:
        def __init__(self):
            self.scans = 0

        def marker_identities(
            self,
            marker,
            excluded_pid,
            marker_environment="PDF2MD_OWNER_MARKER",
            *,
            uninspectable_pid_baseline=frozenset(),
        ):
            self.scans += 1
            current = {target} if self.scans == 1 else set()
            return ScanResult(current, complete=True)

        def identity(self, pid):
            assert pid == target.pid
            return ScanResult(None, complete=False)

        def same_process_with_marker(self, identity, marker):
            assert identity == target
            return ScanResult(False, complete=False)

    table = TargetBecomesUninspectableTable()

    assert not cleanup_marked_processes(
        table,
        "d" * 64,
        999,
        timeout=0.02,
    )
    assert table.scans == 1


def test_esrch_identity_lookup_is_benign_but_other_errors_are_incomplete(monkeypatch):
    table = _table_without_initialization()

    monkeypatch.setattr(table, "_proc_pidinfo", lambda pid, info: (-1, errno.ESRCH))
    assert table.identity(123) == ScanResult(None, complete=True)

    monkeypatch.setattr(table, "_proc_pidinfo", lambda pid, info: (0, 0))
    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", lambda pid, sig: None)
    assert table.identity(123) == ScanResult(None, complete=False)

    def deny_signal(_pid, _signal):
        raise PermissionError

    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", deny_signal)
    assert table.identity(123) == ScanResult(None, complete=False)

    def missing_process(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr("parsing_core.serving.lifecycle.os.kill", missing_process)
    assert table.identity(123) == ScanResult(None, complete=True)

    monkeypatch.setattr(table, "_proc_pidinfo", lambda pid, info: (-1, errno.EPERM))
    assert table.identity(123) == ScanResult(None, complete=False)

    monkeypatch.setattr(table, "_proc_pidinfo", lambda pid, info: (7, 0))
    assert table.identity(123) == ScanResult(None, complete=False)


class _FakeSysctl:
    def __init__(self, raw: bytes, failure: int | None = None):
        self.raw = raw
        self.failure = failure

    def __call__(self, mib, count, buffer, size_pointer, new_value, new_size):
        if self.failure is not None:
            return -1, self.failure
        if buffer is None:
            size_pointer._obj.value = len(self.raw)
        else:
            ctypes.memmove(buffer, self.raw, len(self.raw))
            size_pointer._obj.value = len(self.raw)
        return 0, 0


def _procargs(marker: str, unrelated_secret: bytes) -> bytes:
    return (
        struct.pack("=i", 1)
        + b"/runtime/python\0\0"
        + b"python\0"
        + b"UNRELATED_SECRET="
        + unrelated_secret
        + b"\0PDF2MD_OWNER_MARKER="
        + marker.encode("ascii")
        + b"\0"
    )


def test_marker_query_returns_only_boolean_and_never_exposes_other_environment(monkeypatch, capsys):
    table = _table_without_initialization()
    marker = "e" * 64
    unrelated_secret = b"must-never-leave-the-raw-buffer"
    monkeypatch.setattr(table, "_sysctl_procargs", _FakeSysctl(_procargs(marker, unrelated_secret)))

    result = table.has_exact_marker(123, marker, "PDF2MD_OWNER_MARKER")

    assert result == ScanResult(True, complete=True)
    assert unrelated_secret.decode() not in repr(result)
    captured = capsys.readouterr()
    assert unrelated_secret.decode() not in captured.out
    assert unrelated_secret.decode() not in captured.err


def test_procargs_esrch_is_benign_but_permission_and_abi_failures_are_incomplete(
    monkeypatch,
):
    table = _table_without_initialization()
    marker = "f" * 64

    monkeypatch.setattr(table, "_sysctl_procargs", _FakeSysctl(b"", errno.ESRCH))
    assert table.has_exact_marker(123, marker, "PDF2MD_OWNER_MARKER") == ScanResult(
        False, complete=True
    )

    for error in (errno.EPERM, errno.EINVAL):
        monkeypatch.setattr(table, "_sysctl_procargs", _FakeSysctl(b"", error))
        assert table.has_exact_marker(123, marker, "PDF2MD_OWNER_MARKER") == ScanResult(
            False, complete=False
        )

    invalid = struct.pack("=i", 2) + b"missing-terminators"
    monkeypatch.setattr(table, "_sysctl_procargs", _FakeSysctl(invalid))
    assert table.has_exact_marker(123, marker, "PDF2MD_OWNER_MARKER") == ScanResult(
        False, complete=False
    )
