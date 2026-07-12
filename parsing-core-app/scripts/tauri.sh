#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$app_dir/.." && pwd)"
bash "$app_dir/scripts/prepare-sidecar-python.sh"

if [[ "${1:-}" == "build" ]]; then
  rm -rf "$app_dir/src-tauri/target/release/bundle"
fi

export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$repo_dir=/build/pdf2md --remap-path-prefix=$HOME/.cargo=/build/cargo"

identity="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -n 1 || true)"
if [[ -n "$identity" ]]; then
  export APPLE_SIGNING_IDENTITY="$identity"
  echo "Using Developer ID identity: $identity"
else
  export APPLE_SIGNING_IDENTITY="-"
  echo "No Developer ID Application identity found; using ad-hoc signing." >&2
fi

exec "$app_dir/node_modules/.bin/tauri" "$@"
