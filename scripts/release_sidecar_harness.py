#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from parsing_core.serving.lifecycle import (
        DarwinProcessTable as DarwinProcessTableType,
    )
    from parsing_core.serving.lifecycle import (
        ProcessIdentity as LifecycleProcessIdentity,
    )
    from parsing_core.serving.lifecycle import (
        UninspectableProcessBaseline,
    )

READY_SCHEMA = "pdf2md.sidecar.ready.v1"
SESSION_HEADER = "X-PDF2MD-Session"
MAX_RESPONSE_BYTES = 4096
STARTUP_TIMEOUT_SECONDS = 10.0
EXIT_TIMEOUT_SECONDS = 5.0
HARNESS_MARKER_ENV = "PDF2MD_RELEASE_HARNESS_MARKER"
REDACTED = "[REDACTED]"
MAX_REDACTION_DEPTH = 6
MAX_REDACTION_ITEMS = 64
MAX_REDACTION_NODES = 256
MAX_REDACTION_TEXT_CHARS = 4096
MAX_REDACTION_BINARY_BYTES = 4096
MAX_REDACTION_OUTPUT_BYTES = 16 * 1024
REDACTION_NODE_RESERVE_BYTES = 32
MAX_REDACTION_CONTENT_BYTES = MAX_REDACTION_OUTPUT_BYTES - (
    (MAX_REDACTION_NODES + MAX_REDACTION_DEPTH + 2) * REDACTION_NODE_RESERVE_BYTES
)
SAFE_REDACTABLE_EXCEPTION_TYPES = {
    AssertionError,
    KeyboardInterrupt,
    OSError,
    RuntimeError,
    SystemExit,
    ValueError,
}
CLEAN_SCAN_TIMEOUT_SECONDS = 0.5
CLEAN_SCAN_POLL_SECONDS = 0.01
CLEAN_SCAN_STABLE_RESULTS = 2
PS_TIMEOUT_SECONDS = 0.5
MAX_PS_OUTPUT_BYTES = 256 * 1024
SPAWN_GATE_BOOTSTRAP = """\
import os
import sys

gate_fd = int(sys.argv[1])
try:
    release = os.read(gate_fd, 1)
finally:
    os.close(gate_fd)
if release != b"G":
    os._exit(70)
os.execve("/bin/bash", ["/bin/bash", *sys.argv[2:]], os.environ.copy())
"""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    started_seconds: int
    started_microseconds: int
    depth: int


class DescendantRelationship(NamedTuple):
    parent_pid: int
    depth: int


class CapturedRelationship(NamedTuple):
    identity: ProcessIdentity
    parent_pid: int


class ReadyPayload(TypedDict):
    schema: str
    host: str
    port: int


class FileDescriptorSource(Protocol):
    def fileno(self) -> int: ...


ProcessSnapshot = dict[int, int]
DescendantRelationships = dict[int, DescendantRelationship]
CapturedIdentities = set[ProcessIdentity]


def _secret_values(token: str, marker: str) -> tuple[str, ...]:
    safe_token = token if type(token) is str and len(token) <= MAX_REDACTION_TEXT_CHARS else ""
    safe_marker = marker if type(marker) is str and len(marker) <= MAX_REDACTION_TEXT_CHARS else ""
    marker_assignment = f"{HARNESS_MARKER_ENV}={safe_marker}" if safe_marker else ""
    values = (marker_assignment, safe_token, safe_marker)
    return tuple(value for value in values if value)


def _redact_truncated_secret_prefixes(value: str, secrets_to_redact: tuple[str, ...]) -> str:
    for secret in secrets_to_redact:
        maximum = min(len(secret) - 1, len(value))
        for length in range(maximum, 0, -1):
            if value.endswith(secret[:length]):
                value = value[:-length] + REDACTED
                break
    return value


def _redact_text(value: str, token: str, marker: str) -> str:
    try:
        if type(value) is not str:
            return REDACTED
        was_truncated = len(value) > MAX_REDACTION_TEXT_CHARS
        bounded = value[:MAX_REDACTION_TEXT_CHARS]
        secrets_to_redact = _secret_values(token, marker)
        for secret in secrets_to_redact:
            bounded = bounded.replace(secret, REDACTED)
        bounded = re.sub(
            r"(?<![\w:/])/(?:[^/\s,'\")\]}]+/)*[^/\s,'\")\]}]+",
            REDACTED,
            bounded,
        )
        bounded = re.sub(r"(?i)\bstderr\b[^\n]*", REDACTED, bounded)
        if was_truncated:
            bounded = _redact_truncated_secret_prefixes(bounded, secrets_to_redact)
            bounded = bounded[: MAX_REDACTION_TEXT_CHARS - len(REDACTED)] + REDACTED
        return bounded[:MAX_REDACTION_TEXT_CHARS]
    except BaseException:
        return REDACTED


