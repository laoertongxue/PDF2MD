from __future__ import annotations

import argparse
import ctypes
import errno
import os
import secrets
import select
import signal
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

OWNER_MARKER_ENV = "PDF2MD_OWNER_MARKER"
OWNER_MARKER_BYTES = 32
PROC_ALL_PIDS = 1
PROC_PIDLISTFDS = 1
PROC_PIDTBSDINFO = 3
KERN_PROCARGS2 = 49
CTL_KERN = 1
WATCHDOG_READY_TIMEOUT = 2.0
WATCHDOG_CLEANUP_TIMEOUT = 2.5
WATCHDOG_POLL_INTERVAL = 0.05
MAX_UNINSPECTABLE_BASELINE_PIDS = 256
WATCHDOG_ARM_MESSAGE = struct.Struct("!cIIQQ")

T = TypeVar("T")


class ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    started_seconds: int
    started_microseconds: int


@dataclass(frozen=True)
class ScanResult(Generic[T]):  # noqa: UP046 - release smoke supports macOS system Python.
    value: T
    complete: bool


class SignalState(Enum):
    SENT = "sent"
    GONE = "gone"
    REUSED = "reused"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class SignalOutcome:
    state: SignalState
    signal_sent: bool
    complete: bool
    error: BaseException | None = None

    def __bool__(self) -> bool:
        return self.error is None and self.state is not SignalState.UNCERTAIN


class UninspectableProcessBaseline:
    def __init__(self, queue: Any, pids: set[int]) -> None:
        self._queue = queue
        self._pids = pids
        self._closed = False

    def active_pids(self) -> ScanResult[frozenset[int]]:
        if self._closed:
            return ScanResult(frozenset(), complete=False)
        if not self._pids:
            return ScanResult(frozenset(), complete=True)
        try:
            events = self._queue.control(None, max(len(self._pids), 1), 0)
        except OSError:
            return ScanResult(frozenset(self._pids), complete=False)
        complete = True
        for event in events:
            valid_exit = (
                event.ident in self._pids
                and event.filter == select.KQ_FILTER_PROC
                and bool(event.fflags & select.KQ_NOTE_EXIT)
                and not bool(event.flags & select.KQ_EV_ERROR)
            )
            if not valid_exit:
                complete = False
                continue
            self._pids.remove(int(event.ident))
        return ScanResult(frozenset(self._pids), complete=complete)

    def close(self) -> None:
        if self._closed:
            return
        self._queue.close()
        self._closed = True


