#!/usr/bin/env -S -i PATH=/usr/bin:/bin HOME=/var/empty TMPDIR=/tmp LC_ALL=C LANG=C /bin/bash
set -euo pipefail

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

[[ "$#" -eq 1 ]] || fail "PDF2MD_RELEASE_TEST_E_USAGE"
source_app="$1"
[[ "$source_app" != *$'\n'* && "$source_app" != *$'\r'* ]] || {
  fail "PDF2MD_RELEASE_TEST_E_CONTROL_CHAR"
}
[[ -d "$source_app" && ! -L "$source_app" ]] || {
  fail "PDF2MD_RELEASE_TEST_E_REQUIRED_PATH"
}

case "${BASH_SOURCE[0]}" in
  */*) script_parent=${BASH_SOURCE[0]%/*} ;;
  *) script_parent=. ;;
esac
script_dir="$(cd "$script_parent" && pwd -P)" || fail "PDF2MD_RELEASE_TEST_E_INSPECTION"
temporary="$(/usr/bin/mktemp -d /tmp/pdf2md-release-test.XXXXXX)" || {
  fail "PDF2MD_RELEASE_TEST_E_TEMPORARY"
}
app="$temporary/PDF2MD.app"
home="$temporary/home"
log="$temporary/sidecar.log"
/bin/mkdir -p "$home" || fail "PDF2MD_RELEASE_TEST_E_TEMPORARY"
cleanup() {
  /bin/rm -rf -- "$temporary"
}
trap cleanup EXIT INT TERM HUP

/usr/bin/ditto "$source_app" "$app" || fail "PDF2MD_RELEASE_TEST_E_COPY"
"$script_dir/check-release-sidecar.sh" "$app" >/dev/null || {
  fail "PDF2MD_RELEASE_TEST_E_BUNDLE_CHECK"
}

/usr/bin/env -i \
  HOME="$home" \
  PATH="/usr/bin:/bin" \
  TMPDIR="$temporary" \
  LC_ALL="C" \
  LANG="C" \
  PYTHONDONTWRITEBYTECODE=1 \
  "$app/Contents/Resources/python-runtime/bin/python3.12" \
  "$script_dir/release_sidecar_harness.py" \
  "$app" --home "$home" >"$log" 2>&1 || {
    /bin/cat "$log" >&2
    fail "PDF2MD_RELEASE_TEST_E_COLD_START"
  }

bytecode="$(
  /usr/bin/find "$app/Contents" -type f \( -name '*.pyc' -o -name '*.pyo' \) -print -quit
)" || fail "PDF2MD_RELEASE_TEST_E_SCAN"
[[ -z "$bytecode" ]] || fail "PDF2MD_RELEASE_TEST_E_BYTECODE"

"$script_dir/check-release-sidecar.sh" "$app" >/dev/null || {
  fail "PDF2MD_RELEASE_TEST_E_POST_BUNDLE_CHECK"
}

/usr/bin/codesign --verify --deep --strict --verbose=2 "$app" >/dev/null 2>&1 || {
  fail "PDF2MD_RELEASE_TEST_E_CODESIGN"
}
