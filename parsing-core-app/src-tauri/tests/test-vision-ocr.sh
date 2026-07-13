#!/usr/bin/env bash
set -euo pipefail

tauri_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app_dir="$(cd "$tauri_dir/.." && pwd)"
fixture_dir="$tauri_dir/tests/vision-ocr-fixtures"
fixture="$fixture_dir/bilingual.pdf"
target_triple="${TAURI_ENV_TARGET_TRIPLE:-$(rustc -vV | sed -n 's/^host: //p')}"
helper="$tauri_dir/binaries/vision-ocr-$target_triple"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/pdf2md-vision-test.XXXXXX")"
attack_pid=""
cleanup() {
  if [[ -n "$attack_pid" ]] && kill -0 "$attack_pid" 2>/dev/null; then
    kill "$attack_pid" 2>/dev/null || true
    wait "$attack_pid" 2>/dev/null || true
  fi
  rm -rf "$work_dir"
}
trap cleanup EXIT

case "$target_triple" in
  aarch64-apple-darwin)
    expected_file_architecture="arm64"
    swift_target="arm64-apple-macosx13.0"
    ;;
  x86_64-apple-darwin)
    expected_file_architecture="x86_64"
    swift_target="x86_64-apple-macosx13.0"
    ;;
  *)
    echo "unsupported vision OCR test target: $target_triple" >&2
    exit 64
    ;;
esac

if [[ ! -f "$fixture" ]]; then
  xcrun swift "$fixture_dir/make-fixture.swift" "$fixture"
fi
xcrun swift "$fixture_dir/make-adversarial-fixtures.swift" "$work_dir/fixtures"

TAURI_ENV_TARGET_TRIPLE="$target_triple" bash "$app_dir/scripts/build-vision-ocr.sh"
test -x "$helper"
file "$helper" | grep -q "$expected_file_architecture"
if strings "$helper" | rg -q 'PDF2MD_VISION_TEST_SWAP_(READY|CONTINUE)'; then
  echo "production helper contains TOCTOU test hook" >&2
  exit 1
fi

output_root="$work_dir/output-root"
outside_dir="$work_dir/outside"
absolute_target="$work_dir/arbitrary-absolute-target"
victim="$work_dir/victim.txt"
request_file="$work_dir/requests.jsonl"
response_file="$work_dir/responses.jsonl"
stderr_file="$work_dir/stderr.log"
mkdir -p "$output_root" "$outside_dir" "$absolute_target"
ln -s "$outside_dir" "$output_root/symlink-output"
printf 'existing-directory-entry\n' > "$output_root/existing-file"
printf 'DO-NOT-OVERWRITE\n' > "$victim"

python3 - \
  "$fixture" \
  "$work_dir/fixtures" \
  "$absolute_target" \
  "$victim" \
  > "$request_file" <<'PY'
import json
import pathlib
import sys

fixture, fixture_dir, absolute_target, victim = sys.argv[1:]
fixtures = pathlib.Path(fixture_dir)
requests = [
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["zh-Hans", "en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["zh-Hans", "en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 600, "languages": ["en-US"], "output_dir": "task-output", "output_json_path": victim},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["en-US"], "output_dir": absolute_target},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["en-US"], "output_dir": "../escape"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["en-US"], "output_dir": "symlink-output"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["en-US"], "output_dir": "existing-file"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 144, "languages": ["en-US"]},
    {"command": "render_and_recognize", "pdf_path": str(fixtures / "huge-media-box.pdf"), "page": 1, "dpi": 600, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 1, "dpi": 601, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": str(fixtures / "encrypted.pdf"), "page": 1, "dpi": 72, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": str(fixtures / "locked.pdf"), "page": 1, "dpi": 72, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": str(fixtures / "crop-rotate.pdf"), "page": 1, "dpi": 72, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": str(fixtures / "crop-rotate.pdf"), "page": 2, "dpi": 72, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": str(fixtures / "crop-rotate.pdf"), "page": 3, "dpi": 72, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "bad\u2028command\u2029payload", "pdf_path": fixture, "page": 1, "dpi": 144, "output_dir": "task-output"},
    {"command": "render_and_recognize", "pdf_path": fixture, "page": 2, "dpi": 144, "languages": ["en-US"], "output_dir": "task-output"},
    {"command": "not_a_command", "pdf_path": fixture, "page": 1, "dpi": 144, "output_dir": "task-output"},
]
for request in requests:
    print(json.dumps(request, ensure_ascii=False))
