#!/bin/bash -p
set -euo pipefail

CDPATH=""
IFS=$' \t\n'
umask 077

readonly PYTHON_RELEASE="20260510"
readonly PYTHON_VERSION="3.12.13"
readonly UV_VERSION="0.12.3"
readonly PYTHON_ARCHIVE="cpython-${PYTHON_VERSION}+${PYTHON_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
readonly PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}/cpython-${PYTHON_VERSION}%2B${PYTHON_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
readonly PYTHON_SHA256="5a30271f8d345a5b02b0c9e4e31e0f1e1455a8e4a04fba95cd9762472abc3b17"
readonly CACHE_DIR="${HOME}/Library/Caches/PDF2MD-build"
readonly SYSTEM_PYTHON="/usr/bin/python3"
readonly SYSTEM_UNAME="/usr/bin/uname"
readonly SYSTEM_DIRNAME="/usr/bin/dirname"

export PATH="/usr/bin:/bin"
export LC_ALL="C"

if [[ "$($SYSTEM_UNAME -m)" != "arm64" ]]; then
  printf 'embedded Python runtime requires arm64, got: %s\n' "$($SYSTEM_UNAME -m)" >&2
  exit 64
fi

uv_path="${PDF2MD_UV_BIN:-}"
if [[ -z "$uv_path" ]]; then
  echo "PDF2MD_UV_BIN must name the uv $UV_VERSION executable" >&2
  exit 64
fi
if [[ "$uv_path" != /* ]]; then
  echo "PDF2MD_UV_BIN must be an absolute path" >&2
  exit 64
fi

wheelhouse_root="${PDF2MD_WHEELHOUSE_ROOT:-}"
if [[ -z "$wheelhouse_root" ]]; then
  echo "PDF2MD_WHEELHOUSE_ROOT must name the prefetched locked wheelhouse" >&2
  exit 64
fi
if [[ "$wheelhouse_root" != /* ]]; then
  echo "PDF2MD_WHEELHOUSE_ROOT must be an absolute path" >&2
  exit 64
fi

script_dir="$(cd -P -- "$($SYSTEM_DIRNAME -- "${BASH_SOURCE[0]}")" && pwd -P)"
app_dir="$(cd -P -- "$script_dir/.." && pwd -P)"
repo_dir="$(cd -P -- "$app_dir/.." && pwd -P)"
helper="$script_dir/sidecar_runtime.py"

exec "$SYSTEM_PYTHON" -I -B "$helper" prepare \
  --repo "$repo_dir" \
  --app "$app_dir" \
  --prepare-script "$script_dir/prepare-sidecar-python.sh" \
  --helper "$helper" \
  --cache "$CACHE_DIR" \
  --archive-name "$PYTHON_ARCHIVE" \
  --archive-url "$PYTHON_URL" \
  --archive-sha256 "$PYTHON_SHA256" \
  --python-version "$PYTHON_VERSION" \
  --uv-path "$uv_path" \
  --uv-version "$UV_VERSION" \
  --wheelhouse-root "$wheelhouse_root"
