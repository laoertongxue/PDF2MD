import json
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
SIDECAR_PREPARE = ROOT / "parsing-core-app/scripts/prepare-sidecar-python.sh"
SIDECAR_RUNTIME = ROOT / "parsing-core-app/scripts/sidecar_runtime.py"
TAURI_CONFIG = ROOT / "parsing-core-app/src-tauri/tauri.conf.json"


def _load_toml(filename: str):
    with (ROOT / filename).open("rb") as handle:
        return tomllib.load(handle)


def test_python_version_file_pins_exact_python_release():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").splitlines() == ["3.12.13"]


def test_node_version_file_pins_exact_lts_release():
    assert (ROOT / ".node-version").read_text(encoding="utf-8").splitlines() == ["24.19.0"]


def test_project_requires_only_python_3_12():
    assert _load_toml("pyproject.toml")["project"]["requires-python"] == ">=3.12,<3.13"


def test_ruff_targets_python_3_12():
    assert _load_toml("pyproject.toml")["tool"]["ruff"]["target-version"] == "py312"


def test_mypy_targets_python_3_12():
    assert _load_toml("pyproject.toml")["tool"]["mypy"]["python_version"] == "3.12"


def test_rust_uses_exact_tested_toolchain():
    assert _load_toml("rust-toolchain.toml")["toolchain"]["channel"] == "1.97.1"


def test_rust_installs_only_required_components():
    assert _load_toml("rust-toolchain.toml")["toolchain"]["components"] == [
        "clippy",
        "rustfmt",
    ]


def test_rust_uses_minimal_profile():
    assert _load_toml("rust-toolchain.toml")["toolchain"]["profile"] == "minimal"


def test_release_uses_exact_python_3_12_patch():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert 'python-version: "3.12.13"' in workflow
    assert 'python-runtime/bin/python3.12"' in workflow
    assert "python3.13" not in workflow


def test_ci_uses_current_native_apple_silicon_runner():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: macos-15\n" in workflow
    assert "macos-14" not in workflow


def test_sidecar_prepare_delegates_pinned_python_and_uv_to_the_runtime_helper():
    script = SIDECAR_PREPARE.read_text(encoding="utf-8")

    assert 'readonly PYTHON_VERSION="3.12.13"' in script
    assert 'readonly UV_VERSION="0.12.3"' in script
    assert 'exec "$SYSTEM_PYTHON" -I -B "$helper" prepare' in script
    assert '--python-version "$PYTHON_VERSION"' in script
    assert '--uv-version "$UV_VERSION"' in script
    assert 'helper="$script_dir/sidecar_runtime.py"' in script
    assert "python3.13" not in script
    assert "3.13.13" not in script
    assert "5a30271f8d345a5b02b0c9e4e31e0f1e1455a8e4a04fba95cd9762472abc3b17" in script


def test_sidecar_runtime_is_the_python_3_12_launcher_source_of_truth():
    source = SIDECAR_RUNTIME.read_text(encoding="utf-8")
    emitted = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", str(SIDECAR_RUNTIME), "emit-launcher"],
        capture_output=True,
        check=True,
    ).stdout

    assert 'PINNED_PYTHON_VERSION = "3.12.13"' in source
    assert b"runtime/lib/python3.12" in emitted
    assert b'PYTHONPATH="$resources/src:$runtime/lib/python3.12/site-packages"' in emitted
    assert b"parsing_core.serving.lifecycle" in emitted
    assert "config-3.12-darwin" in source
    assert "python3.13" not in source


def test_tauri_bundles_only_the_sanitized_project_source_copy():
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
    resources = config["bundle"]["resources"]

    assert "../../src" not in resources
    assert (
        resources["sidecar-runtime/python/lib/python3.12/site-packages/parsing_core"]
        == "src/parsing_core"
    )


def test_sidecar_runtime_transaction_journals_are_ignored_by_git():
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            "--quiet",
            "parsing-core-app/src-tauri/.sidecar-runtime-transactions/example.json",
        ],
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0
