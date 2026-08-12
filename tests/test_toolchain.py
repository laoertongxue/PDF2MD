import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
SIDECAR_PREPARE = ROOT / "parsing-core-app/scripts/prepare-sidecar-python.sh"


def _load_toml(filename: str):
    with (ROOT / filename).open("rb") as handle:
        return tomllib.load(handle)


def test_python_version_file_pins_python_3_12():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").splitlines() == ["3.12"]


def test_project_requires_only_python_3_12():
    assert _load_toml("pyproject.toml")["project"]["requires-python"] == ">=3.12,<3.13"


def test_ruff_targets_python_3_12():
    assert _load_toml("pyproject.toml")["tool"]["ruff"]["target-version"] == "py312"


def test_mypy_targets_python_3_12():
    assert _load_toml("pyproject.toml")["tool"]["mypy"]["python_version"] == "3.12"


def test_rust_uses_stable_channel():
    assert _load_toml("rust-toolchain.toml")["toolchain"]["channel"] == "stable"


def test_rust_installs_only_required_components():
    assert _load_toml("rust-toolchain.toml")["toolchain"]["components"] == [
        "clippy",
        "rustfmt",
    ]


def test_rust_uses_minimal_profile():
    assert _load_toml("rust-toolchain.toml")["toolchain"]["profile"] == "minimal"


def test_release_uses_python_3_12():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert 'python-version: "3.12"' in workflow
    assert 'python-runtime/bin/python3.12"' in workflow
    assert "python3.13" not in workflow


def test_sidecar_uses_python_3_12_runtime():
    script = SIDECAR_PREPARE.read_text(encoding="utf-8")

    assert 'readonly PYTHON_VERSION="3.12.13"' in script
    assert "lib/python3.12" in script
    assert "bin/python3.12" in script
    assert "config-3.12-darwin" in script
    assert "python3.13" not in script
    assert "3.13.13" not in script
    assert "5a30271f8d345a5b02b0c9e4e31e0f1e1455a8e4a04fba95cd9762472abc3b17" in script