class DarwinProcessTable:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("sidecar lifecycle supervision requires macOS")
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._libproc.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._libproc.proc_listpids.restype = ctypes.c_int
        self._libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._libproc.proc_pidinfo.restype = ctypes.c_int
        self._libc.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self._libc.sysctl.restype = ctypes.c_int

    def _proc_pidinfo(self, pid: int, info: ProcBsdInfo) -> tuple[int, int]:
        ctypes.set_errno(0)
        received = self._libproc.proc_pidinfo(
            pid,
            PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return int(received), ctypes.get_errno()

    def _proc_listpids(
        self, buffer: ctypes.Array[ctypes.c_int] | None, size: int
    ) -> tuple[int, int]:
        ctypes.set_errno(0)
        pointer = None if buffer is None else ctypes.byref(buffer)
        received = self._libproc.proc_listpids(PROC_ALL_PIDS, 0, pointer, size)
        return int(received), ctypes.get_errno()

    def _sysctl_procargs(
        self,
        mib: ctypes.Array[ctypes.c_int],
        count: int,
        buffer: object | None,
        size_pointer: object,
        new_value: object | None,
        new_size: int,
    ) -> tuple[int, int]:
        ctypes.set_errno(0)
        result = self._libc.sysctl(
            mib,
            count,
            buffer,
            size_pointer,
            new_value,
            new_size,
        )
        return int(result), ctypes.get_errno()

    def identity(self, pid: int) -> ScanResult[ProcessIdentity | None]:
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        received, error = self._proc_pidinfo(pid, info)
        if received != size:
            vanished = error == errno.ESRCH or (
                received == 0 and error == 0 and self._pid_is_definitely_gone(pid)
            )
            return ScanResult(None, complete=vanished)
        return ScanResult(
            ProcessIdentity(
                pid=int(info.pbi_pid),
                uid=int(info.pbi_uid),
                started_seconds=int(info.pbi_start_tvsec),
                started_microseconds=int(info.pbi_start_tvusec),
            ),
            complete=True,
        )

    @staticmethod
    def _pid_is_definitely_gone(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        return False

    def pids(self) -> ScanResult[list[int]]:
        required, _error = self._proc_listpids(None, 0)
        if required <= 0:
            return ScanResult([], complete=False)
        capacity = required + 256 * ctypes.sizeof(ctypes.c_int)
        buffer = (ctypes.c_int * (capacity // ctypes.sizeof(ctypes.c_int)))()
        received, _error = self._proc_listpids(buffer, ctypes.sizeof(buffer))
        if received <= 0:
            return ScanResult([], complete=False)
        count = received // ctypes.sizeof(ctypes.c_int)
        return ScanResult(
            [int(pid) for pid in buffer[:count] if pid > 1],
            complete=received < ctypes.sizeof(buffer),
        )

    def uninspectable_pid_baseline(
        self,
    ) -> ScanResult[UninspectableProcessBaseline | None]:
        pid_scan = self.pids()
        if not pid_scan.complete:
            return ScanResult(None, complete=False)
        try:
            queue = select.kqueue()
        except OSError:
            return ScanResult(None, complete=False)
        uninspectable: set[int] = set()
        try:
            for pid in pid_scan.value:
                identity_scan = self.identity(pid)
                if identity_scan.complete or self._pid_is_definitely_not_owned(pid):
                    continue
                if len(uninspectable) >= MAX_UNINSPECTABLE_BASELINE_PIDS:
                    queue.close()
                    return ScanResult(None, complete=False)
                try:
                    queue.control(
                        [
                            select.kevent(
                                pid,
                                filter=select.KQ_FILTER_PROC,
                                flags=(select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR),
                                fflags=select.KQ_NOTE_EXIT,
                            )
                        ],
                        0,
                        0,
                    )
                except OSError as error:
                    if error.errno == errno.ESRCH:
                        continue
                    queue.close()
                    return ScanResult(None, complete=False)
                uninspectable.add(pid)
        except BaseException:
            queue.close()
            raise
        return ScanResult(
            UninspectableProcessBaseline(queue, uninspectable),
            complete=True,
        )

    def has_exact_marker(
        self,
        pid: int,
        marker: str,
        marker_environment: str = OWNER_MARKER_ENV,
    ) -> ScanResult[bool]:
        expected = f"{marker_environment}={marker}".encode("ascii")
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        size = ctypes.c_size_t()
        result, error = self._sysctl_procargs(mib, 3, None, ctypes.byref(size), None, 0)
        if result != 0:
            return ScanResult(False, complete=error == errno.ESRCH)
        if size.value < ctypes.sizeof(ctypes.c_int) or size.value > 8 * 1024 * 1024:
            return ScanResult(False, complete=False)
        buffer = ctypes.create_string_buffer(size.value)
        result, error = self._sysctl_procargs(
            mib,
            3,
            ctypes.byref(buffer),
            ctypes.byref(size),
            None,
            0,
        )
        if result != 0:
            return ScanResult(False, complete=error == errno.ESRCH)
        raw = buffer.raw[: size.value]
        if len(raw) < ctypes.sizeof(ctypes.c_int):
            return ScanResult(False, complete=False)
        argc = struct.unpack_from("=i", raw)[0]
        if argc < 0:
            return ScanResult(False, complete=False)
        offset = ctypes.sizeof(ctypes.c_int)
        executable_end = raw.find(b"\0", offset)
        if executable_end < 0:
            return ScanResult(False, complete=False)
        offset = executable_end + 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        for _ in range(argc):
            argument_end = raw.find(b"\0", offset)
            if argument_end < 0:
                return ScanResult(False, complete=False)
            offset = argument_end + 1
        while offset < len(raw):
            environment_end = raw.find(b"\0", offset)
            if environment_end < 0:
                return ScanResult(False, complete=False)
            if raw[offset:environment_end] == expected:
                return ScanResult(True, complete=True)
            offset = environment_end + 1
        return ScanResult(False, complete=True)

    def marker_identities(
        self,
        marker: str,
        excluded_pid: int,
        marker_environment: str = OWNER_MARKER_ENV,
        *,
        uninspectable_pid_baseline: UninspectableProcessBaseline | None = None,
    ) -> ScanResult[set[ProcessIdentity]]:
        uid = os.getuid()
        matches: set[ProcessIdentity] = set()
        baseline_scan = (
            uninspectable_pid_baseline.active_pids()
            if uninspectable_pid_baseline is not None
            else ScanResult(frozenset(), complete=True)
        )
        baseline_pids = baseline_scan.value
        pid_scan = self.pids()
        complete = pid_scan.complete and baseline_scan.complete
        for pid in pid_scan.value:
            if pid == excluded_pid:
                continue
            before_scan = self.identity(pid)
            if not before_scan.complete:
                if self._pid_is_definitely_not_owned(pid):
                    continue
                if pid in baseline_pids:
                    continue
                complete = False
                continue
            before = before_scan.value
            if before is None or before.uid != uid:
                continue
            marker_scan = self.has_exact_marker(pid, marker, marker_environment)
            complete = complete and marker_scan.complete
            if not marker_scan.value:
                continue
            after_scan = self.identity(pid)
            complete = complete and after_scan.complete
            if after_scan.value == before:
                matches.add(before)
        return ScanResult(matches, complete=complete)

    def child_identities(
        self,
        parent_identity: ProcessIdentity,
        excluded_pid: int,
    ) -> ScanResult[set[ProcessIdentity]]:
        parent_before = self.identity(parent_identity.pid)
        if not parent_before.complete or parent_before.value != parent_identity:
            return ScanResult(set(), complete=False)
        pid_scan = self.pids()
        complete = pid_scan.complete
        matches: set[ProcessIdentity] = set()
        uid = os.getuid()
        for pid in pid_scan.value:
            if pid in {excluded_pid, parent_identity.pid}:
                continue
            info = ProcBsdInfo()
            received, error = self._proc_pidinfo(pid, info)
            if received != ctypes.sizeof(info):
                vanished = error == errno.ESRCH or (
                    received == 0 and error == 0 and self._pid_is_definitely_gone(pid)
                )
                if not vanished and not self._pid_is_definitely_not_owned(pid):
                    complete = False
                continue
            identity = ProcessIdentity(
                pid=int(info.pbi_pid),
                uid=int(info.pbi_uid),
                started_seconds=int(info.pbi_start_tvsec),
                started_microseconds=int(info.pbi_start_tvusec),
            )
            if (
                identity.pid != pid
                or identity.uid != uid
                or int(info.pbi_ppid) != parent_identity.pid
            ):
                continue
            after = self.identity(pid)
            complete = complete and after.complete
            if after.value == identity:
                matches.add(identity)
        parent_after = self.identity(parent_identity.pid)
        complete = complete and parent_after.complete and parent_after.value == parent_identity
        return ScanResult(matches, complete=complete)

    def same_direct_child(
        self,
        parent_identity: ProcessIdentity,
        child_identity: ProcessIdentity,
    ) -> ScanResult[bool]:
        parent_before = self.identity(parent_identity.pid)
        if not parent_before.complete or parent_before.value != parent_identity:
            return ScanResult(False, complete=parent_before.complete)

        info = ProcBsdInfo()
        received, error = self._proc_pidinfo(child_identity.pid, info)
        if received != ctypes.sizeof(info):
            vanished = error == errno.ESRCH or (
                received == 0 and error == 0 and self._pid_is_definitely_gone(child_identity.pid)
            )
            return ScanResult(False, complete=vanished)
        observed_child = ProcessIdentity(
            pid=int(info.pbi_pid),
            uid=int(info.pbi_uid),
            started_seconds=int(info.pbi_start_tvsec),
            started_microseconds=int(info.pbi_start_tvusec),
        )
        if observed_child != child_identity or int(info.pbi_ppid) != parent_identity.pid:
            return ScanResult(False, complete=True)

        child_after = self.identity(child_identity.pid)
        parent_after = self.identity(parent_identity.pid)
        complete = child_after.complete and parent_after.complete
        return ScanResult(
            child_after.value == child_identity and parent_after.value == parent_identity,
            complete=complete,
        )

    @staticmethod
    def _pid_is_definitely_not_owned(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        except OSError:
            return False
        return False

    def same_process_with_marker(
        self,
        identity: ProcessIdentity,
        marker: str,
        marker_environment: str = OWNER_MARKER_ENV,
    ) -> ScanResult[bool]:
        before = self.identity(identity.pid)
        if not before.complete or before.value != identity:
            return ScanResult(False, complete=before.complete)
        marker_scan = self.has_exact_marker(identity.pid, marker, marker_environment)
        if not marker_scan.complete or not marker_scan.value:
            return ScanResult(False, complete=marker_scan.complete)
        after = self.identity(identity.pid)
        return ScanResult(
            after.value == identity,
            complete=after.complete,
        )


def cleanup_marked_processes(
    process_table: DarwinProcessTable,
    marker: str,
    watchdog_pid: int,
    *,
    timeout: float = WATCHDOG_CLEANUP_TIMEOUT,
    uninspectable_pid_baseline: UninspectableProcessBaseline | None = None,
    bound_identities: set[ProcessIdentity] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    bound = bound_identities if bound_identities is not None else set()
    unresolved: set[ProcessIdentity] = set()
    stable_empty_scans = 0
    sent_term = False
    sent_kill = False
    while time.monotonic() < deadline:
        current_scan = process_table.marker_identities(
            marker,
            watchdog_pid,
            uninspectable_pid_baseline=uninspectable_pid_baseline,
        )
        scan_complete = current_scan.complete
        for identity in current_scan.value - bound:
            verified = True
            for _ in range(2):
                marker_scan = process_table.same_process_with_marker(identity, marker)
                scan_complete = scan_complete and marker_scan.complete
                if not marker_scan.complete or not marker_scan.value:
                    verified = False
                    break
            if verified:
                bound.add(identity)
                unresolved.discard(identity)
            else:
                unresolved.add(identity)

        live_bound: set[ProcessIdentity] = set()
        for identity in bound | unresolved:
            identity_scan = process_table.identity(identity.pid)
            scan_complete = scan_complete and identity_scan.complete
            if identity_scan.value == identity:
                if identity in bound:
                    live_bound.add(identity)
                else:
                    scan_complete = False
            elif identity_scan.complete:
                unresolved.discard(identity)

        if scan_complete and not live_bound and not unresolved:
            stable_empty_scans += 1
            if stable_empty_scans >= 2:
                return True
        else:
            stable_empty_scans = 0
            targets = live_bound
            if not sent_term:
                for identity in targets:
                    outcome = _signal_identity(process_table, identity, signal.SIGTERM)
                    scan_complete = scan_complete and outcome.complete and bool(outcome)
                if targets:
                    sent_term = True
            elif not sent_kill:
                for identity in targets:
                    outcome = _signal_identity(process_table, identity, signal.SIGKILL)
                    scan_complete = scan_complete and outcome.complete and bool(outcome)
                sent_kill = True
            else:
                for identity in targets:
                    outcome = _signal_identity(process_table, identity, signal.SIGKILL)
                    scan_complete = scan_complete and outcome.complete and bool(outcome)
        time.sleep(0.05)
    return False


def _bind_marked_identities(
    process_table: DarwinProcessTable,
    marker: str,
    excluded_pid: int,
    bound: set[ProcessIdentity],
    *,
    uninspectable_pid_baseline: UninspectableProcessBaseline | None = None,
) -> bool:
    current_scan = process_table.marker_identities(
        marker,
        excluded_pid,
        uninspectable_pid_baseline=uninspectable_pid_baseline,
    )
    complete = current_scan.complete
    for identity in current_scan.value - bound:
        for _ in range(2):
            marker_scan = process_table.same_process_with_marker(identity, marker)
            complete = complete and marker_scan.complete
            if not marker_scan.complete or not marker_scan.value:
                complete = False
                break
        else:
            bound.add(identity)
    return complete


def _signal_identity(
    process_table: DarwinProcessTable,
    identity: ProcessIdentity,
    sig: signal.Signals,
) -> SignalOutcome:
    for _ in range(2):
        try:
            current = process_table.identity(identity.pid)
        except BaseException as error:
            return SignalOutcome(SignalState.UNCERTAIN, False, False, error)
        if not current.complete:
            return SignalOutcome(SignalState.UNCERTAIN, False, False)
        if current.value is None:
            return SignalOutcome(SignalState.GONE, False, True)
        if current.value != identity:
            return SignalOutcome(SignalState.REUSED, False, True)
    try:
        os.kill(identity.pid, sig)
    except ProcessLookupError:
        try:
            after = process_table.identity(identity.pid)
        except BaseException as error:
            return SignalOutcome(SignalState.UNCERTAIN, False, False, error)
        if after.complete and after.value is None:
            return SignalOutcome(SignalState.GONE, False, True)
        if after.complete and after.value != identity:
            return SignalOutcome(SignalState.REUSED, False, True)
        return SignalOutcome(SignalState.UNCERTAIN, False, after.complete)
    except (PermissionError, OSError):
        try:
            after = process_table.identity(identity.pid)
        except BaseException as error:
            return SignalOutcome(SignalState.UNCERTAIN, False, False, error)
        if after.complete and after.value is None:
            return SignalOutcome(SignalState.GONE, False, True)
        if after.complete and after.value != identity:
            return SignalOutcome(SignalState.REUSED, False, True)
        return SignalOutcome(SignalState.UNCERTAIN, False, after.complete)
    try:
        after = process_table.identity(identity.pid)
    except BaseException as error:
        return SignalOutcome(SignalState.SENT, True, False, error)
    if not after.complete:
        return SignalOutcome(SignalState.SENT, True, False)
    if after.value is None:
        return SignalOutcome(SignalState.GONE, True, True)
    if after.value != identity:
        return SignalOutcome(SignalState.REUSED, True, True)
    return SignalOutcome(SignalState.SENT, True, True)


def _resume_stopped_identity(
    process_table: DarwinProcessTable,
    identity: ProcessIdentity,
) -> SignalOutcome:
    raw_outcome = _signal_identity(process_table, identity, signal.SIGCONT)
    if isinstance(raw_outcome, SignalOutcome):
        outcome = raw_outcome
    else:
        outcome = SignalOutcome(
            SignalState.SENT if raw_outcome else SignalState.UNCERTAIN,
            bool(raw_outcome),
            bool(raw_outcome),
        )
    if outcome.state in {SignalState.GONE, SignalState.REUSED}:
        return outcome
    if outcome.signal_sent:
        return outcome

    # A successful STOP constrains the original process from voluntarily exiting.
    # If proc_pidinfo is temporarily incomplete, prioritize releasing that STOP.
    try:
        os.kill(identity.pid, signal.SIGCONT)
    except ProcessLookupError:
        return SignalOutcome(SignalState.GONE, False, True, outcome.error)
    except (PermissionError, OSError):
        return outcome
    try:
        after = process_table.identity(identity.pid)
    except BaseException as error:
        return SignalOutcome(SignalState.SENT, True, False, outcome.error or error)
    if after.complete and after.value is None:
        return SignalOutcome(SignalState.GONE, True, True, outcome.error)
    if after.complete and after.value != identity:
        return SignalOutcome(SignalState.REUSED, True, True, outcome.error)
    return SignalOutcome(SignalState.SENT, True, after.complete, outcome.error)


def _cleanup_captured_processes(
    process_table: DarwinProcessTable,
    identities: set[ProcessIdentity],
) -> bool:
    complete = True
    for identity in identities:
        complete = bool(_signal_identity(process_table, identity, signal.SIGTERM)) and complete
    for identity in identities:
        if _wait_identity_gone(process_table, identity, 0.25):
            continue
        complete = bool(_signal_identity(process_table, identity, signal.SIGKILL)) and complete
        complete = (
            _wait_identity_gone(
                process_table,
                identity,
                WATCHDOG_CLEANUP_TIMEOUT,
            )
            and complete
        )
    return complete


def _wait_identity_gone(
    process_table: DarwinProcessTable,
    identity: ProcessIdentity,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = process_table.identity(identity.pid)
        if current.complete and current.value != identity:
            return True
        time.sleep(0.025)
    return False


def _append_exception_note(error: BaseException, note: str) -> None:
    if sys.version_info >= (3, 11):  # noqa: UP036 - release smoke supports Python 3.9.
        try:
            BaseException.add_note(error, note)
        except BaseException:
            pass
    else:
        try:
            sys.stderr.write(f"{note}\n")
            sys.stderr.flush()
        except BaseException:
            pass


def _audit_incomplete_sigcont_recovery() -> None:
    try:
        sys.stderr.write("sidecar supervisor SIGCONT recovery was incomplete\n")
        sys.stderr.flush()
    except BaseException:
        pass


def _handle_parent_exit(
    process_table: DarwinProcessTable,
    supervisor_identity: ProcessIdentity,
    marker: str,
    uninspectable_pid_baseline: UninspectableProcessBaseline,
    bound_identities: set[ProcessIdentity] | None = None,
) -> bool:
    stop_may_be_active = False
    primary_error: BaseException | None = None
    cleanup_complete = True
    captured: set[ProcessIdentity] = set()

    def record_error(error: BaseException, note: str) -> None:
        nonlocal primary_error, cleanup_complete
        cleanup_complete = False
        if primary_error is None:
            primary_error = error
        else:
            _append_exception_note(primary_error, note)

    def signal_supervisor(sig: signal.Signals) -> SignalOutcome:
        nonlocal cleanup_complete
        try:
            raw_outcome = _signal_identity(process_table, supervisor_identity, sig)
        except BaseException as error:
            record_error(error, "sidecar supervisor signal cleanup was incomplete")
            return SignalOutcome(SignalState.UNCERTAIN, False, False, error)
        if isinstance(raw_outcome, SignalOutcome):
            outcome = raw_outcome
        else:
            outcome = SignalOutcome(
                SignalState.SENT if raw_outcome else SignalState.UNCERTAIN,
                bool(raw_outcome),
                bool(raw_outcome),
            )
        if outcome.error is not None:
            record_error(outcome.error, "sidecar supervisor signal cleanup was incomplete")
        if not outcome.complete or not outcome:
            cleanup_complete = False
        return outcome

    stop_outcome = signal_supervisor(signal.SIGSTOP)
    stop_may_be_active = stop_outcome.signal_sent and stop_outcome.state is SignalState.SENT

    if stop_may_be_active:
        try:
            captured_scan = process_table.child_identities(
                supervisor_identity,
                os.getpid(),
            )
            captured = captured_scan.value
            cleanup_complete = captured_scan.complete and cleanup_complete
        except BaseException as error:
            record_error(error, "sidecar child capture was incomplete")

    term_outcome = signal_supervisor(signal.SIGTERM)
    if term_outcome.state in {SignalState.GONE, SignalState.REUSED}:
        stop_may_be_active = False

    if stop_may_be_active:
        try:
            resume_outcome = _resume_stopped_identity(process_table, supervisor_identity)
        except BaseException as error:
            record_error(error, "sidecar supervisor SIGCONT recovery was incomplete")
            resume_outcome = SignalOutcome(SignalState.UNCERTAIN, False, False, error)
        if resume_outcome.error is not None and resume_outcome.error is not primary_error:
            record_error(
                resume_outcome.error,
                "sidecar supervisor SIGCONT recovery was incomplete",
            )
        resumed = bool(resume_outcome) and (
            resume_outcome.signal_sent
            or resume_outcome.state in {SignalState.GONE, SignalState.REUSED}
        )
        if not resumed:
            cleanup_complete = False
            if primary_error is not None:
                _append_exception_note(
                    primary_error,
                    "sidecar supervisor SIGCONT recovery was incomplete",
                )
            else:
                _audit_incomplete_sigcont_recovery()
        stop_may_be_active = not resumed

    supervisor_gone = False
    try:
        supervisor_gone = _wait_identity_gone(process_table, supervisor_identity, 0.25)
    except BaseException as error:
        record_error(error, "sidecar supervisor wait was incomplete")
    if not supervisor_gone:
        kill_outcome = signal_supervisor(signal.SIGKILL)
        if kill_outcome.state in {SignalState.GONE, SignalState.REUSED}:
            stop_may_be_active = False
        try:
            supervisor_gone = _wait_identity_gone(
                process_table,
                supervisor_identity,
                WATCHDOG_CLEANUP_TIMEOUT,
            )
        except BaseException as error:
            record_error(error, "sidecar supervisor wait was incomplete")
        cleanup_complete = supervisor_gone and cleanup_complete

    if stop_may_be_active:
        try:
            final_resume = _resume_stopped_identity(process_table, supervisor_identity)
        except BaseException as error:
            record_error(error, "sidecar supervisor SIGCONT recovery was incomplete")
            final_resume = SignalOutcome(SignalState.UNCERTAIN, False, False, error)
        if not final_resume:
            cleanup_complete = False
            if primary_error is not None:
                _append_exception_note(
                    primary_error,
                    "sidecar supervisor SIGCONT recovery was incomplete",
                )
            else:
                _audit_incomplete_sigcont_recovery()

    try:
        cleanup_complete = _cleanup_captured_processes(process_table, captured) and cleanup_complete
    except BaseException as error:
        record_error(error, "sidecar captured-process cleanup was incomplete")

    try:
        cleanup_complete = (
            cleanup_marked_processes(
                process_table,
                marker,
                os.getpid(),
                uninspectable_pid_baseline=uninspectable_pid_baseline,
                bound_identities=bound_identities,
            )
            and cleanup_complete
        )
    except BaseException as error:
        record_error(error, "sidecar marker cleanup was incomplete")

    if primary_error is not None:
        raise primary_error
    return cleanup_complete


def _watchdog_event_is_parent_exit(
    event: Any,
    *,
    parent_pid: int,
    control_fd: int,
) -> bool:
    if event.flags & select.KQ_EV_ERROR:
        raise RuntimeError("unexpected watchdog event")
    if (
        event.ident == parent_pid
        and event.filter == select.KQ_FILTER_PROC
        and event.fflags & select.KQ_NOTE_EXIT
    ):
        return True
    if event.ident == control_fd and event.filter == select.KQ_FILTER_READ:
        return False
    raise RuntimeError("unexpected watchdog event")


def _encode_watchdog_arm(identity: ProcessIdentity) -> bytes:
    return WATCHDOG_ARM_MESSAGE.pack(
        b"A",
        identity.pid,
        identity.uid,
        identity.started_seconds,
        identity.started_microseconds,
    )


def _decode_watchdog_arm(value: bytes) -> ProcessIdentity | None:
    if len(value) != WATCHDOG_ARM_MESSAGE.size:
        return None
    command, pid, uid, started_seconds, started_microseconds = WATCHDOG_ARM_MESSAGE.unpack(value)
    if command != b"A":
        return None
    return ProcessIdentity(pid, uid, started_seconds, started_microseconds)


def _verify_child_binding(
    process_table: DarwinProcessTable,
    parent_identity: ProcessIdentity,
    child_identity: ProcessIdentity,
    timeout: float = WATCHDOG_READY_TIMEOUT,
) -> bool:
    deadline = time.monotonic() + timeout
    consecutive_matches = 0
    while time.monotonic() < deadline:
        scan = process_table.same_direct_child(parent_identity, child_identity)
        if scan.complete and scan.value:
            consecutive_matches += 1
            if consecutive_matches == 2:
                return True
        else:
            consecutive_matches = 0
        time.sleep(0.01)
    return False


def _watchdog(
    parent_identity: ProcessIdentity,
    supervisor_identity: ProcessIdentity,
    marker: str,
    control_fd: int,
    ready_fd: int,
    acknowledgement_fd: int,
) -> None:
    owned_fds = {control_fd, ready_fd, acknowledgement_fd}
    process_table: DarwinProcessTable | None = None
    uninspectable_pid_baseline: UninspectableProcessBaseline | None = None
    queue: Any | None = None
    primary_error: BaseException | None = None
    cleanup_failed = False
    cleaned = False
    exit_code = 70
    bound: set[ProcessIdentity] = set()

    def close_owned_fd(fd: int) -> None:
        if fd not in owned_fds:
            return
        owned_fds.remove(fd)
        os.close(fd)

    def cleanup_marker_processes() -> bool:
        if process_table is None or uninspectable_pid_baseline is None:
            return False
        return cleanup_marked_processes(
            process_table,
            marker,
            os.getpid(),
            uninspectable_pid_baseline=uninspectable_pid_baseline,
            bound_identities=bound,
        )

    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        process_table = DarwinProcessTable()
        baseline_scan = process_table.uninspectable_pid_baseline()
        if not baseline_scan.complete or baseline_scan.value is None:
            raise RuntimeError("sidecar lifecycle process baseline is incomplete")
        uninspectable_pid_baseline = baseline_scan.value
        queue = select.kqueue()
        changes = [
            select.kevent(
                parent_identity.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT,
            ),
            select.kevent(
                control_fd,
                filter=select.KQ_FILTER_READ,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
            ),
        ]
        queue.control(changes, 0, 0)
        parent_scan = process_table.identity(parent_identity.pid)
        if not parent_scan.complete or parent_scan.value != parent_identity:
            cleaned = _handle_parent_exit(
                process_table,
                supervisor_identity,
                marker,
                uninspectable_pid_baseline,
                bound,
            )
            exit_code = 0 if cleaned else 70
        else:
            os.write(ready_fd, b"R")
            close_owned_fd(ready_fd)
            armed = False
            while True:
                events = queue.control(None, 1, WATCHDOG_POLL_INTERVAL)
                if not events:
                    parent_scan = process_table.identity(parent_identity.pid)
                    if not parent_scan.complete or parent_scan.value != parent_identity:
                        cleaned = _handle_parent_exit(
                            process_table,
                            supervisor_identity,
                            marker,
                            uninspectable_pid_baseline,
                            bound,
                        )
                        exit_code = 0 if cleaned else 70
                        break
                    if armed:
                        _bind_marked_identities(
                            process_table,
                            marker,
                            os.getpid(),
                            bound,
                            uninspectable_pid_baseline=uninspectable_pid_baseline,
                        )
                    continue

                event = events[0]
                if _watchdog_event_is_parent_exit(
                    event,
                    parent_pid=parent_identity.pid,
                    control_fd=control_fd,
                ):
                    cleaned = _handle_parent_exit(
                        process_table,
                        supervisor_identity,
                        marker,
                        uninspectable_pid_baseline,
                        bound,
                    )
                    exit_code = 0 if cleaned else 70
                    break

                command = os.read(control_fd, WATCHDOG_ARM_MESSAGE.size)
                arm_identity = _decode_watchdog_arm(command)
                if arm_identity is not None and not armed:
                    if not _verify_child_binding(
                        process_table,
                        supervisor_identity,
                        arm_identity,
                    ):
                        cleaned = cleanup_marker_processes()
                        exit_code = 70
                        break
                    bound.add(arm_identity)
                    armed = True
                    os.write(acknowledgement_fd, b"A")
                    continue
                if command == b"N" and armed:
                    cleaned = cleanup_marker_processes()
                    os.write(acknowledgement_fd, b"C" if cleaned else b"E")
                    exit_code = 0 if cleaned else 70
                    break

                # EOF, shutdown before ARM, and unknown commands are crashes.
                cleaned = cleanup_marker_processes()
                exit_code = 70
                break
    except BaseException as error:
        primary_error = error
    finally:
        if queue is not None:
            try:
                queue.close()
            except BaseException:
                cleanup_failed = True
                if primary_error is not None:
                    _append_exception_note(
                        primary_error,
                        "sidecar watchdog queue close was incomplete",
                    )
        if uninspectable_pid_baseline is not None:
            try:
                uninspectable_pid_baseline.close()
            except BaseException:
                cleanup_failed = True
                if primary_error is not None:
                    _append_exception_note(
                        primary_error,
                        "sidecar watchdog baseline close was incomplete",
                    )
        for fd in (control_fd, ready_fd, acknowledgement_fd):
            try:
                close_owned_fd(fd)
            except BaseException:
                cleanup_failed = True
                if primary_error is not None:
                    _append_exception_note(
                        primary_error,
                        "sidecar watchdog descriptor close was incomplete",
                    )
    if primary_error is not None:
        raise primary_error
    os._exit(70 if cleanup_failed else exit_code)


def _wait_for_byte(fd: int, timeout: float) -> bytes:
    readable, _, _ = select.select([fd], [], [], timeout)
    if not readable:
        return b""
    return os.read(fd, 1)


def _child_exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 70


@dataclass(frozen=True)
class ChildPoll:
    reaped: bool
    status: int | None


class _WatchdogProtocolError(RuntimeError):
    pass


def _poll_child(pid: int) -> ChildPoll:
    try:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return ChildPoll(True, None)
    if waited_pid == 0:
        return ChildPoll(False, None)
    return ChildPoll(True, status)


def _wait_child_bounded(pid: int, timeout: float) -> ChildPoll:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _poll_child(pid)
        if result.reaped:
            return result
        time.sleep(0.025)
    return _poll_child(pid)


def _capture_forked_identity(
    process_table: DarwinProcessTable,
    pid: int,
    timeout: float,
) -> ProcessIdentity | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        scan = process_table.identity(pid)
        if scan.complete:
            return scan.value
        time.sleep(0.01)
    return None


def _cleanup_supervised_sidecar(
    process_table: DarwinProcessTable,
    marker: str,
    sidecar_pid: int,
    bound: set[ProcessIdentity],
) -> bool:
    reaped = _poll_child(sidecar_pid)
    cleanup_complete = True
    try:
        cleanup_complete = cleanup_marked_processes(
            process_table,
            marker,
            os.getpid(),
            bound_identities=bound,
        )
    except BaseException:
        cleanup_complete = False

    if not reaped.reaped:
        reaped = _wait_child_bounded(sidecar_pid, 0.25)
    if not reaped.reaped:
        root = next((identity for identity in bound if identity.pid == sidecar_pid), None)
        if root is not None:
            _signal_identity(process_table, root, signal.SIGKILL)
        reaped = _wait_child_bounded(sidecar_pid, WATCHDOG_CLEANUP_TIMEOUT)
    return cleanup_complete and reaped.reaped


def _terminate_watchdog_child(watchdog_pid: int) -> bool:
    result = _wait_child_bounded(watchdog_pid, WATCHDOG_CLEANUP_TIMEOUT + 0.5)
    if result.reaped:
        return True
    try:
        os.kill(watchdog_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        return False
    return _wait_child_bounded(watchdog_pid, 1.0).reaped


def _fork_with_owner_marker(marker: str) -> int:
    had_previous = OWNER_MARKER_ENV in os.environ
    previous = os.environ.get(OWNER_MARKER_ENV)
    child = False
    # The supervisor is single-threaded here, so this mutation is confined to
    # the sidecar fork window and inherited before the child can run Python.
    os.environ[OWNER_MARKER_ENV] = marker
    try:
        pid = os.fork()
        child = pid == 0
        return pid
    finally:
        if not child:
            if had_previous:
                if previous is None:
                    raise RuntimeError("owner marker environment changed unexpectedly")
                os.environ[OWNER_MARKER_ENV] = previous
            else:
                os.environ.pop(OWNER_MARKER_ENV, None)


def supervise(
    *,
    parent_pid: int,
    socket_fd: int,
    session_token_fd: int,
    serve_arguments: list[str],
) -> int:
    process_table = DarwinProcessTable()
    parent_scan = process_table.identity(parent_pid)
    supervisor_scan = process_table.identity(os.getpid())
    parent_identity = parent_scan.value
    supervisor_identity = supervisor_scan.value
    if (
        not parent_scan.complete
        or not supervisor_scan.complete
        or parent_identity is None
        or supervisor_identity is None
        or os.getppid() != parent_pid
    ):
        raise RuntimeError("desktop parent identity is unavailable")

    owned_fds = {socket_fd, session_token_fd}
    marker = ""
    watchdog_pid: int | None = None
    sidecar_pid: int | None = None
    watchdog_reaped = False
    sidecar_reaped = False
    protocol_failed = False
    cleanup_failed = False
    primary_error: BaseException | None = None
    result = 70
    bound: set[ProcessIdentity] = set()

    control_read = control_write = -1
    ready_read = ready_write = -1
    acknowledgement_read = acknowledgement_write = -1
    gate_read = gate_write = -1

    def register_pipe() -> tuple[int, int]:
        read_fd, write_fd = os.pipe()
        owned_fds.update((read_fd, write_fd))
        return read_fd, write_fd

    def close_owned_fd(fd: int) -> None:
        if fd not in owned_fds:
            return
        owned_fds.remove(fd)
        os.close(fd)

    def close_child_fds(fds: tuple[int, ...]) -> None:
        for fd in fds:
            if fd < 0:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

    try:
        marker = secrets.token_hex(OWNER_MARKER_BYTES)
        control_read, control_write = register_pipe()
        ready_read, ready_write = register_pipe()
        acknowledgement_read, acknowledgement_write = register_pipe()
        gate_read, gate_write = register_pipe()
        watchdog_pid = os.fork()
        if watchdog_pid == 0:
            close_child_fds(
                (
                    control_write,
                    ready_read,
                    acknowledgement_read,
                    gate_read,
                    gate_write,
                    socket_fd,
                    session_token_fd,
                )
            )
            try:
                _watchdog(
                    parent_identity,
                    supervisor_identity,
                    marker,
                    control_read,
                    ready_write,
                    acknowledgement_write,
                )
            except BaseException:
                os._exit(70)
            os._exit(70)

        close_owned_fd(control_read)
        close_owned_fd(ready_write)
        close_owned_fd(acknowledgement_write)
        if _wait_for_byte(ready_read, WATCHDOG_READY_TIMEOUT) != b"R":
            raise _WatchdogProtocolError("watchdog monitor registration failed")
        close_owned_fd(ready_read)

        sidecar_pid = _fork_with_owner_marker(marker)
        if sidecar_pid == 0:
            close_child_fds((control_write, acknowledgement_read, gate_write))
            gate_command = _wait_for_byte(gate_read, WATCHDOG_READY_TIMEOUT + 0.5)
            close_child_fds((gate_read,))
            if gate_command != b"G":
                os._exit(70)
            environment = os.environ.copy()
            environment[OWNER_MARKER_ENV] = marker
            arguments = [
                sys.executable,
                "-s",
                "-m",
                "parsing_core.serving.serve",
                "--socket-fd",
                str(socket_fd),
                "--session-token-fd",
                str(session_token_fd),
                *serve_arguments,
            ]
            try:
                os.execve(sys.executable, arguments, environment)
            except BaseException:
                os._exit(70)

        close_owned_fd(gate_read)
        close_owned_fd(socket_fd)
        close_owned_fd(session_token_fd)
        sidecar_identity = _capture_forked_identity(
            process_table,
            sidecar_pid,
            WATCHDOG_READY_TIMEOUT,
        )
        if sidecar_identity is None or not _verify_child_binding(
            process_table,
            supervisor_identity,
            sidecar_identity,
        ):
            raise _WatchdogProtocolError("sidecar identity binding failed")
        bound.add(sidecar_identity)

        os.write(control_write, _encode_watchdog_arm(sidecar_identity))
        armed_acknowledgement = _wait_for_byte(
            acknowledgement_read,
            WATCHDOG_READY_TIMEOUT,
        )
        watchdog_poll = _poll_child(watchdog_pid)
        watchdog_reaped = watchdog_poll.reaped
        if armed_acknowledgement != b"A" or watchdog_reaped:
            raise _WatchdogProtocolError("watchdog ARM acknowledgement failed")

        os.write(gate_write, b"G")
        close_owned_fd(gate_write)

        sidecar_status: int | None = None
        while sidecar_status is None:
            watchdog_poll = _poll_child(watchdog_pid)
            if watchdog_poll.reaped:
                watchdog_reaped = True
                raise _WatchdogProtocolError("watchdog exited while sidecar was running")
            sidecar_poll = _poll_child(sidecar_pid)
            if sidecar_poll.reaped:
                sidecar_reaped = True
                sidecar_status = sidecar_poll.status
                break
            _bind_marked_identities(
                process_table,
                marker,
                os.getpid(),
                bound,
            )
            time.sleep(WATCHDOG_POLL_INTERVAL)

        os.write(control_write, b"N")
        close_owned_fd(control_write)
        cleanup_acknowledgement = _wait_for_byte(
            acknowledgement_read,
            WATCHDOG_CLEANUP_TIMEOUT + 0.5,
        )
        close_owned_fd(acknowledgement_read)
        watchdog_poll = _wait_child_bounded(
            watchdog_pid,
            WATCHDOG_CLEANUP_TIMEOUT + 0.5,
        )
        watchdog_reaped = watchdog_poll.reaped
        if (
            cleanup_acknowledgement != b"C"
            or not watchdog_reaped
            or watchdog_poll.status is None
            or _child_exit_code(watchdog_poll.status) != 0
            or sidecar_status is None
        ):
            raise _WatchdogProtocolError("watchdog cleanup failed")
        result = _child_exit_code(sidecar_status)
    except _WatchdogProtocolError:
        protocol_failed = True
        result = 70
    except BaseException as error:
        primary_error = error
    finally:
        if gate_write in owned_fds:
            try:
                close_owned_fd(gate_write)
            except BaseException:
                cleanup_failed = True
        if control_write in owned_fds:
            try:
                close_owned_fd(control_write)
            except BaseException:
                cleanup_failed = True

        if sidecar_pid is not None and (protocol_failed or primary_error is not None):
            if not _cleanup_supervised_sidecar(
                process_table,
                marker,
                sidecar_pid,
                bound,
            ):
                cleanup_failed = True
            sidecar_reaped = _poll_child(sidecar_pid).reaped or sidecar_reaped

        if watchdog_pid is not None and not watchdog_reaped:
            if not _terminate_watchdog_child(watchdog_pid):
                cleanup_failed = True
            else:
                watchdog_reaped = True

        for fd in tuple(owned_fds):
            try:
                close_owned_fd(fd)
            except BaseException:
                cleanup_failed = True

        if cleanup_failed and primary_error is not None:
            _append_exception_note(
                primary_error,
                "sidecar supervisor cleanup was incomplete",
            )

    if primary_error is not None:
        raise primary_error
    if cleanup_failed:
        return 70
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdf2md-sidecar-lifecycle", add_help=False)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--socket-fd", type=int, required=True)
    parser.add_argument("--session-token-fd", type=int, required=True)
    args, serve_arguments = parser.parse_known_args(argv)
    if args.parent_pid <= 1:
        raise RuntimeError("invalid desktop parent PID")
    for value in (args.socket_fd, args.session_token_fd):
        if value <= 2:
            raise RuntimeError("invalid inherited file descriptor")
    return supervise(
        parent_pid=args.parent_pid,
        socket_fd=args.socket_fd,
        session_token_fd=args.session_token_fd,
        serve_arguments=serve_arguments,
    )


def _safe_main(entrypoint: Callable[[], int]) -> int:
    try:
        return entrypoint()
    except BaseException as error:
        try:
            sys.stderr.write("sidecar lifecycle failed\n")
            sys.stderr.flush()
        except BaseException:
            pass
        if type(error) is KeyboardInterrupt:
            return 130
        if type(error) is SystemExit:
            try:
                code = object.__getattribute__(error, "code")
            except BaseException:
                return 70
            if type(code) is int and 0 <= code <= 255:
                return code
        return 70


if __name__ == "__main__":
    sys.exit(_safe_main(main))
