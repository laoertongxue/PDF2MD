from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

import pytest

from parsing_core.workbench import process_control


class _FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 4242,
        returncode: int | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.waited = False
        self.waited_timeouts: list[float] = []

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        self.waited_timeouts.append(timeout if timeout is not None else -1.0)
        self.returncode = -15
        return -15

    def poll(self) -> int | None:
        return self.returncode


def _sleep_forever() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def _wait_until_gone(process: subprocess.Popen[bytes], timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        time.sleep(0.01)
    return False


@pytest.fixture
def spawned_group() -> Any:
    process, group_id = process_control.spawn_isolated_process(
        _sleep_forever(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield process, group_id
    finally:
        process_control.terminate_process_group(process, group_id)
        process_control.close_process_pipes(process)
        process.wait(timeout=5)


def test_spawn_isolated_process_returns_verified_isolated_group(spawned_group):
    process, group_id = spawned_group

    assert group_id == process.pid
    assert group_id != os.getpgrp()
    assert os.getpgid(process.pid) == process.pid
    assert os.getsid(process.pid) == os.getsid(0)


def test_terminate_process_group_reaps_real_isolated_group(spawned_group):
    process, group_id = spawned_group

    process_control.terminate_process_group(process, group_id)

    assert process.poll() is not None
    assert _wait_until_gone(process)


def test_close_process_pipes_closes_each_pipe_once(spawned_group):
    process, _group_id = spawned_group

    process_control.close_process_pipes(process)
    process_control.close_process_pipes(process)

    assert process.stdin is None or process.stdin.closed
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_spawn_isolated_process_terminates_root_when_group_cannot_be_confirmed(monkeypatch):
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(process_control.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(process_control.os, "getpgid", lambda _pid: os.getpgrp())
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_control.os,
        "killpg",
        lambda group_id, signal_number: killpg_calls.append((group_id, signal_number)),
    )

    with pytest.raises(RuntimeError, match="group verification failed"):
        process_control.spawn_isolated_process(_sleep_forever(), stdout=subprocess.PIPE)

    assert len(spawned) == 1
    assert killpg_calls == []
    assert spawned[0].poll() is not None


def test_spawn_isolated_process_cleans_confirmed_group_on_session_mismatch(monkeypatch):
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    caller_session = os.getsid(0)
    monkeypatch.setattr(process_control.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        process_control.os,
        "getsid",
        lambda pid: caller_session if pid == 0 else caller_session + 1,
    )

    with pytest.raises(RuntimeError, match="group verification failed"):
        process_control.spawn_isolated_process(_sleep_forever(), stdout=subprocess.PIPE)

    assert len(spawned) == 1
    assert _wait_until_gone(spawned[0])


@pytest.mark.parametrize("unsafe_group_id", [None, "", 0, -1, 99_999])
def test_terminate_process_group_rejects_unsafe_group_and_terminates_root(unsafe_group_id):
    process = _FakeProcess(pid=42_424)

    process_control.terminate_process_group(process, unsafe_group_id)  # type: ignore[arg-type]

    assert process.terminated
    assert process.waited


def test_terminate_process_group_ignores_already_exited_root_with_unsafe_group():
    process = _FakeProcess(pid=42_424, returncode=0)

    process_control.terminate_process_group(process, 0)

    assert not process.terminated


def test_terminate_process_group_returns_when_group_already_missing(monkeypatch):
    process = _FakeProcess(pid=42_424)
    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: False)

    process_control.terminate_process_group(process, process.pid)

    assert not process.terminated


def test_terminate_process_group_returns_on_process_lookup_error(monkeypatch):
    process = _FakeProcess(pid=42_424)
    killpg_calls: list[tuple[int, int]] = []

    def missing_group(group_id: int, signal_number: int) -> None:
        killpg_calls.append((group_id, signal_number))
        raise ProcessLookupError

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(process_control.os, "killpg", missing_group)

    process_control.terminate_process_group(process, process.pid)

    assert killpg_calls == [(process.pid, signal.SIGTERM)]
    assert not process.terminated


def test_terminate_process_group_tolerates_oserror_on_sigterm(monkeypatch):
    process = _FakeProcess(pid=42_424)
    killpg_calls: list[tuple[int, int]] = []
    wait_results = iter([True])

    def failing_killpg(group_id: int, signal_number: int) -> None:
        killpg_calls.append((group_id, signal_number))
        raise OSError("not permitted")

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(process_control.os, "killpg", failing_killpg)
    monkeypatch.setattr(
        process_control,
        "_wait_for_process_group_exit",
        lambda _process, _gid: next(wait_results),
    )

    process_control.terminate_process_group(process, process.pid)

    assert killpg_calls == [(process.pid, signal.SIGTERM)]


def test_terminate_process_group_escalates_to_sigkill(monkeypatch):
    process = _FakeProcess(pid=42_424)
    killpg_calls: list[tuple[int, int]] = []
    wait_results = iter([False, True])

    def recording_killpg(group_id: int, signal_number: int) -> None:
        killpg_calls.append((group_id, signal_number))

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(process_control.os, "killpg", recording_killpg)
    monkeypatch.setattr(
        process_control,
        "_wait_for_process_group_exit",
        lambda _process, _gid: next(wait_results),
    )

    process_control.terminate_process_group(process, process.pid)

    assert killpg_calls == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]


def test_terminate_process_group_reaps_root_when_sigkill_group_missing(monkeypatch):
    process = _FakeProcess(pid=42_424)
    killpg_calls: list[tuple[int, int]] = []

    def missing_group(group_id: int, signal_number: int) -> None:
        killpg_calls.append((group_id, signal_number))
        if signal_number == signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(process_control.os, "killpg", missing_group)
    monkeypatch.setattr(process_control, "_wait_for_process_group_exit", lambda *_args: False)

    process_control.terminate_process_group(process, process.pid)

    assert killpg_calls == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert process.waited


def test_terminate_process_group_tolerates_oserror_on_sigkill_then_confirms_exit(monkeypatch):
    process = _FakeProcess(pid=42_424)
    wait_results = iter([False, True])

    def failing_killpg(_group_id: int, signal_number: int) -> None:
        if signal_number == signal.SIGKILL:
            raise OSError("not permitted")

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(process_control.os, "killpg", failing_killpg)
    monkeypatch.setattr(
        process_control,
        "_wait_for_process_group_exit",
        lambda _process, _gid: next(wait_results),
    )

    process_control.terminate_process_group(process, process.pid)

    assert process.waited


def test_terminate_process_group_raises_when_group_survives_sigkill(monkeypatch):
    process = _FakeProcess(pid=42_424)

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(process_control.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(process_control, "_wait_for_process_group_exit", lambda *_args: False)

    with pytest.raises(RuntimeError, match="helper process cleanup failed"):
        process_control.terminate_process_group(process, process.pid)

    assert process.waited


def test_confirmed_spawned_process_group_accepts_observed_identity(monkeypatch):
    process = _FakeProcess(pid=7777)

    confirmed = process_control._confirmed_spawned_process_group(
        process,
        observed_process_group_id=7777,
        caller_process_group_id=111,
    )

    assert confirmed == 7777


def test_confirmed_spawned_process_group_rejects_invalid_pid():
    process = _FakeProcess(pid=0)

    confirmed = process_control._confirmed_spawned_process_group(
        process,
        observed_process_group_id=None,
        caller_process_group_id=111,
    )

    assert confirmed is None


def test_confirmed_spawned_process_group_rejects_caller_group(monkeypatch):
    process = _FakeProcess(pid=7777)
    monkeypatch.setattr(process_control.os, "getpgid", lambda _pid: 111)

    confirmed = process_control._confirmed_spawned_process_group(
        process,
        observed_process_group_id=None,
        caller_process_group_id=111,
    )

    assert confirmed is None


def test_confirmed_spawned_process_group_uses_fresh_identity(monkeypatch):
    process = _FakeProcess(pid=7777)
    monkeypatch.setattr(process_control.os, "getpgid", lambda _pid: 7777)

    confirmed = process_control._confirmed_spawned_process_group(
        process,
        observed_process_group_id=None,
        caller_process_group_id=111,
    )

    assert confirmed == 7777


def test_confirmed_spawned_process_group_returns_none_when_lookup_fails(monkeypatch):
    process = _FakeProcess(pid=7777)

    def failing_getpgid(_pid: int) -> int:
        raise OSError("gone")

    monkeypatch.setattr(process_control.os, "getpgid", failing_getpgid)

    confirmed = process_control._confirmed_spawned_process_group(
        process,
        observed_process_group_id=None,
        caller_process_group_id=111,
    )

    assert confirmed is None


def test_process_group_exists_reports_missing_and_indeterminate(monkeypatch):
    def missing(_group_id: int, _signal_number: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(process_control.os, "killpg", missing)
    assert process_control._process_group_exists(123) is False

    def indeterminate(_group_id: int, _signal_number: int) -> None:
        raise OSError("not permitted")

    monkeypatch.setattr(process_control.os, "killpg", indeterminate)
    assert process_control._process_group_exists(123) is True


def test_wait_for_process_group_exit_polls_until_gone(monkeypatch):
    process = _FakeProcess(pid=42_424)
    results = iter([True, False, True])
    polls: list[None] = []

    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: next(results))
    monkeypatch.setattr(process_control, "_poll_root_process", lambda _process: polls.append(None))

    assert process_control._wait_for_process_group_exit(process, process.pid) is True
    assert len(polls) == 2


def test_wait_for_process_group_exit_times_out(monkeypatch):
    process = _FakeProcess(pid=42_424)
    sleeps: list[float] = []
    monkeypatch.setattr(process_control, "_GROUP_POLL_ATTEMPTS", 3)
    monkeypatch.setattr(process_control, "_process_group_exists", lambda _gid: True)
    monkeypatch.setattr(
        process_control.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    assert process_control._wait_for_process_group_exit(process, process.pid) is False
    assert sleeps == [process_control._GROUP_POLL_INTERVAL] * 2


def test_poll_root_process_skips_exited_and_swallows_poll_errors():
    exited = _FakeProcess(pid=42_424, returncode=0)
    process_control._poll_root_process(exited)

    class Exploding(_FakeProcess):
        def poll(self) -> int | None:
            raise OSError("poll failed")

    process_control._poll_root_process(Exploding())


def test_reap_root_process_handles_timeout_and_unexpected_errors():
    exited = _FakeProcess(pid=42_424, returncode=0)
    process_control._reap_root_process(exited)
    assert not exited.waited

    class TimingOut(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd="helper", timeout=timeout)

    process_control._reap_root_process(TimingOut())

    class Exploding(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise OSError("wait failed")

    process_control._reap_root_process(Exploding())


def test_terminate_known_process_escalates_to_kill_after_timeout():
    wait_results = iter(
        [
            subprocess.TimeoutExpired(cmd="helper", timeout=0.5),
            OSError("wait failed"),
        ]
    )

    class TimingThenFailing(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            self.waited_timeouts.append(timeout if timeout is not None else -1.0)
            outcome = next(wait_results)
            raise outcome

    timing = TimingThenFailing(pid=42_424)
    process_control._terminate_known_process(timing)

    assert timing.terminated
    assert timing.killed
    assert timing.waited_timeouts == [0.5, 0.5]


def test_terminate_known_process_swallows_terminate_error_and_returns_on_wait():
    class TerminateThenWait(_FakeProcess):
        def terminate(self) -> None:
            self.terminated = True
            raise OSError("terminate failed")

    process = TerminateThenWait(pid=42_424)
    process_control._terminate_known_process(process)

    assert process.terminated
    assert not process.killed
    assert process.waited


def test_terminate_process_group_treats_getpgrp_failure_as_unsafe(monkeypatch):
    process = _FakeProcess(pid=42_424)

    def failing_getpgrp() -> int:
        raise OSError("getpgrp failed")

    monkeypatch.setattr(process_control.os, "getpgrp", failing_getpgrp)

    process_control.terminate_process_group(process, process.pid)

    assert process.terminated
    assert process.waited


def test_terminate_known_process_returns_when_wait_raises_unexpected_error():
    class WaitFails(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            raise OSError("wait failed")

    process = WaitFails(pid=42_424)
    process_control._terminate_known_process(process)

    assert process.terminated
    assert not process.killed
    assert process.waited


def test_terminate_known_process_tolerates_kill_failure():
    wait_results = iter(
        [
            subprocess.TimeoutExpired(cmd="helper", timeout=0.5),
            OSError("wait failed"),
        ]
    )

    class KillFails(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            self.waited_timeouts.append(timeout if timeout is not None else -1.0)
            raise next(wait_results)

        def kill(self) -> None:
            self.killed = True
            raise OSError("kill failed")

    process = KillFails(pid=42_424)
    process_control._terminate_known_process(process)

    assert process.terminated
    assert process.killed
    assert process.waited_timeouts == [0.5, 0.5]