PY

(
  cd "$output_root"
  PDF2MD_VISION_OUTPUT_ROOT="$output_root" "$helper" < "$request_file" > "$response_file" 2> "$stderr_file"
)

python3 - \
  "$response_file" \
  "$stderr_file" \
  "$output_root" \
  "$outside_dir" \
  "$absolute_target" \
  "$victim" \
  "$work_dir" \
  <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys

response_path, stderr_path, output_root, outside_dir, absolute_target, victim, work_dir = map(pathlib.Path, sys.argv[1:])
output_root = output_root.resolve()
raw = response_path.read_bytes()
assert raw.endswith(b"\n")
frames = raw.split(b"\n")
assert frames[-1] == b""
assert len(frames) - 1 == 18, frames
responses = [json.loads(frame) for frame in frames[:-1]]


def assert_success(result, width, height, expected_text):
    assert set(result) == {"page", "image_path", "image_sha256", "width", "height", "supported_languages", "observations"}
    assert result["width"] == width, result
    assert result["height"] == height, result
    image = pathlib.Path(result["image_path"])
    assert image.is_file(), result
    assert image.parent == output_root / "task-output", result
    assert len(result["image_sha256"]) == 64
    assert hashlib.sha256(image.read_bytes()).hexdigest() == result["image_sha256"]
    properties = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"pixelWidth: {width}" in properties
    assert f"pixelHeight: {height}" in properties
    assert result["observations"], result
    assert any(expected_text in observation["text"] for observation in result["observations"]), result
    for observation in result["observations"]:
        assert set(observation) == {"text", "confidence", "bounding_box", "candidates"}
        assert 0 <= observation["confidence"] <= 1
        assert observation["candidates"]
        box = observation["bounding_box"]
        assert set(box) == {"x", "y", "width", "height"}
        assert all(0 <= box[key] <= 1 for key in box)
        assert box["x"] + box["width"] <= 1.000001
        assert box["y"] + box["height"] <= 1.000001
    return image


images = [
    assert_success(responses[0], 576, 288, "Vision"),
    assert_success(responses[1], 576, 288, "Vision"),
    assert_success(responses[2], 2400, 1200, "Vision"),
    assert_success(responses[12], 60, 100, "UP 90"),
    assert_success(responses[13], 100, 60, "UP 180"),
    assert_success(responses[14], 60, 100, "UP 270"),
]
assert len(images) == len(set(images))
assert (output_root / "task-output").is_dir()
assert len(list((output_root / "task-output").glob("*.png"))) == len(images)

for index in (3, 4, 5, 6):
    assert responses[index]["error"]["code"] == "invalid_output_dir", responses[index]
assert responses[7]["error"]["code"] == "missing_output_dir"
assert responses[8]["error"]["code"] == "resource_limit"
assert responses[9]["error"]["code"] == "invalid_dpi"
assert responses[10]["error"]["code"] == "pdf_encrypted"
assert responses[11]["error"]["code"] == "pdf_locked"
assert responses[15] == {"error": {"code": "unsupported_command", "message": "unsupported command"}}
assert responses[16]["error"]["code"] == "page_out_of_range"
assert responses[17] == {"error": {"code": "unsupported_command", "message": "unsupported command"}}

