#!/usr/bin/env python3
import argparse
import ctypes
import errno
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


def _rename_exclusive(source: Path, target: Path) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise OSError("exclusive rename is unavailable")
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    if renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004) == 0:
        return True
    error = ctypes.get_errno()
    if error in (errno.EEXIST, errno.ENOENT, errno.ENOTEMPTY):
        return False
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


def _owner_is_active(lock: Path) -> bool:
    try:
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
        pid = owner["pid"]
        token = owner["token"]
        process_start = owner["process_start"]
        if not isinstance(pid, int) or not isinstance(token, str) or not token:
            return False
        if not isinstance(process_start, str) or not process_start:
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return _process_start(pid) == process_start


def _claim_and_remove(lock: Path, token: str) -> bool:
    claim = lock.with_name(f"{lock.name}.claim.{token}")
    if not _rename_exclusive(lock, claim):
        return False
    shutil.rmtree(claim)
    return True


def acquire_lock(lock: Path, token: str) -> bool:
    owner_pid = os.getppid()
    process_start = _process_start(owner_pid)
    if process_start is None:
        raise OSError(f"cannot identify lock owner process: {owner_pid}")
    pending = lock.with_name(f".{lock.name}.owner.{token}")
    shutil.rmtree(pending, ignore_errors=True)
    pending.mkdir()
    (pending / "owner.json").write_text(
        json.dumps(
            {"token": token, "pid": owner_pid, "process_start": process_start},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    try:
        while not _rename_exclusive(pending, lock):
            if _owner_is_active(lock):
                return False
            if not _claim_and_remove(lock, token):
                continue
        return True
    finally:
        shutil.rmtree(pending, ignore_errors=True)


def release_lock(lock: Path, token: str) -> None:
    try:
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if owner.get("token") == token:
        _claim_and_remove(lock, token)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-archive")
    validate.add_argument("archive", type=Path)
    install = subparsers.add_parser("atomic-install")
    install.add_argument("staged", type=Path)
    install.add_argument("target", type=Path)
    acquire = subparsers.add_parser("acquire-lock")
    acquire.add_argument("lock", type=Path)
    acquire.add_argument("token")
    release = subparsers.add_parser("release-lock")
    release.add_argument("lock", type=Path)
    release.add_argument("token")
    args = parser.parse_args()

    try:
        if args.command == "validate-archive":
            validate_archive(args.archive)
        elif args.command == "atomic-install":
            atomic_install(args.staged, args.target)
        elif args.command == "acquire-lock":
            if not acquire_lock(args.lock, args.token):
                return 75
        else:
            release_lock(args.lock, args.token)
    except (OSError, tarfile.TarError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
