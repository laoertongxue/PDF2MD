#!/usr/bin/env bash
set -euo pipefail

source_app="${1:?usage: test-release-sidecar.sh /path/to/PDF2MD.app}"
temporary="$(mktemp -d /tmp/pdf2md-release-test.XXXXXX)"
app="$temporary/PDF2MD.app"
home="$temporary/home"
log="$temporary/sidecar.log"
mkdir -p "$home"
trap '[[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true; rm -rf "$temporary"' EXIT

ditto "$source_app" "$app"
bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check-release-sidecar.sh" "$app"

port="$(PYTHONDONTWRITEBYTECODE=1 "$app/Contents/Resources/python-runtime/bin/python3" - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

env -i \
  HOME="$home" \
  PATH="/usr/bin:/bin" \
  "$app/Contents/MacOS/python3" --host 127.0.0.1 --port "$port" >"$log" 2>&1 &
pid=$!

for _ in $(seq 1 100); do
  if curl --fail --silent "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    cat "$log" >&2
    exit 1
  fi
  sleep 0.1
done
curl --fail --silent "http://127.0.0.1:$port/health"

if find "$app/Contents" -type f \( -name '*.pyc' -o -name '*.pyo' \) -print -quit | grep -q .; then
  echo "cold start wrote Python bytecode into signed bundle" >&2
  exit 1
fi
codesign --verify --deep --strict --verbose=2 "$app"
