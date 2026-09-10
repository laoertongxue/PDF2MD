#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pdf2md-uv-cache}"
readonly SYSTEM_GIT="/usr/bin/git"
readonly SYSTEM_PYTHON="/usr/bin/python3"

validate_external_tool() {
  local tool_path="$1"
  [[ -n "$tool_path" ]] || return 1
  PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" - "$tool_path" >/dev/null 2>&1 <<'PY'
import os
import stat
import sys
from pathlib import Path

requested = Path(sys.argv[1])
if not requested.is_absolute():
    raise SystemExit(1)
resolved = Path(os.path.realpath(requested))
entry = os.lstat(resolved)
if (
    not stat.S_ISREG(entry.st_mode)
    or entry.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(entry.st_mode) & 0o022
    or not os.access(resolved, os.X_OK)
):
    raise SystemExit(1)
for candidate in {requested.parent, resolved.parent}:
    current = candidate
    while current != current.parent:
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
            raise SystemExit(1)
        current = current.parent
PY
}

scan_tracked_secrets() {
  local fixture_path="tests/test_workbench/test_ocr_codex.py"
  local fixture_digest="e980cdd4563dd3c82b1c70229e147f01c83018185da361aa040c527cfde33980"
  local status

  if [[ ! -x "$SYSTEM_GIT" || ! -x "$SYSTEM_PYTHON" ]]; then
    printf 'Credential scan could not inspect tracked source.\n' >&2
    return 1
  fi

  set +e
  PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" - \
    "$ROOT" "$SYSTEM_GIT" "$fixture_path" "$fixture_digest" \
    >/dev/null 2>&1 <<'PY'
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

scan_root, git_binary, fixture_relative, expected_digest = sys.argv[1:]
root_path = Path(scan_root)
root_stat = os.lstat(root_path)
root = Path(os.path.realpath(root_path))
if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
    raise SystemExit(1)

def git(*arguments: str) -> bytes:
    completed = subprocess.run(
        [git_binary, "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout

repository_root = Path(os.path.realpath(git("rev-parse", "--show-toplevel").decode().strip()))
if repository_root != root:
    raise SystemExit(1)

credential = re.compile(rb"(?:sk-[A-Za-z0-9_-]{32,}|AKID[A-Za-z0-9]{12,})")
entries: dict[str, tuple[str, str]] = {}
for raw_entry in git("ls-files", "-z", "--stage").split(b"\0"):
    if not raw_entry:
        continue
    metadata, raw_path = raw_entry.split(b"\t", 1)
    mode, object_id, stage = metadata.decode("ascii").split()
    path = raw_path.decode("utf-8", "surrogateescape")
    pure = PurePosixPath(path)
    if stage != "0" or not pure.parts or pure.is_absolute() or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise SystemExit(1)
    if path in entries:
        raise SystemExit(1)
    entries[path] = (mode, object_id)

fixture_entry = entries.get(fixture_relative)
if fixture_entry is None or fixture_entry[0] != "100644":
    raise SystemExit(3)
fixture_index = git("cat-file", "blob", fixture_entry[1])
if hashlib.sha256(fixture_index).hexdigest() != expected_digest:
    raise SystemExit(3)

def worktree_bytes(relative: str, *, fixture: bool = False) -> bytes | None:
    pure = PurePosixPath(relative)
    current = root
    for part in pure.parts[:-1]:
        current /= part
        try:
            entry = os.lstat(current)
        except OSError:
            raise SystemExit(3 if fixture else 1) from None
        if not stat.S_ISDIR(entry.st_mode):
            raise SystemExit(3 if fixture else 1)
    path = current / pure.parts[-1]
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise SystemExit(3 if fixture else 1) from None
    if stat.S_ISLNK(entry.st_mode):
        return os.readlink(path).encode("utf-8", "surrogateescape")
    if not stat.S_ISREG(entry.st_mode):
        raise SystemExit(3 if fixture else 1)
    try:
        return path.read_bytes()
    except OSError:
        raise SystemExit(3 if fixture else 1) from None

fixture_worktree = worktree_bytes(fixture_relative, fixture=True)
fixture_stat = os.lstat(root / fixture_relative)
if (
    fixture_worktree != fixture_index
    or fixture_stat.st_nlink != 1
    or stat.S_IMODE(fixture_stat.st_mode) & 0o022
):
    raise SystemExit(3)

for relative, (_mode, object_id) in entries.items():
    if relative == fixture_relative:
        continue
    if credential.search(git("cat-file", "blob", object_id)):
        raise SystemExit(2)
    worktree = worktree_bytes(relative)
    if worktree is not None and credential.search(worktree):
        raise SystemExit(2)
PY
  status=$?
  set -e
  case "$status" in
    0) ;;
    2)
      printf 'Potential credential found in tracked source.\n' >&2
      return 1
      ;;
    3)
      printf 'Potential credential found in tracked source.\n' >&2
      return 1
      ;;
    *)
      printf 'Credential scan could not inspect tracked source.\n' >&2
      return 1
      ;;
  esac
}

if [[ "${1:-}" == "--secret-scan-only" ]]; then
  [[ "$#" -eq 1 ]] || {
    printf 'usage: verify-security.sh [--secret-scan-only]\n' >&2
    exit 64
  }
  scan_tracked_secrets
  exit 0
fi

[[ "$#" -eq 0 ]] || {
  printf 'usage: verify-security.sh [--secret-scan-only]\n' >&2
  exit 64
}

cd "$ROOT"

npm_bin="${PDF2MD_NPM_BIN:-}"
uv_bin="${PDF2MD_UV_BIN:-}"
if ! validate_external_tool "$npm_bin" || ! validate_external_tool "$uv_bin"; then
  printf 'Security toolchain paths are invalid.\n' >&2
  exit 69
fi
if [[ "$("$uv_bin" --version)" != "uv 0.12.3 "* ]]; then
  printf 'Security toolchain paths are invalid.\n' >&2
  exit 69
fi
export PATH="$(dirname "$npm_bin"):$(dirname "$uv_bin"):/usr/bin:/bin:/usr/sbin:/sbin"

(
  cd parsing-core-app
  "$npm_bin" audit --audit-level=moderate
)

"$uv_bin" run --frozen pytest \
  tests/test_security.py \
  tests/test_serving/test_api_health.py \
  tests/test_serving/test_api_ws.py \
  tests/test_serving/test_api_ws_network.py \
  -q

scan_tracked_secrets
