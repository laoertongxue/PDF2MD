#!/usr/bin/env -S -i PATH=/usr/bin:/bin HOME=/var/empty TMPDIR=/tmp LC_ALL=C LANG=C /bin/bash
set -euo pipefail

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

contains_control_char() {
  [[ "$1" == *$'\n'* || "$1" == *$'\r'* ]]
}

[[ "$#" -eq 2 ]] || fail "PDF2MD_DMG_E_USAGE"
dmg="$1"
expected_version="$2"
contains_control_char "$dmg" && fail "PDF2MD_DMG_E_CONTROL_CHAR"
contains_control_char "$expected_version" && fail "PDF2MD_DMG_E_CONTROL_CHAR"
[[ -f "$dmg" && ! -L "$dmg" ]] || fail "PDF2MD_DMG_E_REQUIRED_PATH"

case "${BASH_SOURCE[0]}" in
  */*) script_parent=${BASH_SOURCE[0]%/*} ;;
  *) script_parent=. ;;
esac
script_dir="$(cd "$script_parent" && pwd -P)" || fail "PDF2MD_DMG_E_INSPECTION"
hdiutil_bin="/usr/bin/hdiutil"
plistbuddy_bin="/usr/libexec/PlistBuddy"
lipo_bin="/usr/bin/lipo"
codesign_bin="/usr/bin/codesign"
check_sidecar_bin="$script_dir/check-release-sidecar.sh"
test_sidecar_bin="$script_dir/test-release-sidecar.sh"

for tool in \
  "$hdiutil_bin" \
  "$plistbuddy_bin" \
  "$lipo_bin" \
  "$codesign_bin" \
  "$check_sidecar_bin" \
  "$test_sidecar_bin"; do
  [[ -f "$tool" && -x "$tool" && ! -L "$tool" ]] || fail "PDF2MD_DMG_E_INSPECTION"
done

mountpoint="$(/usr/bin/mktemp -d /tmp/pdf2md-dmg-mount.XXXXXX)" || {
  fail "PDF2MD_DMG_E_MOUNTPOINT"
}
attach_report="$(/usr/bin/mktemp /tmp/pdf2md-dmg-attach.XXXXXX)" || {
  /bin/rmdir "$mountpoint" >/dev/null 2>&1 || true
  fail "PDF2MD_DMG_E_ATTACH_REPORT"
}
device=""
attached=0

cleanup() {
  local original_status=$?
  local cleanup_status=0
  local detach_target
  trap - EXIT INT TERM HUP
  set +e

  if [[ "$attached" -eq 1 ]]; then
    detach_target="${device:-$mountpoint}"
    "$hdiutil_bin" detach "$detach_target" >/dev/null 2>&1
    if [[ "$?" -ne 0 ]]; then
      "$hdiutil_bin" detach -force "$detach_target" >/dev/null 2>&1
      [[ "$?" -eq 0 ]] || cleanup_status=1
    fi
  fi

  /bin/rm -f -- "$attach_report" >/dev/null 2>&1
  [[ "$?" -eq 0 ]] || cleanup_status=1
  if [[ -d "$mountpoint" ]]; then
    /bin/rmdir "$mountpoint" >/dev/null 2>&1
    [[ "$?" -eq 0 ]] || cleanup_status=1
  elif [[ -e "$mountpoint" ]]; then
    cleanup_status=1
  fi

  if [[ "$cleanup_status" -ne 0 ]]; then
    printf '%s\n' "PDF2MD_DMG_E_CLEANUP" >&2
    if [[ "$original_status" -eq 0 ]]; then
      exit 1
    fi
  fi
  exit "$original_status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

attached=1
"$hdiutil_bin" attach -readonly -nobrowse -mountpoint "$mountpoint" "$dmg" \
  >"$attach_report" 2>/dev/null || fail "PDF2MD_DMG_E_ATTACH"
device="$(
  /usr/bin/awk '$1 ~ /^\/dev\/[A-Za-z0-9._-]+$/ { print $1; exit }' "$attach_report"
)" || fail "PDF2MD_DMG_E_DEVICE"
[[ "$device" =~ ^/dev/[A-Za-z0-9._-]+$ ]] || fail "PDF2MD_DMG_E_DEVICE"

mounted_app="$mountpoint/PDF2MD.app"
mounted_executable="$mounted_app/Contents/MacOS/parsing-core-app"
mounted_python="$mounted_app/Contents/Resources/python-runtime/bin/python3.12"
mounted_plist="$mounted_app/Contents/Info.plist"
set +e
/usr/bin/python3 -I -S -B - "$mountpoint" "$mounted_app" >/dev/null 2>&1 <<'PY'
import os
import stat
import sys

mountpoint, app = sys.argv[1:]
try:
    app_stat = os.lstat(app)
except OSError:
    raise SystemExit(41)
if stat.S_ISLNK(app_stat.st_mode):
    raise SystemExit(40)
if not stat.S_ISDIR(app_stat.st_mode):
    raise SystemExit(41)
root = os.path.realpath(mountpoint)
resolved = os.path.realpath(app)
try:
    inside = os.path.commonpath((root, resolved)) == root
except ValueError:
    inside = False
raise SystemExit(0 if inside else 42)
PY
status=$?
set -e
case "$status" in
  0) ;;
  40) fail "PDF2MD_DMG_E_APP_SYMLINK" ;;
  41) fail "PDF2MD_DMG_E_APP" ;;
  *) fail "PDF2MD_DMG_E_APP_OUTSIDE" ;;
esac

"$check_sidecar_bin" --dmg-volume "$mountpoint" >/dev/null 2>&1 || {
  fail "PDF2MD_DMG_E_VOLUME_CHECK"
}

actual_version="$(
  "$plistbuddy_bin" -c 'Print :CFBundleShortVersionString' "$mounted_plist" 2>/dev/null
)" || fail "PDF2MD_DMG_E_VERSION"
[[ "$actual_version" == "$expected_version" ]] || fail "PDF2MD_DMG_E_VERSION"

app_architectures="$("$lipo_bin" -archs "$mounted_executable" 2>/dev/null)" || {
  fail "PDF2MD_DMG_E_APP_ARCH"
}
[[ "$app_architectures" == "arm64" ]] || fail "PDF2MD_DMG_E_APP_ARCH"
python_architectures="$("$lipo_bin" -archs "$mounted_python" 2>/dev/null)" || {
  fail "PDF2MD_DMG_E_PYTHON_ARCH"
}
[[ "$python_architectures" == "arm64" ]] || fail "PDF2MD_DMG_E_PYTHON_ARCH"

"$check_sidecar_bin" "$mounted_app" >/dev/null 2>&1 || {
  fail "PDF2MD_DMG_E_SIDECAR_CHECK"
}
"$codesign_bin" --verify --deep --strict --verbose=2 "$mounted_app" \
  >/dev/null 2>&1 || fail "PDF2MD_DMG_E_CODESIGN"
"$test_sidecar_bin" "$mounted_app" >/dev/null 2>&1 || {
  fail "PDF2MD_DMG_E_SIDECAR_START"
}
