#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$app_dir/.." && pwd)"

identity="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -n 1 || true)"
if [[ -n "$identity" ]]; then
  export APPLE_SIGNING_IDENTITY="$identity"
  echo "Using Developer ID identity: $identity"
else
  export APPLE_SIGNING_IDENTITY="-"
  echo "No Developer ID Application identity found; using ad-hoc signing." >&2
fi

bash "$app_dir/scripts/prepare-sidecar-python.sh"
bash "$app_dir/scripts/build-vision-ocr.sh"

entitlements="$app_dir/scripts/sidecar-entitlements.plist"

/usr/bin/python3 -I -B "$app_dir/scripts/sign-app-bundle.py" \
  "$app_dir/src-tauri/sidecar-runtime" "${APPLE_SIGNING_IDENTITY:--}" "$entitlements"
/usr/bin/python3 -I -B "$app_dir/scripts/sign-app-bundle.py" \
  "$app_dir/src-tauri/binaries" "${APPLE_SIGNING_IDENTITY:--}" "$entitlements"

if [[ "${1:-}" == "build" ]]; then
  rm -rf "$app_dir/src-tauri/target/release/bundle"
fi

export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$repo_dir=/build/pdf2md --remap-path-prefix=$HOME/.cargo=/build/cargo"

set +e
"$app_dir/node_modules/.bin/tauri" "$@"
status=$?
set -e
if [[ $status -ne 0 ]]; then
  exit $status
fi
