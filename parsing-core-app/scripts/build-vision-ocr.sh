#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_file="$app_dir/src-tauri/vision-ocr/main.swift"
binary_dir="$app_dir/src-tauri/binaries"
target_triple="${TAURI_ENV_TARGET_TRIPLE:-$(rustc -vV | sed -n 's/^host: //p')}"
architecture="${target_triple%%-*}"

case "$target_triple" in
  aarch64-apple-darwin)
    file_architecture="arm64"
    swift_target="arm64-apple-macosx13.0"
    ;;
  x86_64-apple-darwin)
    file_architecture="x86_64"
    swift_target="x86_64-apple-macosx13.0"
    ;;
  *)
    echo "vision OCR helper only supports macOS target triples, got: $target_triple" >&2
    exit 64
    ;;
esac

mkdir -p "$binary_dir"
output="$binary_dir/vision-ocr-$target_triple"
temporary="$output.tmp.$$"
trap 'rm -f "$temporary"' EXIT

xcrun swiftc \
  -O \
  -module-name PDF2MDVisionOCR \
  -target "$swift_target" \
  -framework Vision \
  -framework PDFKit \
  -framework AppKit \
  "$source_file" \
  -o "$temporary"

chmod 755 "$temporary"
if ! file "$temporary" | grep -q "$file_architecture"; then
  echo "vision OCR helper architecture mismatch: expected $architecture" >&2
  file "$temporary" >&2
  exit 65
fi

if [[ -n "${APPLE_SIGNING_IDENTITY:-}" && "${APPLE_SIGNING_IDENTITY}" != "-" ]]; then
  codesign --force --options runtime --sign "$APPLE_SIGNING_IDENTITY" "$temporary"
fi

mv -f "$temporary" "$output"
trap - EXIT
echo "$output"
