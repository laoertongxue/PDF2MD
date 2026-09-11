#!/usr/bin/python3 -I
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NoReturn

MACHO_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
}
CODESIGN = "/usr/bin/codesign"


def fail(message: str) -> NoReturn:
    print(f"sign-app-bundle: {message}", file=sys.stderr)
    raise SystemExit(1)


def sign(target: Path, identity: str, *, deep: bool, entitlements: str | None = None) -> None:
    command = [CODESIGN, "--force"]
    if deep:
        command.append("--deep")
    if entitlements is not None:
        command += ["--entitlements", entitlements]
    command += ["--options", "runtime", "--sign", identity, str(target)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        fail(f"codesign failed for {target}: {detail}")


def main(arguments: list[str]) -> int:
    if len(arguments) not in (2, 3):
        fail("usage: sign-app-bundle.py <tree-or-app> <identity> [entitlements]")
    target = Path(arguments[0])
    identity = arguments[1]
    entitlements = arguments[2] if len(arguments) == 3 else None
    if not identity or target.is_symlink() or not target.is_dir():
        fail("signing tree and identity are required")
    if entitlements is not None and not Path(entitlements).is_file():
        fail("entitlements file is missing")
    for path in sorted(target.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(4) not in MACHO_MAGICS:
                continue
        executable_entitlements = entitlements if path.stat().st_mode & 0o111 else None
        sign(path, identity, deep=False, entitlements=executable_entitlements)
    if target.suffix == ".app":
        sign(target, identity, deep=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