def _redact_bytes(value: bytes, token: str, marker: str) -> bytes:
    replacement = REDACTED.encode("ascii")
    try:
        if type(value) is not bytes:
            return replacement
        was_truncated = len(value) > MAX_REDACTION_BINARY_BYTES
        bounded = value[:MAX_REDACTION_BINARY_BYTES]
        for secret in _secret_values(token, marker):
            bounded = bounded.replace(secret.encode("utf-8"), replacement)
        bounded = re.sub(
            rb"(?<![\w:/])/(?:[^/\s,'\")\]}]+/)*[^/\s,'\")\]}]+",
            replacement,
            bounded,
        )
        bounded = re.sub(rb"(?i)\bstderr\b[^\n]*", replacement, bounded)
        if was_truncated:
            bounded = bounded[: MAX_REDACTION_BINARY_BYTES - len(replacement)] + replacement
        return bounded[:MAX_REDACTION_BINARY_BYTES]
    except BaseException:
        return replacement


@dataclass
class _RedactionBudget:
    nodes_remaining: int = MAX_REDACTION_NODES
    content_bytes_remaining: int = MAX_REDACTION_CONTENT_BYTES

    def claim_node(self) -> bool:
        if self.nodes_remaining <= 0:
            return False
        self.nodes_remaining -= 1
        return True

    def bound_text(self, value: str, token: str, marker: str) -> str:
        redacted = _redact_text(value, token, marker)
        maximum_chars = max((self.content_bytes_remaining - len(REDACTED)) // 12, 0)
        if len(redacted) > maximum_chars:
            redacted = redacted[:maximum_chars] + REDACTED
        cost = min((len(redacted) * 12) + 2, self.content_bytes_remaining)
        self.content_bytes_remaining -= cost
        return redacted

    def bound_bytes(self, value: bytes, token: str, marker: str) -> bytes:
        redacted = _redact_bytes(value, token, marker)
        maximum_bytes = max((self.content_bytes_remaining - len(REDACTED)) // 4, 0)
        if len(redacted) > maximum_bytes:
            redacted = redacted[:maximum_bytes] + REDACTED.encode("ascii")
        cost = min((len(redacted) * 4) + 3, self.content_bytes_remaining)
        self.content_bytes_remaining -= cost
        return redacted


def _redact_value(
    value: object,
    token: str,
    marker: str,
    *,
    depth: int,
    seen: set[int],
    budget: _RedactionBudget,
) -> object:
    if depth > MAX_REDACTION_DEPTH or not budget.claim_node():
        return REDACTED
    value_type = type(value)
    if value_type is str:
        return budget.bound_text(cast(str, value), token, marker)
    if value_type is bytes:
        return budget.bound_bytes(cast(bytes, value), token, marker)
    if value_type in {type(None), bool, float}:
        return value
    if value_type is int:
        integer = cast(int, value)
        return integer if integer.bit_length() <= 128 else REDACTED
    if value_type not in {dict, list, tuple, set, frozenset}:
        return REDACTED

    identifier = id(value)
    if identifier in seen:
        return REDACTED
    seen.add(identifier)
    try:
        if value_type is dict:
            result: dict[object, object] = {}
            mapping = cast(dict[object, object], value)
            for index, (key, item) in enumerate(mapping.items()):
                if index >= MAX_REDACTION_ITEMS or budget.nodes_remaining <= 0:
                    break
                redacted_key = _redact_value(
                    key,
                    token,
                    marker,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
                if type(redacted_key) in {dict, list, set}:
                    redacted_key = REDACTED
                result[redacted_key] = _redact_value(
                    item,
                    token,
                    marker,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
            return result
        sequence = cast(list[object] | tuple[object, ...] | set[object] | frozenset[object], value)
        items: list[object] = []
        for index, item in enumerate(sequence):
            if index >= MAX_REDACTION_ITEMS or budget.nodes_remaining <= 0:
                break
            try:
                items.append(
                    _redact_value(
                        item,
                        token,
                        marker,
                        depth=depth + 1,
                        seen=seen,
                        budget=budget,
                    )
                )
            except BaseException:
                return REDACTED
        if value_type is list:
            return items
        if value_type is tuple:
            return tuple(items)
        redacted_set: set[object] = set()
        for item in items:
            try:
                redacted_set.add(item)
            except TypeError:
                redacted_set.add(REDACTED)
        if value_type is frozenset:
            return frozenset(redacted_set)
        if value_type is set:
            return redacted_set
        return items
    finally:
        seen.discard(identifier)


def _exception_attribute(error: BaseException, name: str) -> object | None:
    try:
        value: object = object.__getattribute__(error, name)
        return value
    except BaseException:
        return None


def _set_exception_attribute(error: BaseException, name: str, value: object) -> None:
    try:
        object.__setattr__(error, name, value)
    except BaseException:
        pass


def _replace_exception_args(error: BaseException, arguments: tuple[object, ...]) -> None:
    try:
        object.__setattr__(error, "args", arguments)
    except BaseException:
        pass


def _replace_exception_notes(error: BaseException, notes: list[str]) -> None:
    if sys.version_info < (3, 11):  # noqa: UP036 - release smoke uses Apple Python 3.9.
        return
    try:
        namespace = object.__getattribute__(error, "__dict__")
        if type(namespace) is dict:
            namespace["__notes__"] = notes
    except BaseException:
        pass


def _truncate_exception_graph(error: BaseException) -> None:
    _replace_exception_args(error, (REDACTED,))
    _replace_exception_notes(error, [REDACTED])
    _set_exception_attribute(error, "__cause__", None)
    _set_exception_attribute(error, "__context__", None)
    _set_exception_attribute(error, "__suppress_context__", True)


def _redact_exception(
    error: BaseException,
    token: str,
    marker: str,
    seen: set[int] | None = None,
    *,
    depth: int = 0,
    budget: _RedactionBudget | None = None,
) -> None:
    if type(error) not in SAFE_REDACTABLE_EXCEPTION_TYPES:
        return
    if seen is None:
        seen = set()
    if budget is None:
        budget = _RedactionBudget()
    if depth > MAX_REDACTION_DEPTH or not budget.claim_node():
        _truncate_exception_graph(error)
        return
    identifier = id(error)
    if identifier in seen:
        _truncate_exception_graph(error)
        return
    seen.add(identifier)
    try:
        arguments = _exception_attribute(error, "args")
        if type(arguments) is tuple:
            redacted_argument_items: list[object] = []
            for argument in arguments[:MAX_REDACTION_ITEMS]:
                if budget.nodes_remaining <= 0:
                    break
                redacted_argument_items.append(
                    _redact_value(
                        argument,
                        token,
                        marker,
                        depth=0,
                        seen=set(),
                        budget=budget,
                    )
                )
            redacted_arguments = tuple(redacted_argument_items)
        else:
            redacted_arguments = (REDACTED,)
        _replace_exception_args(error, redacted_arguments)
        notes = _exception_attribute(error, "__notes__")
        if type(notes) is list:
            redacted_notes = []
            for note in notes[:MAX_REDACTION_ITEMS]:
                if not budget.claim_node():
                    break
                if type(note) is str:
                    redacted_notes.append(budget.bound_text(note, token, marker))
                else:
                    redacted_notes.append(REDACTED)
            _replace_exception_notes(error, redacted_notes)
        if depth >= MAX_REDACTION_DEPTH or budget.nodes_remaining <= 0:
            _set_exception_attribute(error, "__cause__", None)
            _set_exception_attribute(error, "__context__", None)
            _set_exception_attribute(error, "__suppress_context__", True)
            return
        cause = _exception_attribute(error, "__cause__")
        context = _exception_attribute(error, "__context__")
        if type(cause) in SAFE_REDACTABLE_EXCEPTION_TYPES:
            assert isinstance(cause, BaseException)
            if id(cause) in seen:
                _set_exception_attribute(error, "__cause__", None)
            else:
                _redact_exception(
                    cause,
                    token,
                    marker,
                    seen,
                    depth=depth + 1,
                    budget=budget,
                )
        elif cause is not None:
            _set_exception_attribute(error, "__cause__", None)
        if type(context) in SAFE_REDACTABLE_EXCEPTION_TYPES:
            assert isinstance(context, BaseException)
            if id(context) in seen:
                _set_exception_attribute(error, "__context__", None)
                _set_exception_attribute(error, "__suppress_context__", True)
            else:
                _redact_exception(
                    context,
                    token,
                    marker,
                    seen,
                    depth=depth + 1,
                    budget=budget,
                )
        elif context is not None:
            _set_exception_attribute(error, "__context__", None)
            _set_exception_attribute(error, "__suppress_context__", True)
    except BaseException:
        _truncate_exception_graph(error)
    finally:
        seen.discard(identifier)


def _append_exception_note(error: BaseException, note: str) -> None:
    if sys.version_info >= (3, 11):  # noqa: UP036 - release smoke uses 3.9.
        try:
            BaseException.add_note(error, note)
        except BaseException:
            pass
    else:
        try:
            sys.stderr.write("release sidecar cleanup was incomplete\n")
            sys.stderr.flush()
        except BaseException:
            pass


def _safe_exception_text(error: BaseException) -> str:
    if type(error) not in SAFE_REDACTABLE_EXCEPTION_TYPES:
        return REDACTED
    try:
        return BaseException.__str__(error)
    except BaseException:
        return REDACTED


def _safe_boundary_exception(error: BaseException, token: str, marker: str) -> BaseException:
    if type(error) not in SAFE_REDACTABLE_EXCEPTION_TYPES:
        return RuntimeError("release sidecar smoke failed")
    try:
        _redact_exception(error, token, marker)
    except BaseException:
        return RuntimeError("release sidecar smoke failed")
    return error


def _process_snapshot() -> ProcessSnapshot:
    command = ["/bin/ps", "-axo", "pid=,ppid="]
    process: subprocess.Popen[bytes] | None = None
    stdout: FileDescriptorSource | None = None
    selector: selectors.BaseSelector | None = None
    output = bytearray()
    primary_error: BaseException | None = None
    cleanup_failed = False
    reaped = False
    try:
        process = subprocess.Popen(
            command,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = process.stdout
        if stdout is None:
            raise subprocess.SubprocessError("process table stdout is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + PS_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, PS_TIMEOUT_SECONDS)
            events = selector.select(min(remaining, 0.05))
            if not events:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(
                stdout.fileno(),
                min(8192, MAX_PS_OUTPUT_BYTES + 1 - len(output)),
            )
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_PS_OUTPUT_BYTES:
                raise subprocess.SubprocessError("process table output exceeded limit")
        status = process.wait(timeout=max(deadline - time.monotonic(), 0.01))
        reaped = True
        if status != 0:
            raise subprocess.CalledProcessError(status, command)
    except BaseException as error:
        primary_error = error
    finally:
        if process is not None and not reaped:
            running = True
            try:
                running = process.poll() is None
            except BaseException:
                cleanup_failed = True
            if running:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except BaseException:
                    cleanup_failed = True
            try:
                process.wait(timeout=0.25)
            except BaseException:
                cleanup_failed = True
        if selector is not None:
            try:
                selector.close()
            except BaseException:
                cleanup_failed = True
        if stdout is not None:
            try:
                stdout.close()  # type: ignore[attr-defined]
            except BaseException:
                cleanup_failed = True

    if primary_error is not None:
        raise primary_error
    if cleanup_failed:
        raise RuntimeError("process table cleanup failed") from None
    try:
        decoded = bytes(output).decode("ascii")
    except UnicodeDecodeError as error:
        raise subprocess.SubprocessError("process table output was invalid") from error
    rows: ProcessSnapshot = {}
    for line in decoded.splitlines():
        fields = line.strip().split()
        if len(fields) != 2:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        rows[pid] = ppid
    return rows


def _descendants(
    rows: ProcessSnapshot,
    root_pid: int,
) -> DescendantRelationships:
    depths: dict[int, int] = {root_pid: 0}
    changed = True
    while changed:
        changed = False
        for pid, ppid in rows.items():
            if pid in depths or ppid not in depths:
                continue
            depths[pid] = depths[ppid] + 1
            changed = True
    return {
        pid: DescendantRelationship(rows[pid], depth)
        for pid, depth in depths.items()
        if pid != root_pid
    }


def _capture_identity(
    process_table: DarwinProcessTableType,
    pid: int,
    depth: int,
) -> ProcessIdentity | None:
    scan = process_table.identity(pid)
    if not scan.complete or scan.value is None:
        return None
    identity = scan.value
    return ProcessIdentity(
        pid=identity.pid,
        uid=identity.uid,
        started_seconds=identity.started_seconds,
        started_microseconds=identity.started_microseconds,
        depth=depth,
    )


def _same_process(
    process_table: DarwinProcessTableType,
    identity: ProcessIdentity,
) -> bool | None:
    scan = process_table.identity(identity.pid)
    if not scan.complete:
        return None
    current = scan.value
    if current is None:
        return False
    return (
        current.pid == identity.pid
        and current.uid == identity.uid
        and current.started_seconds == identity.started_seconds
        and current.started_microseconds == identity.started_microseconds
    )


def _capture_descendants(
    process_table: DarwinProcessTableType,
    root_identity: ProcessIdentity,
) -> CapturedIdentities:
    if _same_process(process_table, root_identity) is not True:
        return set()
    first_rows = _process_snapshot()
    first_relationships = _descendants(first_rows, root_identity.pid)
    first: set[CapturedRelationship] = set()
    for pid, relationship in first_relationships.items():
        if pid == os.getpid():
            continue
        identity = _capture_identity(process_table, pid, relationship.depth)
        if identity is not None:
            first.add(CapturedRelationship(identity, relationship.parent_pid))
    second_rows = _process_snapshot()
    second_relationships = _descendants(second_rows, root_identity.pid)
    if _same_process(process_table, root_identity) is not True:
        return set()
    return {
        identity
        for identity, parent_pid in first
        if second_relationships.get(identity.pid)
        == DescendantRelationship(parent_pid, identity.depth)
        and _same_process(process_table, identity) is True
    }


def _signal_identities(
    process_table: DarwinProcessTableType,
    identities: CapturedIdentities,
    sig: signal.Signals,
) -> None:
    for identity in sorted(identities, key=lambda item: item.depth, reverse=True):
        for _ in range(2):
            if _same_process(process_table, identity) is not True:
                break
        else:
            try:
                os.kill(identity.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            _same_process(process_table, identity)


def _wait_identities_gone(
    process_table: DarwinProcessTableType,
    identities: CapturedIdentities,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = [_same_process(process_table, identity) for identity in identities]
        if states and all(state is False for state in states):
            return True
        if not states:
            return True
        time.sleep(0.05)
    return False


def _cleanup_process_tree(
    process: subprocess.Popen[bytes],
    process_table: DarwinProcessTableType,
    root_identity: ProcessIdentity | None,
    captured: CapturedIdentities,
) -> None:
    try:
        if root_identity is not None:
            captured.update(_capture_descendants(process_table, root_identity))
    except (OSError, subprocess.SubprocessError):
        pass

    targets = set(captured)
    if root_identity is not None:
        targets.add(root_identity)
    _signal_identities(process_table, targets, signal.SIGTERM)
    if not _wait_identities_gone(process_table, targets, 0.75):
        _signal_identities(process_table, targets, signal.SIGKILL)

    if process.poll() is None:
        try:
            process.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            _signal_identities(process_table, targets, signal.SIGKILL)
            process.wait(timeout=2)
    else:
        process.wait()

    _signal_identities(process_table, targets, signal.SIGKILL)
    if not _wait_identities_gone(process_table, targets, 2.0):
        live = [
            str(identity.pid)
            for identity in targets
            if _same_process(process_table, identity) is not False
        ]
        raise RuntimeError("release sidecar retained child processes: " + ",".join(live))


def _signal_bound_identity(
    process_table: DarwinProcessTableType,
    identity: LifecycleProcessIdentity,
    sig: signal.Signals,
) -> bool:
    for _ in range(2):
        identity_scan = process_table.identity(identity.pid)
        if not identity_scan.complete:
            return False
        if identity_scan.value != identity:
            return True
    try:
        os.kill(identity.pid, sig)
    except ProcessLookupError:
        signaled = True
    except PermissionError:
        signaled = False
    else:
        signaled = True
    process_table.identity(identity.pid)
    return signaled


def _cleanup_marked_processes(
    process_table: DarwinProcessTableType,
    marker: str,
    uninspectable_pid_baseline: UninspectableProcessBaseline,
    bound_identities: set[LifecycleProcessIdentity] | None = None,
) -> bool:
    deadline = time.monotonic() + 2.0
    bound = bound_identities if bound_identities is not None else set()
    unresolved: set[LifecycleProcessIdentity] = set()
    stable_empty_scans = 0
    sent_term = False
    while time.monotonic() < deadline:
        marker_scan = process_table.marker_identities(
            marker,
            os.getpid(),
            HARNESS_MARKER_ENV,
            uninspectable_pid_baseline=uninspectable_pid_baseline,
        )
        scan_complete = marker_scan.complete
        for identity in marker_scan.value - bound:
            verified = True
            for _ in range(2):
                marker_verification = process_table.same_process_with_marker(
                    identity,
                    marker,
                    HARNESS_MARKER_ENV,
                )
                scan_complete = scan_complete and marker_verification.complete
                if not marker_verification.complete or not marker_verification.value:
                    verified = False
                    break
            if verified:
                bound.add(identity)
                unresolved.discard(identity)
            else:
                unresolved.add(identity)

        live_bound: set[LifecycleProcessIdentity] = set()
        for identity in bound | unresolved:
            identity_scan = process_table.identity(identity.pid)
            scan_complete = scan_complete and identity_scan.complete
            if identity_scan.complete and identity_scan.value == identity:
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
            sig = signal.SIGTERM if not sent_term else signal.SIGKILL
            signaled = False
            for identity in live_bound:
                signaled = _signal_bound_identity(process_table, identity, sig) or signaled
            if signaled:
                sent_term = True
        time.sleep(0.05)
    return False


def _bind_marked_identities(
    process_table: DarwinProcessTableType,
    marker: str,
    uninspectable_pid_baseline: UninspectableProcessBaseline,
    bound: set[LifecycleProcessIdentity],
) -> bool:
    marker_scan = process_table.marker_identities(
        marker,
        os.getpid(),
        HARNESS_MARKER_ENV,
        uninspectable_pid_baseline=uninspectable_pid_baseline,
    )
    complete = marker_scan.complete
    for identity in marker_scan.value - bound:
        for _ in range(2):
            verification = process_table.same_process_with_marker(
                identity,
                marker,
                HARNESS_MARKER_ENV,
            )
            complete = complete and verification.complete
            if not verification.complete or not verification.value:
                complete = False
                break
        else:
            bound.add(identity)
    return complete


def _session_token_pipe(token: str) -> int:
    read_fd, write_fd = os.pipe()
    owned = {read_fd, write_fd}
    primary_error: BaseException | None = None

    def close_owned(fd: int) -> None:
        if fd not in owned:
            return
        owned.remove(fd)
        os.close(fd)

    try:
        os.set_inheritable(read_fd, True)
        token_bytes = token.encode("ascii")
        if os.write(write_fd, token_bytes) != len(token_bytes):
            raise RuntimeError("release sidecar token pipe write was incomplete")
        close_owned(write_fd)
    except BaseException as error:
        primary_error = error
    finally:
        if primary_error is not None:
            for fd in (write_fd, read_fd):
                try:
                    close_owned(fd)
                except BaseException:
                    pass
    if primary_error is not None:
        raise primary_error
    owned.remove(read_fd)
    return read_fd


def _read_ready_line(
    process: subprocess.Popen[bytes],
    token: str,
    marker: str = "",
    on_poll: Callable[[], None] | None = None,
) -> ReadyPayload:
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("release sidecar stdout pipe is unavailable")
    descriptor = stdout.fileno()
    chunks = bytearray()
    selector: selectors.BaseSelector | None = None
    was_blocking: bool | None = None
    primary_error: BaseException | None = None
    cleanup_failed = False
    line = b""
    try:
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        was_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while b"\n" not in chunks and len(chunks) <= MAX_RESPONSE_BYTES:
            if on_poll is not None:
                on_poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("release sidecar did not emit a ready line")
            events = selector.select(min(remaining, 0.05))
            if not events:
                continue
            try:
                chunk = os.read(
                    descriptor,
                    MAX_RESPONSE_BYTES + 1 - len(chunks),
                )
            except BlockingIOError:
                continue
            if not chunk:
                break
            chunks.extend(chunk)
        newline = chunks.find(b"\n")
        line = bytes(chunks if newline < 0 else chunks[: newline + 1])
    except BaseException as error:
        primary_error = error
    finally:
        if selector is not None:
            try:
                selector.close()
            except BaseException:
                cleanup_failed = True
        if was_blocking is not None:
            try:
                os.set_blocking(descriptor, was_blocking)
            except BaseException:
                cleanup_failed = True
    if primary_error is not None:
        raise primary_error
    if cleanup_failed:
        raise RuntimeError("release sidecar ready reader cleanup failed") from None
    if not line or len(line) > MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
        raise RuntimeError("release sidecar emitted an invalid ready line")
    exposed_secret = token.encode("ascii") in line or (
        bool(marker) and marker.encode("ascii") in line
    )
    if exposed_secret:
        raise RuntimeError("release sidecar exposed a secret in ready output")
    try:
        payload_object: object = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("release sidecar emitted invalid ready JSON") from error
    if not isinstance(payload_object, dict):
        raise RuntimeError("release sidecar ready JSON has unexpected fields")
    payload: dict[object, object] = payload_object
    if set(payload) != {"schema", "host", "port"}:
        raise RuntimeError("release sidecar ready JSON has unexpected fields")
    if payload["schema"] != READY_SCHEMA or payload["host"] != "127.0.0.1":
        raise RuntimeError("release sidecar ready JSON has an invalid endpoint")
    port = payload["port"]
    if isinstance(port, bool) or not isinstance(port, int):
        raise RuntimeError("release sidecar ready JSON has an invalid port")
    return {"schema": READY_SCHEMA, "host": "127.0.0.1", "port": port}


def _read_available(pipe: FileDescriptorSource | None, limit: int) -> bytes:
    if pipe is None:
        return b""
    selector: selectors.BaseSelector | None = None
    primary_error: BaseException | None = None
    cleanup_failed = False
    result = b""
    try:
        selector = selectors.DefaultSelector()
        selector.register(pipe, selectors.EVENT_READ)
        if selector.select(0.05):
            result = os.read(pipe.fileno(), limit)
    except BaseException as error:
        primary_error = error
    finally:
        if selector is not None:
            try:
                selector.close()
            except BaseException:
                cleanup_failed = True
    if primary_error is not None:
        raise primary_error
    if cleanup_failed:
        raise RuntimeError("release sidecar available reader cleanup failed") from None
    return result


def _request_json(
    port: int,
    method: str,
    path: str,
    token: str,
    expected: Mapping[str, str],
) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
    try:
        connection.request(
            method,
            path,
            body=b"" if method == "POST" else None,
            headers={SESSION_HEADER: token, "Connection": "close"},
        )
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()
    if response.status != 200 or len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("release sidecar rejected authenticated " + path)
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("release sidecar returned invalid JSON for " + path) from error
    if payload != expected:
        raise RuntimeError("release sidecar returned an invalid response for " + path)


def _wait_for_clean_process_tree(
    process_table: DarwinProcessTableType,
    marker: str,
    uninspectable_pid_baseline: UninspectableProcessBaseline,
) -> None:
    deadline = time.monotonic() + CLEAN_SCAN_TIMEOUT_SECONDS
    stable_empty_results = 0
    while True:
        retained = process_table.marker_identities(
            marker,
            os.getpid(),
            HARNESS_MARKER_ENV,
            uninspectable_pid_baseline=uninspectable_pid_baseline,
        )
        if retained.complete and not retained.value:
            stable_empty_results += 1
            if stable_empty_results >= CLEAN_SCAN_STABLE_RESULTS:
                return
        else:
            stable_empty_results = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("release sidecar did not clean its own process tree")
        time.sleep(min(CLEAN_SCAN_POLL_SECONDS, remaining))


def run(app: Path, home: Path) -> None:
    resources_source = app / "Contents/Resources/src"
    sys.path.insert(0, str(resources_source))
    try:
        from parsing_core.serving.lifecycle import DarwinProcessTable
    finally:
        while str(resources_source) in sys.path:
            sys.path.remove(str(resources_source))

    launcher = app / "Contents/MacOS/python3"
    token = ""
    harness_marker = ""
    process_table: DarwinProcessTableType | None = None
    uninspectable_pid_baseline: UninspectableProcessBaseline | None = None
    listener: socket.socket | None = None
    token_fd: int | None = None
    spawn_gate_read_fd: int | None = None
    spawn_gate_write_fd: int | None = None
    process: subprocess.Popen[bytes] | None = None
    captured: CapturedIdentities = set()
    root_identity: ProcessIdentity | None = None
    bound_marker_identities: set[LifecycleProcessIdentity] = set()
    primary_error: BaseException | None = None
    primary_cause: BaseException | None = None
    cleanup_failures: list[str] = []

    def record_cleanup_failure(prefix: str, error: BaseException | None = None) -> None:
        if error is not None:
            safe_error = _safe_boundary_exception(error, token, harness_marker)
            detail = _safe_exception_text(safe_error)
            if detail:
                prefix += ": " + detail
        cleanup_failures.append(prefix)

    try:
        token = secrets.token_urlsafe(48)
        process_table = DarwinProcessTable()
        baseline_scan = process_table.uninspectable_pid_baseline()
        if not baseline_scan.complete or baseline_scan.value is None:
            raise RuntimeError("release sidecar process baseline is incomplete")
        uninspectable_pid_baseline = baseline_scan.value
        harness_marker = secrets.token_hex(32)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.set_inheritable(True)
        port = listener.getsockname()[1]
        token_fd = _session_token_pipe(token)
        spawn_gate_read_fd, spawn_gate_write_fd = os.pipe()
        os.set_inheritable(spawn_gate_read_fd, True)
        listener_fd = listener.fileno()
        environment = {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            HARNESS_MARKER_ENV: harness_marker,
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                SPAWN_GATE_BOOTSTRAP,
                str(spawn_gate_read_fd),
                str(launcher),
                "--parent-pid",
                str(os.getpid()),
                "--socket-fd",
                str(listener_fd),
                "--session-token-fd",
                str(token_fd),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(listener_fd, token_fd, spawn_gate_read_fd),
            start_new_session=True,
        )
        owned_listener = listener
        listener = None
        owned_listener.close()
        owned_token_fd = token_fd
        token_fd = None
        os.close(owned_token_fd)

        root_scan = process_table.identity(process.pid)
        if not root_scan.complete or root_scan.value is None:
            raise RuntimeError("release sidecar root identity is unavailable")
        root_marker_identity = root_scan.value
        root_identity = ProcessIdentity(
            pid=root_marker_identity.pid,
            uid=root_marker_identity.uid,
            started_seconds=root_marker_identity.started_seconds,
            started_microseconds=root_marker_identity.started_microseconds,
            depth=0,
        )
        marker_deadline = time.monotonic() + 0.5
        consecutive_marker_checks = 0
        while consecutive_marker_checks < 2:
            if time.monotonic() >= marker_deadline:
                raise RuntimeError("release sidecar root marker binding failed")
            marker_scan = process_table.same_process_with_marker(
                root_marker_identity,
                harness_marker,
                HARNESS_MARKER_ENV,
            )
            if marker_scan.complete and marker_scan.value:
                consecutive_marker_checks += 1
                continue
            consecutive_marker_checks = 0
            time.sleep(0.01)
        bound_marker_identities.add(root_marker_identity)
        if os.write(spawn_gate_write_fd, b"G") != 1:
            raise RuntimeError("release sidecar spawn gate write was incomplete")
        owned_gate_write_fd = spawn_gate_write_fd
        spawn_gate_write_fd = None
        os.close(owned_gate_write_fd)
        owned_gate_read_fd = spawn_gate_read_fd
        spawn_gate_read_fd = None
        os.close(owned_gate_read_fd)
        last_descendant_capture = 0.0
        assert uninspectable_pid_baseline is not None
        bound_baseline = uninspectable_pid_baseline

        def bind_running_processes() -> None:
            nonlocal last_descendant_capture
            _bind_marked_identities(
                process_table,
                harness_marker,
                bound_baseline,
                bound_marker_identities,
            )
            now = time.monotonic()
            if now - last_descendant_capture >= 0.2:
                captured.update(_capture_descendants(process_table, root_identity))
                last_descendant_capture = now

        ready = _read_ready_line(
            process,
            token,
            harness_marker,
            bind_running_processes,
        )
        bind_running_processes()
        if ready["port"] != port:
            raise RuntimeError("release sidecar ready port does not match inherited socket")
        _request_json(port, "GET", "/health", token, {"status": "ok"})
        captured.update(_capture_descendants(process_table, root_identity))
        _request_json(
            port,
            "POST",
            "/shutdown",
            token,
            {"status": "shutting_down"},
        )
        try:
            status = process.wait(timeout=EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "release sidecar did not exit after shutdown acknowledgement"
            ) from error
        if status != 0:
            raise RuntimeError("release sidecar exited with nonzero status")
        _wait_for_clean_process_tree(
            process_table,
            harness_marker,
            uninspectable_pid_baseline,
        )
    except BaseException as error:
        primary_error = _safe_boundary_exception(error, token, harness_marker)
        if (
            type(error) in {AssertionError, OSError, RuntimeError, ValueError}
            and process is not None
        ):
            try:
                stderr = _read_available(process.stderr, MAX_RESPONSE_BYTES)
            except BaseException:
                cleanup_failures.append("release sidecar stderr read failed")
            else:
                detail = _redact_bytes(stderr, token, harness_marker).decode(
                    "utf-8",
                    errors="replace",
                )
                if detail:
                    primary_error = RuntimeError(
                        _safe_exception_text(error) + ": " + detail.strip()
                    )
                    primary_cause = error
    finally:
        if spawn_gate_write_fd is not None:
            owned_gate_write_fd = spawn_gate_write_fd
            spawn_gate_write_fd = None
            try:
                os.close(owned_gate_write_fd)
            except BaseException as error:
                record_cleanup_failure(
                    "release sidecar spawn gate write close failed",
                    error,
                )
        if spawn_gate_read_fd is not None:
            owned_gate_read_fd = spawn_gate_read_fd
            spawn_gate_read_fd = None
            try:
                os.close(owned_gate_read_fd)
            except BaseException as error:
                record_cleanup_failure(
                    "release sidecar spawn gate read close failed",
                    error,
                )
        if listener is not None:
            owned_listener = listener
            listener = None
            try:
                owned_listener.close()
            except BaseException as error:
                record_cleanup_failure("release sidecar listener close failed", error)
        if token_fd is not None:
            owned_token_fd = token_fd
            token_fd = None
            try:
                os.close(owned_token_fd)
            except BaseException as error:
                record_cleanup_failure("release sidecar token descriptor close failed", error)
        if process_table is not None and uninspectable_pid_baseline is not None and harness_marker:
            try:
                marked_cleaned = _cleanup_marked_processes(
                    process_table,
                    harness_marker,
                    uninspectable_pid_baseline,
                    bound_marker_identities,
                )
                if not marked_cleaned:
                    record_cleanup_failure("release sidecar marker cleanup was incomplete")
            except BaseException as error:
                record_cleanup_failure("release sidecar marker cleanup failed", error)
        if process is not None and process_table is not None:
            try:
                _cleanup_process_tree(
                    process,
                    process_table,
                    root_identity,
                    captured,
                )
            except BaseException as error:
                record_cleanup_failure(
                    "release sidecar process-tree cleanup failed",
                    error,
                )
        if process is not None:
            if process.stdout is not None:
                owned_stdout = process.stdout
                process.stdout = None
                try:
                    owned_stdout.close()
                except BaseException as error:
                    record_cleanup_failure(
                        "release sidecar stdout close failed",
                        error,
                    )
            if process.stderr is not None:
                owned_stderr = process.stderr
                process.stderr = None
                try:
                    owned_stderr.close()
                except BaseException as error:
                    record_cleanup_failure(
                        "release sidecar stderr close failed",
                        error,
                    )
        if uninspectable_pid_baseline is not None:
            owned_baseline = uninspectable_pid_baseline
            uninspectable_pid_baseline = None
            try:
                owned_baseline.close()
            except BaseException as error:
                record_cleanup_failure(
                    "release sidecar process baseline close failed",
                    error,
                )

    if cleanup_failures:
        cleanup_detail = _redact_text(
            "; ".join(cleanup_failures),
            token,
            harness_marker,
        )
        if primary_error is None:
            raise RuntimeError(cleanup_detail)
        _append_exception_note(primary_error, "cleanup also failed: " + cleanup_detail)
        primary_error = _safe_boundary_exception(primary_error, token, harness_marker)
    if primary_error is not None:
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    args.home.mkdir(parents=True, exist_ok=True)
    run(args.app, args.home)
    return 0


def _safe_main(entrypoint: Callable[[], int]) -> int:
    try:
        return entrypoint()
    except BaseException as error:
        try:
            sys.stderr.write("release sidecar smoke failed\n")
            sys.stderr.flush()
        except BaseException:
            pass
        if type(error) is KeyboardInterrupt:
            return 130
        if type(error) is SystemExit:
            code = _exception_attribute(error, "code")
            if type(code) is int and 0 <= code <= 255:
                return code
        return 1


if __name__ == "__main__":
    sys.exit(_safe_main(main))
