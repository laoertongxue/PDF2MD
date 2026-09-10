from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

_GROUP_POLL_ATTEMPTS = 10
_GROUP_POLL_INTERVAL = 0.01
_CLEANUP_FAILURE = "helper process cleanup failed"


def spawn_isolated_process(
    args: list[str], **popen_kwargs: Any
) -> tuple[subprocess.Popen[bytes], int]:
    caller_process_group_id = os.getpgrp()
    caller_session_id = os.getsid(0)
    process = subprocess.Popen(args, process_group=0, **popen_kwargs)
    process_group_id: int | None = None
    try:
        process_group_id = os.getpgid(process.pid)
        process_session_id = os.getsid(process.pid)
        if (
            process_group_id != process.pid
            or process_group_id == caller_process_group_id
            or process_session_id != caller_session_id
        ):
            raise RuntimeError("helper process group verification failed")
    except BaseException:
        cleanup_group_id = _confirmed_spawned_process_group(
            process,
            observed_process_group_id=process_group_id,
            caller_process_group_id=caller_process_group_id,
        )
        if cleanup_group_id is None:
            _terminate_known_process(process)
        else:
            try:
                terminate_process_group(process, cleanup_group_id)
            finally:
                close_process_pipes(process)
            raise
        close_process_pipes(process)
        raise
    return process, process_group_id


def terminate_process_group(
    process: subprocess.Popen[bytes],
    process_group_id: int,
) -> None:
    try:
        group_is_safe = (
            isinstance(process_group_id, int)
            and process_group_id > 0
            and process_group_id == process.pid
            and process_group_id != os.getpgrp()
        )
    except Exception:
        group_is_safe = False
    if not group_is_safe:
        if getattr(process, "returncode", None) is None:
            _terminate_known_process(process)
        return
    if not _process_group_exists(process_group_id):
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        pass
    if _wait_for_process_group_exit(process, process_group_id):
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        _reap_root_process(process)
        return
    except OSError:
        pass
    if _wait_for_process_group_exit(process, process_group_id):
        _reap_root_process(process)
        return
    _reap_root_process(process)
    if _process_group_exists(process_group_id):
        raise RuntimeError(_CLEANUP_FAILURE)


def close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _confirmed_spawned_process_group(
    process: subprocess.Popen[bytes],
    *,
    observed_process_group_id: int | None,
    caller_process_group_id: int,
) -> int | None:
    process_id = getattr(process, "pid", None)
    if not isinstance(process_id, int) or process_id <= 0:
        return None
    if (
        observed_process_group_id == process_id
        and observed_process_group_id != caller_process_group_id
    ):
        return observed_process_group_id
    try:
        current_process_group_id = os.getpgid(process_id)
    except Exception:
        return None
    if (
        current_process_group_id == process_id
        and current_process_group_id != caller_process_group_id
    ):
        return current_process_group_id
    return None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_for_process_group_exit(process: subprocess.Popen[bytes], process_group_id: int) -> bool:
    for attempt in range(_GROUP_POLL_ATTEMPTS):
        _poll_root_process(process)
        if not _process_group_exists(process_group_id):
            return True
        if attempt + 1 < _GROUP_POLL_ATTEMPTS:
            time.sleep(_GROUP_POLL_INTERVAL)
    return False


def _poll_root_process(process: subprocess.Popen[bytes]) -> None:
    if getattr(process, "returncode", None) is not None:
        return
    try:
        process.poll()
    except Exception:
        pass


def _reap_root_process(process: subprocess.Popen[bytes]) -> None:
    if getattr(process, "returncode", None) is not None:
        return
    try:
        process.wait(timeout=0.1)
    except Exception:
        pass


def _terminate_known_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=0.5)
    except Exception:
        pass