assert victim.read_text(encoding="utf-8") == "DO-NOT-OVERWRITE\n"
assert not any(outside_dir.iterdir())
assert not any(absolute_target.iterdir())
assert not (work_dir / "escape").exists()
assert (output_root / "existing-file").read_text(encoding="utf-8") == "existing-directory-entry\n"
assert b"bad\xe2\x80\xa8command\xe2\x80\xa9payload" not in raw
stderr = stderr_path.read_bytes()
assert b"bad\xe2\x80\xa8command\xe2\x80\xa9payload" not in stderr
assert str(absolute_target).encode() not in raw + stderr
PY

if rg -q '中文测试|Vision OCR Test' "$stderr_file" || grep -Fq "$fixture" "$stderr_file"; then
  echo "stderr leaked PDF path or text" >&2
  exit 1
fi

attack_helper="$work_dir/vision-ocr-toctou-test"
xcrun swiftc \
  -O \
  -D VISION_OCR_TESTING \
  -module-name PDF2MDVisionOCRTOCTOUTest \
  -target "$swift_target" \
  -framework Vision \
  -framework PDFKit \
  -framework AppKit \
  "$tauri_dir/vision-ocr/main.swift" \
  -o "$attack_helper"
file "$attack_helper" | grep -q "$expected_file_architecture"

swap_root="$work_dir/swap-root"
swap_outside="$work_dir/swap-outside"
swap_ready="$work_dir/swap-ready"
swap_continue="$work_dir/swap-continue"
swap_requests="$work_dir/swap-requests.jsonl"
swap_responses="$work_dir/swap-responses.jsonl"
swap_stderr="$work_dir/swap-stderr.log"
mkdir -p "$swap_root/task-output" "$swap_outside"
python3 - "$fixture" > "$swap_requests" <<'PY'
import json
import sys

for output_dir in ("task-output", "recovery-output"):
    print(json.dumps({
        "command": "render_and_recognize",
        "pdf_path": sys.argv[1],
        "page": 1,
        "dpi": 144,
        "languages": ["en-US"],
        "output_dir": output_dir,
    }))
PY

PDF2MD_VISION_OUTPUT_ROOT="$swap_root" \
PDF2MD_VISION_TEST_SWAP_READY="$swap_ready" \
PDF2MD_VISION_TEST_SWAP_CONTINUE="$swap_continue" \
  "$attack_helper" < "$swap_requests" > "$swap_responses" 2> "$swap_stderr" &
attack_pid=$!
for _ in {1..1000}; do
  [[ -e "$swap_ready" ]] && break
  kill -0 "$attack_pid" 2>/dev/null || break
  sleep 0.01
done
test -e "$swap_ready"
mv "$swap_root/task-output" "$swap_root/moved-task-output"
ln -s "$swap_outside" "$swap_root/task-output"
: > "$swap_continue"
wait "$attack_pid"
attack_pid=""

python3 - "$swap_responses" "$swap_root" "$swap_outside" <<'PY'
import hashlib
import json
import pathlib
import sys

response_path, root, outside = map(pathlib.Path, sys.argv[1:])
responses = [json.loads(line) for line in response_path.read_bytes().splitlines()]
assert len(responses) == 2, responses
assert responses[0] == {"error": {"code": "output_failed", "message": "published image path changed during output"}}, responses[0]
recovery = responses[1]
image = pathlib.Path(recovery["image_path"])
assert image.parent == root.resolve() / "recovery-output"
assert image.is_file()
assert hashlib.sha256(image.read_bytes()).hexdigest() == recovery["image_sha256"]
assert recovery["observations"]
assert not list((root / "moved-task-output").glob("*.png"))
assert not any(outside.iterdir())
PY

if rg -q '中文测试|Vision OCR Test' "$swap_stderr" || grep -Fq "$fixture" "$swap_stderr"; then
  echo "TOCTOU test stderr leaked PDF path or text" >&2
  exit 1
fi

identity="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -n 1 || true)"
if [[ -n "$identity" && "${APPLE_SIGNING_IDENTITY:-}" == "$identity" ]]; then
  codesign --verify --strict --verbose=2 "$helper"
fi

echo "vision OCR protocol tests passed"
