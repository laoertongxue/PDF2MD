#!/usr/bin/env python3
import argparse
import ctypes
import os
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


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-archive")
    validate.add_argument("archive", type=Path)
    install = subparsers.add_parser("atomic-install")
    install.add_argument("staged", type=Path)
    install.add_argument("target", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "validate-archive":
            validate_archive(args.archive)
        else:
            atomic_install(args.staged, args.target)
    except (OSError, tarfile.TarError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
