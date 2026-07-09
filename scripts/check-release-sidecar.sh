#!/usr/bin/env bash
set -euo pipefail

launcher="parsing-core-app/src-tauri/binaries/python3"

test -x "$launcher"

if grep -En '/Users/laoer|/Users/[^[:space:]]+/Documents/PDF2MD|/\.venv/bin/python3' "$launcher"; then
  echo "release sidecar contains a development-machine path" >&2
  exit 1
fi
