import json
import os
import plistlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import parsing_core

ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = ROOT / "scripts/release_version.py"


def _toml(path: str):
    with (ROOT / path).open("rb") as handle:
        return tomllib.load(handle)


def _json(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_project_versions_are_consistent():
    expected_version = _toml("pyproject.toml")["project"]["version"]
    versions = {
        "python": parsing_core.__version__,
        "pyproject": _toml("pyproject.toml")["project"]["version"],
        "npm": _json("parsing-core-app/package.json")["version"],
        "npm-lock": _json("parsing-core-app/package-lock.json")["version"],
        "cargo": _toml("parsing-core-app/src-tauri/Cargo.toml")["package"]["version"],
        "tauri": _json("parsing-core-app/src-tauri/tauri.conf.json")["version"],
    }
    cargo_lock = _toml("parsing-core-app/src-tauri/Cargo.lock")
    app_package = next(
        package for package in cargo_lock["package"] if package["name"] == "parsing-core-app"
    )
    versions["cargo-lock"] = app_package["version"]
    assert versions == dict.fromkeys(versions, expected_version)


def test_release_version_script_reads_pyproject_version():
    expected_version = _toml("pyproject.toml")["project"]["version"]

    result = subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--github-ref-name", f"v{expected_version}"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.strip() == expected_version


def test_release_version_script_rejects_mismatched_tag():
    result = subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--github-ref-name", "v999.0.0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match project version tag" in result.stderr


def test_bundled_app_version_matches_project():
    expected_version = _toml("pyproject.toml")["project"]["version"]
    configured = os.environ.get("PDF2MD_APP_PATH")
    default = ROOT / "parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app"
    app = Path(configured) if configured else default
    if not app.is_dir():
        pytest.skip("release app is not built")
    with (app / "Contents/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleShortVersionString"] == expected_version
