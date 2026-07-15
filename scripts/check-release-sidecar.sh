#!/usr/bin/env bash
set -euo pipefail

app="${1:?usage: check-release-sidecar.sh /path/to/PDF2MD.app}"
contents="$app/Contents"
launcher="$contents/MacOS/python3"
runtime="$contents/Resources/python-runtime"

require() {
  if [[ ! -e "$1" ]]; then
    echo "required release path is missing: $1" >&2
    exit 1
  fi
}

require "$app"
require "$launcher"
require "$runtime/bin/python3"
test -x "$launcher" || { echo "launcher is not executable: $launcher" >&2; exit 1; }
test -x "$runtime/bin/python3" || { echo "runtime is not executable" >&2; exit 1; }

# Match concrete development-machine paths, not source-code regexes such as
# /Users/[^\\s]+.  The user-name/path components are deliberately restricted
# to filesystem-safe characters so this remains a release-artifact check.
forbidden='(^|[^A-Za-z0-9_])/(Users/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*|opt/homebrew(/[A-Za-z0-9._-]+)*|usr/bin/python[0-9.]*|Library/Frameworks(/[A-Za-z0-9._-]+)*)'
while IFS= read -r -d '' file; do
  if ! file "$file" | grep -q 'Mach-O'; then
    if rg -a -n "$forbidden" "$file"; then
      echo "release bundle contains a development-machine or external Python path: $file" >&2
      exit 1
    fi
    if rg -a -n '/Library/Frameworks' "$file" | rg -v '/System/Library/Frameworks'; then
      echo "release bundle contains a non-system framework path: $file" >&2
      exit 1
    fi
  fi
done < <(find "$contents" -type f -print0)

if find "$contents/Resources" -type f \( -name '*.pyc' -o -name '*.pyo' \) -print -quit | grep -q .; then
  echo "release bundle contains Python bytecode" >&2
  exit 1
fi

while IFS= read -r -d '' file; do
  first_line="$(LC_ALL=C head -n 1 "$file" 2>/dev/null || true)"
  if [[ "$first_line" == '#!'/* ]] && [[ "$first_line" != '#!/bin/bash' ]] && [[ "$first_line" != '#!/bin/sh' ]]; then
    echo "release bundle contains an absolute script shebang: $file: $first_line" >&2
    exit 1
  fi
done < <(find "$runtime" -type f -perm -111 -print0)

while IFS= read -r -d '' file; do
  if file "$file" | grep -q 'Mach-O'; then
    if otool -L "$file" | rg -q '^\s+(/opt/homebrew|/usr/local|/Library/Frameworks|/Users/)'; then
      echo "Mach-O dependency escapes the release bundle: $file" >&2
      otool -L "$file" >&2
      exit 1
    fi
  fi
done < <(find "$runtime" -type f -print0)

codesign --verify --deep --strict --verbose=2 "$app"
