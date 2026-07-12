#!/usr/bin/env python3
import argparse
import ctypes
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


def _contained(path: PurePosixPath) -> bool:
    return not path.is_absolute() and ".." not in path.parts


def validate_archive(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            safe = _contained(name) and (member.isfile() or member.isdir())
            if member.issym():
                safe = _contained(PurePosixPath(*name.parent.parts, member.linkname))
            elif member.islnk():
                safe = _contained(PurePosixPath(member.linkname))
            if not safe:
                raise ValueError(f"unsafe archive member: {member.name}")


def atomic_install(staged: Path, target: Path) -> None:
    if staged.parent != target.parent:
        raise ValueError("staged runtime and target must share a parent directory")
    if not target.exists():
        os.rename(staged, target)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise OSError("atomic directory exchange is unavailable")
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    if renamex_np(os.fsencode(staged), os.fsencode(target), 0x00000002) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _process_start(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return value or None


def _write_owner(directory: Path, token: str, pid: int, process_start: str) -> None:
    directory.mkdir()
    (directory / "owner.json").write_text(
        json.dumps(
            {"token": token, "pid": pid, "process_start": process_start},
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _publish_owner(lock: Path, token: str) -> None:
    owner_pid = os.getpid()
    process_start = _process_start(owner_pid)
    if process_start is None:
        raise OSError(f"cannot identify lock owner process: {owner_pid}")
    pending = lock.with_name(f".{lock.name}.owner.{token}")
    shutil.rmtree(pending, ignore_errors=True)
    _write_owner(pending, token, owner_pid, process_start)
    stale = lock.with_name(f"{lock.name}.stale.{token}")
    shutil.rmtree(stale, ignore_errors=True)
    if lock.exists():
        os.rename(lock, stale)
    os.rename(pending, lock)
    shutil.rmtree(stale, ignore_errors=True)


def _remove_owner(lock: Path, token: str) -> None:
    try:
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if owner.get("token") != token:
        return
    shutil.rmtree(lock)


def run_with_lock(lock: Path, token: str, command: list[str]) -> int:
    guard = lock.with_name(f"{lock.name}.guard")
    guard.parent.mkdir(parents=True, exist_ok=True)
    with guard.open("a+") as guard_file:
        fcntl.flock(guard_file.fileno(), fcntl.LOCK_EX)
        _publish_owner(lock, token)
        try:
            return subprocess.run(command, check=False).returncode
        finally:
            _remove_owner(lock, token)
            fcntl.flock(guard_file.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-archive")
    validate.add_argument("archive", type=Path)
    install = subparsers.add_parser("atomic-install")
    install.add_argument("staged", type=Path)
    install.add_argument("target", type=Path)
    locked = subparsers.add_parser("run-with-lock")
    locked.add_argument("lock", type=Path)
    locked.add_argument("token")
    locked.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        if args.command == "validate-archive":
            validate_archive(args.archive)
        elif args.command == "atomic-install":
            atomic_install(args.staged, args.target)
        else:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise ValueError("run-with-lock requires a command")
            return run_with_lock(args.lock, args.token, command)
    except (OSError, tarfile.TarError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
