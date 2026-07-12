#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON_RELEASE="20260510"
readonly PYTHON_VERSION="3.13.13"
readonly PYTHON_ARCHIVE="cpython-${PYTHON_VERSION}+${PYTHON_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
readonly PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}/cpython-${PYTHON_VERSION}%2B${PYTHON_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
readonly PYTHON_SHA256="${PDF2MD_TEST_PYTHON_SHA256:-1ad1ed518447005d4b6dfa16d4f847d45790e17e94e30164a0a6e6c79a99730f}"

machine="${PDF2MD_MACHINE:-$(uname -m)}"
if [[ "$machine" != "arm64" && "$machine" != "x86_64" ]]; then
  echo "embedded Python runtime packaging requires macOS arm64 or x86_64, got: $machine" >&2
  exit 64
fi

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$app_dir/.." && pwd)"
cache_dir="${PDF2MD_BUILD_CACHE:-$HOME/Library/Caches/PDF2MD-build}"
archive="$cache_dir/$PYTHON_ARCHIVE"
target="$app_dir/src-tauri/sidecar-runtime"
launcher="$app_dir/src-tauri/binaries/python3"
expected="$PYTHON_SHA256:$(shasum -a 256 "$repo_dir/pyproject.toml" "$app_dir/scripts/prepare-sidecar-python.sh" | shasum -a 256 | awk '{print $1}')"
helper="$app_dir/scripts/sidecar_runtime.py"
lock="$app_dir/src-tauri/.sidecar-runtime.lock"
lock_token="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
temporary=""
temporary_archive=""

mkdir -p "$cache_dir" "$(dirname "$launcher")"

cleanup() {
  [[ -z "$temporary" ]] || rm -rf "$temporary"
  [[ -z "$temporary_archive" ]] || rm -f "$temporary_archive"
  python3 "$helper" release-lock "$lock" "$lock_token"
}

while true; do
  if python3 "$helper" acquire-lock "$lock" "$lock_token"; then
    break
  else
    status=$?
  fi
  [[ "$status" == 75 ]] || exit "$status"
  sleep 0.1
done
trap cleanup EXIT

sanitize_runtime() {
  local runtime="$1"
  find "$runtime" -type d -name '__pycache__' -prune -exec rm -rf {} +
  find "$runtime" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
  rm -rf "$runtime/lib/python3.13/site-packages/bin"
  rm -rf "$runtime/lib/python3.13/site-packages/pip" "$runtime/share/man" \
    "$runtime/lib/python3.13/config-3.13-darwin"
  find "$runtime/lib/python3.13/site-packages" -type d -name sboms -prune -exec rm -rf {} +
  find "$runtime/lib/python3.13/site-packages" -name direct_url.json -delete
  sed -i '' \
    -e '/expanduser("~\/Library\/Frameworks")/d' \
    -e '/"\/Library\/Frameworks"/d' \
    -e '/"\/Network\/Library\/Frameworks"/d' \
    "$runtime/lib/python3.13/ctypes/macholib/dyld.py"
  sed -i '' \
    -e '/"\/usr\/local\/bin",/d' \
    -e '/"\/opt[^" ]*",/d' \
    "$runtime/lib/python3.13/site-packages/markitdown/_markitdown.py"
  find "$runtime" -type f -name '*.py' -exec chmod a-x {} +
  find "$runtime" -type f -perm -111 -print0 | while IFS= read -r -d '' file; do
    if [[ "$file" != "$runtime/bin/python3.13" ]] && \
      [[ "$(LC_ALL=C head -c 2 "$file" 2>/dev/null || true)" == '#!' ]]; then
      chmod a-x "$file"
    fi
  done
  find "$runtime/bin" -mindepth 1 -maxdepth 1 \
    ! -name 'python' ! -name 'python3' ! -name 'python3.13' \
    -exec rm -rf {} +
}

runtime_is_valid() {
  local candidate="$1"
  local candidate_stamp="$candidate/.runtime-stamp"
  local candidate_manifest="$candidate/.runtime-manifest.sha256"
  local actual_manifest="$candidate/.runtime-manifest.actual.$$"
  [[ -f "$candidate_stamp" && "$(cat "$candidate_stamp")" == "$expected" ]] || return 1
  [[ -f "$candidate_manifest" && -x "$candidate/python/bin/python3" ]] || return 1
  [[ -f "$candidate/python/lib/python3.13/os.py" ]] || return 1
  [[ "$(readlink "$candidate/python/bin/python")" == "python3.13" ]] || return 1
  [[ "$(readlink "$candidate/python/bin/python3")" == "python3.13" ]] || return 1
  file "$candidate/python/bin/python3.13" | grep -q 'arm64' || return 1
  if [[ "$machine" == "arm64" ]]; then
    [[ "$(PYTHONDONTWRITEBYTECODE=1 "$candidate/python/bin/python3" -c 'import platform; print(platform.python_version())')" == "$PYTHON_VERSION" ]] || return 1
  fi
  (cd "$candidate/python" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256) \
    > "$actual_manifest"
  cmp -s "$candidate_manifest" "$actual_manifest" || { rm -f "$actual_manifest"; return 1; }
  rm -f "$actual_manifest"
}

if [[ ! -f "$archive" ]] || [[ "$(shasum -a 256 "$archive" | awk '{print $1}')" != "$PYTHON_SHA256" ]]; then
  temporary_archive="$archive.tmp.$$"
  rm -f "$archive" "$temporary_archive"
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$temporary_archive" "$PYTHON_URL"
  echo "$PYTHON_SHA256  $temporary_archive" | shasum -a 256 --check
  mv "$temporary_archive" "$archive"
  temporary_archive=""
fi

if ! runtime_is_valid "$target"; then
  temporary="$target.tmp.$$"
  rm -rf "$temporary"
  mkdir -p "$temporary"
  python3 "$helper" validate-archive "$archive"
  tar -xzf "$archive" -C "$temporary"
  runtime="$temporary/python"
  test -x "$runtime/bin/python3"
  file "$runtime/bin/python3.13" | grep -q 'arm64'
  if [[ "$machine" == "arm64" ]]; then
    [[ "$(PYTHONDONTWRITEBYTECODE=1 "$runtime/bin/python3" -c 'import platform; print(platform.python_version())')" == "$PYTHON_VERSION" ]]
    "$runtime/bin/python3" -m pip install \
      --disable-pip-version-check \
      --no-compile \
      --target "$runtime/lib/python3.13/site-packages" \
      "$repo_dir[serve]"
  else
    requirements=()
    while IFS= read -r requirement; do
      requirements+=("$requirement")
    done < <(python3 - "$repo_dir/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    project = tomllib.load(handle)["project"]
for requirement in [*project.get("dependencies", []), *project.get("optional-dependencies", {}).get("serve", [])]:
    print(requirement)
PY
    )
    python3 -m pip install \
      --disable-pip-version-check \
      --no-compile \
      --platform macosx_11_0_arm64 \
      --python-version 3.13 \
      --implementation cp \
      --abi cp313 \
      --only-binary=:all: \
      --target "$runtime/lib/python3.13/site-packages" \
      "${requirements[@]}"
  fi
  sanitize_runtime "$runtime"
  (cd "$runtime" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256) \
    > "$temporary/.runtime-manifest.sha256"
  printf '%s\n' "$expected" > "$temporary/.runtime-stamp"
  runtime_is_valid "$temporary"
  python3 "$helper" atomic-install "$temporary" "$target"
  rm -rf "$temporary"
  temporary=""
fi

find "$repo_dir/src" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$repo_dir/src" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

cat > "$launcher" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
resources="$script_dir/../Resources"
runtime="$resources/python-runtime"
python="$runtime/bin/python3"
support="$HOME/Library/Application Support/PDF2MD"

if [[ ! -x "$python" || ! -d "$runtime/lib/python3.13" ]]; then
  echo "bundled Python runtime is incomplete" >&2
  exit 70
fi

mkdir -p "$support/data" "$support/cache" "$support/tmp" "$support/logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$resources/src:$runtime/lib/python3.13/site-packages"
export XDG_DATA_HOME="$support/data"
export XDG_CACHE_HOME="$support/cache"
export TMPDIR="$support/tmp"
exec "$python" -s -m parsing_core.serving.serve "$@"
LAUNCHER
chmod 755 "$launcher"
ln -sfn python3 "$launcher-aarch64-apple-darwin"
