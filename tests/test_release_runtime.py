import hashlib
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREPARE = REPO / "parsing-core-app/scripts/prepare-sidecar-python.sh"
RUNTIME = REPO / "parsing-core-app/src-tauri/sidecar-runtime/python"
ARCHIVE_NAME = "cpython-3.13.13+20260510-aarch64-apple-darwin-install_only.tar.gz"
ARCHIVE_SHA256 = "1ad1ed518447005d4b6dfa16d4f847d45790e17e94e30164a0a6e6c79a99730f"


def _prepare(**env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(PREPARE)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepare_rejects_non_arm64():
    result = _prepare(PDF2MD_MACHINE="x86_64")
    assert result.returncode == 64
    assert "requires arm64" in result.stderr


def test_prepare_repairs_missing_python_and_tampered_stdlib():
    initial = _prepare()
    assert initial.returncode == 0, initial.stderr
    python = RUNTIME / "bin/python3"
    stdlib = RUNTIME / "lib/python3.13/os.py"
    expected_stdlib = _digest(stdlib)

    python.unlink()
    repaired_python = _prepare()
    assert repaired_python.returncode == 0, repaired_python.stderr
    assert python.exists()

    stdlib.write_text("# corrupted\n", encoding="utf-8")
    repaired_stdlib = _prepare()
    assert repaired_stdlib.returncode == 0, repaired_stdlib.stderr
    assert _digest(stdlib) == expected_stdlib


def test_prepare_replaces_corrupt_cached_archive(tmp_path):
    prepared = _prepare()
    assert prepared.returncode == 0, prepared.stderr
    source = Path.home() / "Library/Caches/PDF2MD-build" / ARCHIVE_NAME
    assert _digest(source) == ARCHIVE_SHA256
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ARCHIVE_NAME).write_bytes(b"corrupt")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ $1 == --output ]]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "cp \"$PDF2MD_TEST_ARCHIVE_SOURCE\" \"$output\"\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    result = _prepare(
        PDF2MD_BUILD_CACHE=str(cache),
        PDF2MD_TEST_ARCHIVE_SOURCE=str(source),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
    )
    assert result.returncode == 0, result.stderr
    assert _digest(cache / ARCHIVE_NAME) == ARCHIVE_SHA256
