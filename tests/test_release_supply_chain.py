import gzip
import os
import platform
import plistlib
import runpy
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECURITY_SCRIPT = ROOT / "scripts/verify-security.sh"
BUNDLE_SCRIPT = ROOT / "scripts/check-release-sidecar.sh"
DMG_SCRIPT = ROOT / "scripts/verify-release-dmg.sh"
REAL_MACOS_RELEASE_TEST = pytest.mark.skipif(
    sys.platform != "darwin" or platform.machine() != "arm64",
    reason="requires native Apple Silicon macOS release tools",
)


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _release_gate_core() -> dict:
    return runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))


@pytest.mark.parametrize(
    "content",
    [
        b"BAIDU_SECRET_KEY=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789AB\n",
        b"API_TOKEN=0123456789abcdef0123456789abcdef0123456789abcdef\n",
        b"-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
        b"-----END PRIVATE KEY-----\n",
        b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
        b"-----END ENCRYPTED PRIVATE KEY-----\n",
        b"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        b"eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IlBERjJNRCJ9."
        b"QmFzZTY0VXJsU2lnbmF0dXJlMTIzNDU2Nzg5MA\n",
    ],
)
def test_bundle_pattern_gate_rejects_extended_credential_shapes_without_echoing_value(
    content: bytes,
):
    core = _release_gate_core()

    with pytest.raises(core["GateError"]) as captured:
        core["_inspect_patterns"](content)

    assert captured.value.code == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert content.decode("ascii").strip() not in str(captured.value)


@pytest.mark.parametrize(
    "content",
    [
        b"sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
        b"dependency-checksum: ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789\n",
        b"API_TOKEN_NAME=PDF2MD_DEEPSEEK_TOKEN\n",
        b"tokenizer vocabulary uses ordinary dependency text\n",
    ],
)
def test_bundle_pattern_gate_keeps_checksums_and_dependency_text(content: bytes):
    core = _release_gate_core()

    core["_inspect_patterns"](content)


@pytest.mark.parametrize(
    ("digest_assignment", "secret_assignment"),
    [
        (
            b"API_TOKEN_SHA256=" + (b"a1" * 32) + b"\n",
            b"API_TOKEN=" + (b"a1" * 32) + b"\n",
        ),
        (
            b"dependency_token_digest=" + (b"b2" * 32) + b"\n",
            b"dependency_token=" + (b"b2" * 32) + b"\n",
        ),
        (
            b"SECRET_CHECKSUM=" + (b"C3" * 32) + b"\n",
            b"SECRET_KEY=" + (b"C3" * 32) + b"\n",
        ),
    ],
)
def test_bundle_pattern_gate_allows_exact_digest_fields_but_rejects_secret_fields(
    digest_assignment: bytes,
    secret_assignment: bytes,
):
    core = _release_gate_core()

    with pytest.raises(core["GateError"]) as captured:
        core["_inspect_patterns"](secret_assignment)

    assert captured.value.code == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret_assignment.decode("ascii").strip() not in str(captured.value)
    core["_inspect_patterns"](digest_assignment)


@pytest.mark.parametrize(
    ("name", "head"),
    [
        ("payload.zst", b"ordinary"),
        ("payload.bin", b"\x28\xb5\x2f\xfd"),
        ("payload.bin", b"\x22\xb5\x2f\xfd"),
        ("payload.bin", b"\x50\x2a\x4d\x18"),
        ("payload.bin", b"\x5f\x2a\x4d\x18"),
        ("payload.lz4", b"ordinary"),
        ("payload.bin", b"\x04\x22\x4d\x18"),
        ("payload.bin", b"\x02\x21\x4c\x18"),
        ("payload.cpio", b"ordinary"),
        ("payload.bin", b"070701"),
        ("payload.7z", b"ordinary"),
        ("payload.bin", b"7z\xbc\xaf'\x1c"),
        ("payload.rar", b"ordinary"),
        ("payload.bin", b"Rar!\x1a\x07\x01\x00"),
        ("payload.cab", b"ordinary"),
        ("payload.bin", b"MSCF\x00\x00\x00\x00"),
        ("payload.Z", b"ordinary"),
        ("payload.bin", b"\x1f\x9d\x90\x00"),
        ("payload.lz", b"ordinary"),
        ("payload.bin", b"LZIP\x01\x0c"),
        ("payload.rpm", b"ordinary"),
        ("payload.bin", b"\xed\xab\xee\xdb\x03\x00"),
        ("payload.squashfs", b"ordinary"),
        ("payload.bin", b"hsqs\x00\x00\x00\x00"),
        ("payload.bin", b"sqsh\x00\x00\x00\x00"),
    ],
)
def test_bundle_archive_gate_fails_closed_for_known_unsupported_containers(name: str, head: bytes):
    core = _release_gate_core()

    with pytest.raises(core["GateError"]) as captured:
        core["_archive_kind"](name, head)

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED"


@pytest.mark.parametrize(
    "name",
    [
        "payload.dmg",
        "payload.udif",
        "payload.sparseimage",
        "payload.sparsebundle",
        "payload.hdi",
        "payload.iso",
        "payload.iso9660",
        "payload.udf",
        "payload.cdr",
        "payload.nrg",
        "payload.toast",
        "payload.img",
        "payload.ima",
        "payload.dsk",
        "payload.vhd",
        "payload.vhdx",
        "payload.avhd",
        "payload.avhdx",
        "payload.qcow",
        "payload.qcow2",
        "payload.qed",
        "payload.vmdk",
        "payload.vdi",
        "payload.hdd",
        "payload.xar",
        "payload.pkg",
        "payload.mpkg",
        "payload.wim",
        "payload.swm",
        "payload.esd",
        "payload.ova",
    ],
)
def test_bundle_archive_gate_rejects_disk_image_and_installer_suffixes(name: str):
    core = _release_gate_core()

    with pytest.raises(core["GateError"]) as captured:
        core["_archive_kind"](name, b"ordinary binary data")

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED"


@pytest.mark.parametrize(
    "content",
    [
        b"\x00" * 512 + b"koly" + b"\x00" * 508,
        b"\x00" * (16 * 2048 + 1) + b"CD001" + b"\x00" * 8,
        b"\x00" * (16 * 2048 + 1) + b"NSR02" + b"\x00" * 8,
        b"\x00" * 512 + b"NER5" + b"\x00" * 8,
        b"conectix" + b"\x00" * 504,
        b"\x00" * 512 + b"conectix" + b"\x00" * 504,
        b"vhdxfile" + b"\x00" * 64,
        b"QFI\xfb" + b"\x00" * 64,
        b"QED\x00" + b"\x00" * 64,
        b"xar!" + b"\x00" * 64,
        b"KDMV" + b"\x00" * 64,
        b"\x00" * 64 + b"\x7f\x10\xda\xbe" + b"\x00" * 64,
        b"MSWIM\x00\x00\x00" + b"\x00" * 64,
        b"sprs" + b"\x00" * 64,
    ],
)
def test_bundle_archive_gate_rejects_renamed_disk_image_magic_and_trailers(content: bytes):
    core = _release_gate_core()

    with pytest.raises(core["GateError"]) as captured:
        core["_archive_kind"]("renamed-payload.bin", content)

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("background.png", b"\x89PNG\r\n\x1a\nclean"),
        (".VolumeIcon.icns", b"icns\x00\x00\x00\x10clean"),
        ("parsing-core-app", b"\xcf\xfa\xed\xfeclean"),
        ("libclean.dylib", b"\xcf\xfa\xed\xfeclean"),
        ("config.json", b'{"clean":true}'),
        ("main.js", b"export const clean = true;"),
        ("module.py", b"CLEAN = True\n"),
        ("font.ttf", b"\x00\x01\x00\x00clean"),
        ("font.otf", b"OTTOclean"),
        ("Info.plist", b"bplist00clean"),
        (".DS_Store", b"safe finder metadata"),
        ("opaque.bin", b"unknown arbitrary bytes"),
    ],
)
def test_bundle_archive_gate_keeps_expected_tauri_and_ordinary_files(name: str, content: bytes):
    core = _release_gate_core()

    assert core["_archive_kind"](name, content) is None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (
            "renamed-udif.bin",
            b"\x00" * (16 * 2048 + 1024) + b"koly" + b"\x00" * 508,
        ),
        (
            "renamed-iso.bin",
            b"\x00" * (16 * 2048 + 1) + b"CD001" + b"\x00" * 4096,
        ),
    ],
)
def test_bundle_scanner_rejects_renamed_far_offset_and_footer_images(
    tmp_path: Path, name: str, content: bytes
):
    core = _release_gate_core()
    app = tmp_path / "PDF2MD.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / name).write_bytes(content)

    with pytest.raises(core["GateError"]) as captured:
        core["TreeScanner"](str(app), inspect_content=True).scan()

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED"


def test_bundle_scanner_rejects_sparsebundle_directory(tmp_path: Path):
    core = _release_gate_core()
    app = tmp_path / "PDF2MD.app"
    sparsebundle = app / "Contents/Resources/payload.sparsebundle"
    sparsebundle.mkdir(parents=True)

    with pytest.raises(core["GateError"]) as captured:
        core["TreeScanner"](str(app), inspect_content=True).scan()

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED"


@REAL_MACOS_RELEASE_TEST
def test_bundle_archive_budget_is_shared_across_top_level_archives(tmp_path: Path):
    core = _release_gate_core()
    app = tmp_path / "PDF2MD.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    for name in ("first.gz", "second.gz"):
        with gzip.open(resources / name, "wb") as stream:
            stream.write(b"A" * 64)
    core["TreeScanner"]._inspect_regular_policy.__globals__["MAX_ARCHIVE_TOTAL_BYTES"] = 100

    with pytest.raises(core["GateError"]) as captured:
        core["TreeScanner"](str(app), inspect_content=True).scan()

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_LIMIT"


@REAL_MACOS_RELEASE_TEST
def test_bundle_archive_member_limit_remains_per_top_level_container(tmp_path: Path):
    core = _release_gate_core()
    app = tmp_path / "PDF2MD.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    for name in ("first.gz", "second.gz"):
        with gzip.open(resources / name, "wb") as stream:
            stream.write(b"clean")
    globals_ = core["TreeScanner"]._inspect_regular_policy.__globals__
    globals_["MAX_ARCHIVE_MEMBERS"] = 1
    globals_["MAX_ARCHIVE_TOTAL_BYTES"] = 1024

    core["TreeScanner"](str(app), inspect_content=True).scan()


def _tauri_volume(tmp_path: Path) -> Path:
    volume = tmp_path / "mounted-volume"
    (volume / "PDF2MD.app").mkdir(parents=True)
    (volume / "Applications").symlink_to("/Applications")
    background = volume / ".background"
    background.mkdir()
    (background / "background.png").write_bytes(b"safe png fixture")
    (volume / ".VolumeIcon.icns").write_bytes(b"safe icon fixture")
    (volume / ".DS_Store").write_bytes(b"safe finder fixture")
    return volume


@REAL_MACOS_RELEASE_TEST
def test_dmg_volume_gate_accepts_only_the_expected_tauri_presentation(tmp_path: Path):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    verify(str(volume))


@REAL_MACOS_RELEASE_TEST
def test_dmg_volume_gate_allows_absent_ds_store(tmp_path: Path):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    (volume / ".DS_Store").unlink()
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    verify(str(volume))


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize("extra", ["Unexpected.txt", "Other.app", "run-me"])
def test_dmg_volume_gate_rejects_extra_files_apps_and_executables(tmp_path: Path, extra: str):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    target = volume / extra
    if extra.endswith(".app"):
        target.mkdir()
    else:
        target.write_text("extra\n", encoding="utf-8")
        if extra == "run-me":
            target.chmod(0o755)
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    with pytest.raises(core["GateError"]) as captured:
        verify(str(volume))

    assert captured.value.code == "PDF2MD_BUNDLE_E_DMG_LAYOUT"


@REAL_MACOS_RELEASE_TEST
def test_dmg_volume_gate_requires_applications_link_to_exact_system_target(tmp_path: Path):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    applications = volume / "Applications"
    applications.unlink()
    applications.symlink_to("/tmp/Applications")
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    with pytest.raises(core["GateError"]) as captured:
        verify(str(volume))

    assert captured.value.code == "PDF2MD_BUNDLE_E_DMG_TARGET"


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize(
    ("relative", "content", "expected"),
    [
        (
            ".background/background.png",
            b"API_TOKEN=0123456789abcdef0123456789abcdef0123456789abcdef",
            "PDF2MD_BUNDLE_E_CREDENTIAL",
        ),
        (
            ".DS_Store",
            b"build=/Users/builder/Documents/PDF2MD/release",
            "PDF2MD_BUNDLE_E_DEVELOPMENT_PATH",
        ),
    ],
)
def test_dmg_volume_gate_scans_allowed_regular_presentation_content(
    tmp_path: Path, relative: str, content: bytes, expected: str
):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    (volume / relative).write_bytes(content)
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    with pytest.raises(core["GateError"]) as captured:
        verify(str(volume))

    assert captured.value.code == expected
    assert content.decode("ascii") not in str(captured.value)


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize(
    "relative",
    [
        ".background/background.png",
        ".VolumeIcon.icns",
        ".DS_Store",
    ],
)
@pytest.mark.parametrize(
    ("signal", "content"),
    [
        ("udif-footer", b"\x00" * 512 + b"koly" + b"\x00" * 508),
        ("iso-offset", b"\x00" * (16 * 2048 + 1) + b"CD001" + b"\x00" * 8),
        ("udf-offset", b"\x00" * (16 * 2048 + 1) + b"NSR02" + b"\x00" * 8),
        ("xar-header", b"xar!" + b"\x00" * 64),
        ("qcow-header", b"QFI\xfb" + b"\x00" * 64),
    ],
    ids=["udif-footer", "iso-offset", "udf-offset", "xar-header", "qcow-header"],
)
def test_dmg_volume_gate_rejects_known_container_signals_in_every_presentation_file(
    tmp_path: Path, relative: str, signal: str, content: bytes
):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    (volume / relative).write_bytes(content)
    verify = core.get("verify_dmg_volume")

    assert callable(verify), signal
    with pytest.raises(core["GateError"]) as captured:
        verify(str(volume))

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_UNSUPPORTED"


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize(
    ("relative", "content"),
    [
        (
            ".background/background.png",
            bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001"
                "08060000001f15c4890000000d4944415408d763f8ffff3f"
                "030008fc02fe0def46b80000000049454e44ae426082"
            ),
        ),
        (".VolumeIcon.icns", b"icns\x00\x00\x00\x08"),
        (".DS_Store", b"\x00\x00\x00\x01Bud1" + b"\x00" * 64),
    ],
)
def test_dmg_volume_gate_accepts_clean_bounded_presentation_formats(
    tmp_path: Path, relative: str, content: bytes
):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    (volume / relative).write_bytes(content)
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    verify(str(volume))


@REAL_MACOS_RELEASE_TEST
def test_dmg_volume_gate_shares_archive_budget_across_presentation_files(tmp_path: Path):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    for relative in (".background/background.png", ".VolumeIcon.icns"):
        (volume / relative).write_bytes(gzip.compress(b"A" * 64))
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    verify.__globals__["MAX_ARCHIVE_TOTAL_BYTES"] = 100
    with pytest.raises(core["GateError"]) as captured:
        verify(str(volume))

    assert captured.value.code == "PDF2MD_BUNDLE_E_ARCHIVE_LIMIT"


@REAL_MACOS_RELEASE_TEST
def test_dmg_volume_gate_scans_presentation_extended_attributes(tmp_path: Path):
    core = _release_gate_core()
    volume = _tauri_volume(tmp_path)
    secret = b"sk-" + (b"V" * 40)
    _run_checked(
        "/usr/bin/xattr",
        "-w",
        "com.pdf2md.fixture",
        secret.decode("ascii"),
        str(volume / ".VolumeIcon.icns"),
    )
    verify = core.get("verify_dmg_volume")

    assert callable(verify)
    with pytest.raises(core["GateError"]) as captured:
        verify(str(volume))

    assert captured.value.code == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret.decode("ascii") not in str(captured.value)


def _add_pinned_fixture(repo: Path) -> Path:
    fixture = repo / "tests/test_workbench/test_ocr_codex.py"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "tests/test_workbench/test_ocr_codex.py", fixture)
    subprocess.run(["git", "-C", str(repo), "add", str(fixture.relative_to(repo))], check=True)
    return fixture


def _secret_scan(
    repo: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = repo / "scripts/verify-security.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SECURITY_SCRIPT, script)
    script.chmod(0o755)
    return _run(str(script), "--secret-scan-only", env=env)


def _init_scan_repo(tmp_path: Path, content: str) -> Path:
    repo = tmp_path / "scan-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "fixture.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True)
    _add_pinned_fixture(repo)
    return repo


def test_secret_scan_fails_without_echoing_the_matching_secret(tmp_path: Path):
    secret = "sk-" + "A" * 40
    repo = _init_scan_repo(tmp_path, f"token={secret}\n")

    result = _secret_scan(repo)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_scan_succeeds_when_tracked_files_have_no_secret(tmp_path: Path):
    repo = _init_scan_repo(tmp_path, "token=fixture-without-credential\n")

    result = _secret_scan(repo)

    assert result.returncode == 0, result.stderr


def test_secret_scan_fails_closed_when_scan_root_is_not_a_repository(tmp_path: Path):
    result = _secret_scan(tmp_path)

    assert result.returncode != 0
    assert "could not inspect" in result.stderr


def test_secret_scan_root_environment_cannot_narrow_repository_scan(tmp_path: Path):
    secret = "sk-" + "R" * 40
    repo = _init_scan_repo(tmp_path, f"outside={secret}\n")
    narrowed = repo / "src"
    narrowed.mkdir()
    (narrowed / "clean.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "src/clean.py"], check=True)
    env = os.environ | {"PDF2MD_SECRET_SCAN_ROOT": str(narrowed)}

    result = _secret_scan(repo, env=env)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_scan_checks_staged_content_even_when_worktree_is_clean(tmp_path: Path):
    secret = "sk-" + "S" * 40
    repo = _init_scan_repo(tmp_path, "clean\n")
    tracked = repo / "fixture.txt"
    tracked.write_text(f"token={secret}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True)
    tracked.write_text("clean worktree\n", encoding="utf-8")

    result = _secret_scan(repo)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_scan_checks_worktree_content_even_when_index_is_clean(tmp_path: Path):
    secret = "sk-" + "W" * 40
    repo = _init_scan_repo(tmp_path, "clean\n")
    (repo / "fixture.txt").write_text(f"token={secret}\n", encoding="utf-8")

    result = _secret_scan(repo)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_scan_rejects_missing_index_fixture_even_with_worktree_symlink(tmp_path: Path):
    repo, fixture = _pinned_fixture_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "rm", "--cached", str(fixture.relative_to(repo))],
        check=True,
        capture_output=True,
    )
    fixture.unlink()
    outside = repo / "outside.py"
    outside.write_text("clean replacement\n", encoding="utf-8")
    fixture.symlink_to(outside)

    result = _secret_scan(repo)

    assert result.returncode == 1
    assert result.stderr.strip() == "Potential credential found in tracked source."


def test_secret_scan_ignores_path_injected_git_and_python(tmp_path: Path):
    secret = "sk-" + "P" * 40
    repo = _init_scan_repo(tmp_path, f"token={secret}\n")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name in ("git", "python3"):
        fake = fake_bin / name
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = _secret_scan(repo, env=env)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_security_full_mode_requires_explicit_absolute_toolchain_paths():
    source = SECURITY_SCRIPT.read_text(encoding="utf-8")

    assert 'npm_bin="${PDF2MD_NPM_BIN:-}"' in source
    assert 'uv_bin="${PDF2MD_UV_BIN:-}"' in source
    assert '"$npm_bin" audit --audit-level=moderate' in source
    assert '"$uv_bin" run --frozen pytest' in source
    assert "command -v npm" not in source
    assert "command -v uv" not in source


def test_repository_secret_scan_accepts_only_the_pinned_redaction_fixture(tmp_path: Path):
    repo = _init_scan_repo(tmp_path, "tracked fixture is clean\n")
    result = _secret_scan(repo)

    assert result.returncode == 0, result.stderr


def test_pinned_fixture_path_does_not_hide_an_added_secret(tmp_path: Path):
    repo = tmp_path / "scan-repo"
    fixture = repo / "tests/test_workbench/test_ocr_codex.py"
    fixture.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shutil.copyfile(ROOT / "tests/test_workbench/test_ocr_codex.py", fixture)
    subprocess.run(["git", "-C", str(repo), "add", str(fixture.relative_to(repo))], check=True)
    assert _secret_scan(repo).returncode == 0

    added_secret = "sk-" + "C" * 40
    with fixture.open("a", encoding="utf-8") as handle:
        handle.write(f"\nADDED_SECRET = {added_secret!r}\n")
    result = _secret_scan(repo)

    assert result.returncode == 1
    assert added_secret not in result.stdout
    assert added_secret not in result.stderr


def _pinned_fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "scan-repo"
    fixture = repo / "tests/test_workbench/test_ocr_codex.py"
    fixture.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shutil.copyfile(ROOT / "tests/test_workbench/test_ocr_codex.py", fixture)
    subprocess.run(["git", "-C", str(repo), "add", str(fixture.relative_to(repo))], check=True)
    return repo, fixture


@pytest.mark.parametrize("attack", ["index-symlink", "hardlink", "ancestor-symlink"])
def test_pinned_fixture_rejects_filesystem_aliases(tmp_path: Path, attack: str):
    repo, fixture = _pinned_fixture_repo(tmp_path)
    if attack == "index-symlink":
        fixture.unlink()
        fixture.symlink_to("../../outside.py")
        subprocess.run(["git", "-C", str(repo), "add", str(fixture.relative_to(repo))], check=True)
    elif attack == "hardlink":
        os.link(fixture, repo / "fixture-hardlink.py")
    else:
        original_parent = fixture.parent
        relocated_parent = repo / "relocated-ocr-tests"
        original_parent.rename(relocated_parent)
        original_parent.symlink_to(relocated_parent, target_is_directory=True)
    result = _secret_scan(repo)

    assert result.returncode == 1
    assert result.stderr.strip() == "Potential credential found in tracked source."


def test_pinned_fixture_is_validated_even_when_replacement_has_no_secret(tmp_path: Path):
    repo, fixture = _pinned_fixture_repo(tmp_path)
    fixture.write_text("no credential pattern remains\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", str(fixture.relative_to(repo))], check=True)
    result = _secret_scan(repo)

    assert result.returncode == 1
    assert result.stderr.strip() == "Potential credential found in tracked source."


def test_secret_scan_does_not_exclude_docs_superpowers_directory(tmp_path: Path):
    secret = "sk-" + "D" * 40
    repo = tmp_path / "scan-repo"
    fixture = repo / "docs/superpowers/attack.txt"
    fixture.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _add_pinned_fixture(repo)
    fixture.write_text(secret, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", str(fixture.relative_to(repo))], check=True)
    result = _secret_scan(repo)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_scan_checks_tracked_binary_files_without_echoing_secret(tmp_path: Path):
    secret = "sk-" + "G" * 40
    repo = tmp_path / "scan-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _add_pinned_fixture(repo)
    (repo / "fixture.bin").write_bytes(b"prefix\0" + secret.encode())
    subprocess.run(["git", "-C", str(repo), "add", "fixture.bin"], check=True)
    result = _secret_scan(repo)

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_bundle_gate_uses_clean_shell_wrapper_and_isolated_python_core():
    wrapper = BUNDLE_SCRIPT.read_text(encoding="utf-8")
    core = (ROOT / "scripts/check_release_sidecar.py").read_text(encoding="utf-8")

    assert wrapper.startswith("#!/usr/bin/env -S -i ")
    assert "HOME=/var/empty" in wrapper
    assert "TMPDIR=/tmp" in wrapper
    assert "exec /usr/bin/python3 -I -S -B " in wrapper
    assert '"$script_dir/check_release_sidecar.py" "$@"' in wrapper
    assert "/usr/bin/otool" in core
    assert "/usr/bin/lipo" in core
    assert "/usr/bin/codesign" in core
    assert "shell=True" not in core
    assert "command -v" not in wrapper


def _make_real_signed_app_scenario(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    app = _build_real_m0_app(tmp_path / "Bundle With Spaces")
    return app, {
        "TEST_MACHO_ARCH": "arm64",
        "TEST_DEPENDENCY": "@rpath/libclean.dylib",
        "TEST_RPATH_MODE": "internal",
    }


def _run_bundle(app: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    runtime = app / "Contents/Resources/python-runtime/bin/python3.12"
    dependency = env.get("TEST_DEPENDENCY", "@rpath/libclean.dylib")
    rpath_mode = env.get("TEST_RPATH_MODE", "internal")
    architecture = env.get("TEST_MACHO_ARCH", "arm64")
    changed_native_code = False

    if dependency != "@rpath/libclean.dylib":
        _run_checked(
            "/usr/bin/install_name_tool",
            "-change",
            "@rpath/libclean.dylib",
            dependency,
            str(runtime),
        )
        changed_native_code = True
    if rpath_mode == "none":
        _run_checked(
            "/usr/bin/install_name_tool",
            "-delete_rpath",
            "@executable_path/../Frameworks",
            str(runtime),
        )
        changed_native_code = True
    elif rpath_mode == "external":
        _run_checked(
            "/usr/bin/install_name_tool",
            "-rpath",
            "@executable_path/../Frameworks",
            "/Library/Untrusted",
            str(runtime),
        )
        changed_native_code = True
    elif rpath_mode == "mixed":
        _run_checked(
            "/usr/bin/install_name_tool",
            "-add_rpath",
            "/Library/Untrusted",
            str(runtime),
        )
        changed_native_code = True
    if architecture == "x86_64":
        source = app.parent / "foreign.c"
        source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        _run_checked(
            "/usr/bin/clang",
            "-arch",
            "x86_64",
            str(source),
            "-o",
            str(app / "Contents/Frameworks/foreign-macho.dylib"),
        )
        changed_native_code = True

    unreadable = any(
        path.is_file() and not path.is_symlink() and not path.stat().st_mode & 0o400
        for path in (app / "Contents").rglob("*")
    )
    if not unreadable:
        try:
            _sign_m0_app(app)
        except subprocess.CalledProcessError:
            if changed_native_code:
                raise
    return _run(str(BUNDLE_SCRIPT), str(app), env=os.environ.copy())


def test_bundle_check_rejects_symlink_that_resolves_outside_app(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    (tmp_path / "outside").write_text("outside\n", encoding="utf-8")
    (app / "Contents/Resources/escape").symlink_to("../../../../outside")

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_SYMLINK_OUTSIDE"


def test_bundle_check_rejects_absolute_symlink_even_when_target_exists(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    (app / "Contents/Resources/absolute").symlink_to(outside)

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_SYMLINK_ABSOLUTE"


def test_bundle_check_rejects_newline_symlink_target_before_realpath(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    outside = app.parent / "PDF2MD.app\n"
    outside.mkdir()
    (app / "Contents/Resources/escape").symlink_to("../../../PDF2MD.app\n")

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_ENTRY_CONTROL_CHAR"


def test_bundle_check_rejects_newline_in_regular_entry_name(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    (app / "Contents/Resources/line\nbreak").write_text("clean", encoding="utf-8")

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_ENTRY_CONTROL_CHAR"


def test_bundle_check_accepts_internal_relative_symlinks_and_clean_files(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    (app / "Contents/Resources/readme.txt").write_text("clean fixture\n", encoding="utf-8")
    (app / "Contents/Resources/good-data.bin").write_bytes(b"fixture")

    result = _run_bundle(app, env)

    assert result.returncode == 0, result.stderr


def test_bundle_check_fails_closed_for_unreadable_regular_file(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    unreadable = app / "Contents/Resources/unreadable.bin"
    unreadable.write_bytes(b"clean")
    unreadable.chmod(0)

    try:
        result = _run_bundle(app, env)
    finally:
        unreadable.chmod(0o600)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_SCAN"


def test_bundle_check_rejects_non_arm64_macho_anywhere_in_bundle(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    (app / "Contents/Frameworks/foreign-macho.dylib").write_bytes(b"fixture")
    env["TEST_MACHO_ARCH"] = "x86_64"

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_ARCH"


def test_bundle_check_does_not_trust_runtime_python_to_validate_its_links(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    runtime_bin = app / "Contents/Resources/python-runtime/bin"
    runtime_python = runtime_bin / "python3.12"
    marker = tmp_path / "malicious-runtime-ran"
    runtime_python.write_text(
        '#!/bin/sh\nprintf ran > "$PDF2MD_MALICIOUS_RUNTIME_MARKER"\nexit 0\n',
        encoding="utf-8",
    )
    runtime_python.chmod(0o755)
    (runtime_bin / "python3").unlink()
    (runtime_bin / "python3").symlink_to("python")
    env["PDF2MD_MALICIOUS_RUNTIME_MARKER"] = str(marker)

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_RUNTIME_LINK"
    assert not marker.exists()


def test_bundle_check_rejects_secret_without_echoing_it(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    secret = "sk-" + "B" * 40
    (app / "Contents/Resources/config.bin").write_bytes(secret.encode())

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_bundle_check_rejects_secret_in_entry_name_without_echoing_it(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    secret = "sk-" + "E" * 40
    (app / "Contents/Resources" / secret).write_text("clean", encoding="utf-8")

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_bundle_check_development_path_error_does_not_echo_untrusted_name(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    untrusted_name = "UNTRUSTED-FILENAME-MARKER"
    candidate = app / "Contents/Resources" / untrusted_name
    candidate.write_text("/Volumes/build/private/runtime", encoding="utf-8")

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_DEVELOPMENT_PATH"
    assert untrusted_name not in result.stdout
    assert untrusted_name not in result.stderr


def test_bundle_check_rejects_any_external_absolute_macho_dependency(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    env["TEST_DEPENDENCY"] = "/Library/Untrusted/libbad.dylib"
    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"


def test_bundle_check_rejects_normalized_absolute_dependency_escape(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    env["TEST_DEPENDENCY"] = "/usr/lib/../local/libbad.dylib"

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"


def test_bundle_check_rejects_external_lc_rpath(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    env["TEST_RPATH_MODE"] = "external"

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_RPATH"


def test_bundle_check_rejects_any_external_rpath_when_an_internal_one_exists(
    tmp_path: Path,
):
    app, env = _make_real_signed_app_scenario(tmp_path)
    env["TEST_RPATH_MODE"] = "mixed"

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_RPATH"


def test_bundle_check_rejects_rpath_dependency_without_lc_rpath(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    env["TEST_RPATH_MODE"] = "none"

    result = _run_bundle(app, env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"


def test_bundle_check_allows_normalized_internal_frameworks_and_system_lib(tmp_path: Path):
    app, env = _make_real_signed_app_scenario(tmp_path)
    result = _run_bundle(app, env)

    assert result.returncode == 0, result.stderr


def test_bundle_check_allows_safe_loader_path_and_rejects_unknown_token(tmp_path: Path):
    safe_app, safe_env = _make_real_signed_app_scenario(tmp_path / "safe")
    runtime_bin = safe_app / "Contents/Resources/python-runtime/bin"
    shutil.copy2(
        safe_app / "Contents/Frameworks/libclean.dylib",
        runtime_bin / "libclean.dylib",
    )
    safe_env["TEST_DEPENDENCY"] = "@loader_path/libclean.dylib"
    safe_result = _run_bundle(safe_app, safe_env)
    assert safe_result.returncode == 0, safe_result.stderr

    app, env = _make_real_signed_app_scenario(tmp_path / "unknown")
    env["TEST_DEPENDENCY"] = "@unknown_path/libclean.dylib"
    result = _run_bundle(app, env)
    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"


def _make_dmg_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    source_app = tmp_path / "source/PDF2MD.app"
    (source_app / "Contents/MacOS").mkdir(parents=True)
    (source_app / "Contents/Resources/python-runtime/bin").mkdir(parents=True)
    (source_app / "Contents/MacOS/parsing-core-app").write_bytes(b"fixture")
    (source_app / "Contents/Resources/python-runtime/bin/python3.12").write_bytes(b"fixture")
    (source_app / "Contents/Info.plist").write_text("fixture", encoding="utf-8")
    dmg = tmp_path / "PDF2MD.dmg"
    dmg.write_bytes(b"fixture")
    log = tmp_path / "calls.log"
    fake_bin = tmp_path / "dmg-bin"
    controls = tmp_path / "controls"
    fake_bin.mkdir()
    controls.mkdir()
    mountpoint_state = tmp_path / "mountpoint.state"
    detach_state = tmp_path / "detach.state"
    quote = shlex.quote

    (fake_bin / "hdiutil").write_text(
        f"""#!/bin/bash
log={quote(str(log))}
source_app={quote(str(source_app))}
controls={quote(str(controls))}
mountpoint_state={quote(str(mountpoint_state))}
detach_state={quote(str(detach_state))}
printf 'hdiutil:%s\n' "$*" >> "$log"
if [[ "$1" == "attach" ]]; then
  shift
  while (( $# )); do
    if [[ "$1" == "-mountpoint" ]]; then mountpoint="$2"; shift 2; else shift; fi
  done
  if [[ -e "$controls/mount-app-symlink" ]]; then
    /bin/ln -s "$source_app" "$mountpoint/PDF2MD.app"
  else
    /bin/cp -R "$source_app" "$mountpoint/PDF2MD.app"
  fi
  /bin/mkdir "$mountpoint/.background"
  printf 'safe background\n' > "$mountpoint/.background/background.png"
  printf 'safe icon\n' > "$mountpoint/.VolumeIcon.icns"
  printf 'safe finder metadata\n' > "$mountpoint/.DS_Store"
  /bin/ln -s /Applications "$mountpoint/Applications"
  if [[ -e "$controls/extra-volume-entry" ]]; then
    printf 'unexpected\n' > "$mountpoint/Unexpected.txt"
  fi
  printf '%s\n' "$mountpoint" > "$mountpoint_state"
  if [[ -e "$controls/attach-without-device" ]]; then
    printf 'Apple_HFS PDF2MD %s\n' "$mountpoint"
  else
    printf '/dev/disk42 Apple_HFS PDF2MD %s\n' "$mountpoint"
  fi
  [[ -e "$controls/attach-fail-after-mount" ]] && exit 1
  exit 0
fi
if [[ "$1" == "detach" && -e "$controls/detach-fail-once" \
  && "$2" != "-force" && ! -e "$detach_state" ]]; then
  : > "$detach_state"
  exit 1
fi
if [[ "$1" == "detach" && -e "$controls/detach-always-fails" ]]; then
  exit 1
fi
if [[ "$1" == "detach" ]]; then
  mountpoint="$(/bin/cat "$mountpoint_state")"
  /bin/rm -rf \
    "$mountpoint/PDF2MD.app" \
    "$mountpoint/.background" \
    "$mountpoint/.VolumeIcon.icns" \
    "$mountpoint/.DS_Store" \
    "$mountpoint/Applications" \
    "$mountpoint/Unexpected.txt"
  [[ -e "$controls/detach-removes-mountpoint" ]] && /bin/rmdir "$mountpoint"
fi
exit 0
""",
        encoding="utf-8",
    )
    (fake_bin / "PlistBuddy").write_text(
        f"#!/bin/sh\nprintf 'plist:%s\\n' \"$*\" >> {quote(str(log))}\nprintf '0.1.2\\n'\n",
        encoding="utf-8",
    )
    (fake_bin / "lipo").write_text(
        f"#!/bin/sh\nprintf 'lipo:%s\\n' \"$*\" >> {quote(str(log))}\nprintf 'arm64\\n'\n",
        encoding="utf-8",
    )
    (fake_bin / "check").write_text(
        f"""#!/bin/sh
printf 'check:%s\\n' "$*" >> {quote(str(log))}
if [ "$1" = "--dmg-volume" ]; then
  [ -d "$2/PDF2MD.app" ] || exit 1
  [ -L "$2/Applications" ] || exit 1
  [ "$(/usr/bin/readlink "$2/Applications")" = "/Applications" ] || exit 1
  [ -f "$2/.background/background.png" ] || exit 1
  [ -f "$2/.VolumeIcon.icns" ] || exit 1
  [ -f "$2/.DS_Store" ] || exit 1
  [ ! -e "$2/Unexpected.txt" ] || exit 1
fi
""",
        encoding="utf-8",
    )
    for name in ("codesign", "test-sidecar"):
        (fake_bin / name).write_text(
            f"#!/bin/sh\nprintf '{name}:%s\\n' \"$*\" >> {quote(str(log))}\n",
            encoding="utf-8",
        )
    for helper in fake_bin.iterdir():
        helper.chmod(0o755)

    test_script = tmp_path / "verify-release-dmg.test.sh"
    test_source = DMG_SCRIPT.read_text(encoding="utf-8")
    replacements = {
        'hdiutil_bin="/usr/bin/hdiutil"': f'hdiutil_bin="{fake_bin / "hdiutil"}"',
        'plistbuddy_bin="/usr/libexec/PlistBuddy"': (f'plistbuddy_bin="{fake_bin / "PlistBuddy"}"'),
        'lipo_bin="/usr/bin/lipo"': f'lipo_bin="{fake_bin / "lipo"}"',
        'codesign_bin="/usr/bin/codesign"': f'codesign_bin="{fake_bin / "codesign"}"',
        'check_sidecar_bin="$script_dir/check-release-sidecar.sh"': (
            f'check_sidecar_bin="{fake_bin / "check"}"'
        ),
        'test_sidecar_bin="$script_dir/test-release-sidecar.sh"': (
            f'test_sidecar_bin="{fake_bin / "test-sidecar"}"'
        ),
    }
    for trusted, test_tool in replacements.items():
        assert trusted in test_source
        test_source = test_source.replace(trusted, test_tool)
    test_script.write_text(test_source, encoding="utf-8")
    test_script.chmod(0o755)

    env = {
        "PDF2MD_TEST_DMG_SCRIPT": str(test_script),
        "PDF2MD_TEST_DMG_CONTROLS": str(controls),
    }
    return dmg, env, log


def _run_dmg_fixture(
    dmg: Path, version: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    controls = Path(env["PDF2MD_TEST_DMG_CONTROLS"])
    scenarios = {
        "FAKE_MOUNT_APP_SYMLINK": "mount-app-symlink",
        "FAKE_ATTACH_WITHOUT_DEVICE": "attach-without-device",
        "FAKE_ATTACH_FAIL_AFTER_MOUNT": "attach-fail-after-mount",
        "FAKE_DETACH_FAIL_ONCE": "detach-fail-once",
        "FAKE_DETACH_ALWAYS_FAILS": "detach-always-fails",
        "FAKE_DETACH_REMOVES_MOUNTPOINT": "detach-removes-mountpoint",
        "FAKE_EXTRA_VOLUME_ENTRY": "extra-volume-entry",
    }
    for key, marker in scenarios.items():
        if env.get(key) == "1":
            (controls / marker).touch()
    return _run(env["PDF2MD_TEST_DMG_SCRIPT"], str(dmg), version)


def test_dmg_verifier_checks_the_whole_volume_and_mounted_app_then_detaches(tmp_path: Path):
    dmg, env, log = _make_dmg_fixture(tmp_path)

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "hdiutil:attach -readonly -nobrowse -mountpoint" in calls
    assert "hdiutil:detach /dev/disk42" in calls
    for command in ("plist:", "lipo:", "check:", "codesign:", "test-sidecar:"):
        assert command in calls
        assert "source/PDF2MD.app" not in next(
            line for line in calls.splitlines() if line.startswith(command)
        )
    check_calls = [line for line in calls.splitlines() if line.startswith("check:")]
    assert len(check_calls) == 2
    volume_call = next(line for line in check_calls if line.startswith("check:--dmg-volume "))
    app_call = next(line for line in check_calls if not line.startswith("check:--dmg-volume "))
    mountpoint = Path(volume_call.removeprefix("check:--dmg-volume "))
    assert Path(app_call.removeprefix("check:")) == mountpoint / "PDF2MD.app"
    assert not mountpoint.exists()


def test_dmg_verifier_force_detaches_and_cleans_mountpoint_after_detach_failure(
    tmp_path: Path,
):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_DETACH_FAIL_ONCE"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "hdiutil:detach /dev/disk42" in calls
    assert "hdiutil:detach -force /dev/disk42" in calls
    volume_call = next(
        line for line in calls.splitlines() if line.startswith("check:--dmg-volume ")
    )
    assert not Path(volume_call.removeprefix("check:--dmg-volume ")).exists()


def test_dmg_verifier_rejects_extra_volume_entry_and_still_detaches(tmp_path: Path):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_EXTRA_VOLUME_ENTRY"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_DMG_E_VOLUME_CHECK"
    calls = log.read_text(encoding="utf-8")
    assert "check:--dmg-volume " in calls
    assert "hdiutil:detach /dev/disk42" in calls


def test_dmg_verifier_preserves_volume_error_and_reports_cleanup_failure(
    tmp_path: Path,
):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    sentinel_contents = b"mounted release sentinel\n"
    source_sentinel = tmp_path / "source/PDF2MD.app/Contents/Resources/release-test-sentinel.txt"
    source_sentinel.write_bytes(sentinel_contents)
    env["FAKE_EXTRA_VOLUME_ENTRY"] = "1"
    env["FAKE_DETACH_ALWAYS_FAILS"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "PDF2MD_DMG_E_VOLUME_CHECK",
        "PDF2MD_DMG_E_CLEANUP",
    ]
    calls = log.read_text(encoding="utf-8").splitlines()
    volume_events = [
        (index, call) for index, call in enumerate(calls) if call.startswith("check:--dmg-volume ")
    ]
    assert len(volume_events) == 1
    volume_index, volume_call = volume_events[0]
    normal_detach_index = calls.index("hdiutil:detach /dev/disk42")
    force_detach_index = calls.index("hdiutil:detach -force /dev/disk42")
    assert volume_index < normal_detach_index < force_detach_index

    mountpoint = Path(volume_call.removeprefix("check:--dmg-volume "))
    mounted_sentinel = mountpoint / "PDF2MD.app/Contents/Resources/release-test-sentinel.txt"
    assert mountpoint.is_dir()
    assert mounted_sentinel.is_file()
    assert mounted_sentinel.read_bytes() == sentinel_contents


def test_dmg_verifier_reports_stable_cleanup_error_when_force_detach_fails(
    tmp_path: Path,
):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_DETACH_ALWAYS_FAILS"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_DMG_E_CLEANUP"
    calls = log.read_text(encoding="utf-8")
    assert "hdiutil:detach /dev/disk42" in calls
    assert "hdiutil:detach -force /dev/disk42" in calls


def test_dmg_verifier_falls_back_to_mountpoint_cleanup_when_device_is_missing(
    tmp_path: Path,
):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_ATTACH_WITHOUT_DEVICE"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    detach = next(line for line in calls.splitlines() if line.startswith("hdiutil:detach "))
    mountpoint = Path(detach.removeprefix("hdiutil:detach "))
    assert not mountpoint.exists()


def test_dmg_verifier_cleans_partial_mount_when_attach_returns_failure(tmp_path: Path):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_ATTACH_FAIL_AFTER_MOUNT"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    detach = next(line for line in calls.splitlines() if line.startswith("hdiutil:detach "))
    mountpoint = Path(detach.removeprefix("hdiutil:detach "))
    assert not mountpoint.exists()


def test_dmg_verifier_accepts_detach_that_already_removed_mountpoint(tmp_path: Path):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_DETACH_REMOVES_MOUNTPOINT"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    detach = next(line for line in calls.splitlines() if line.startswith("hdiutil:detach "))
    assert not Path(detach.removeprefix("hdiutil:detach ")).exists()


def test_dmg_verifier_rejects_mounted_app_symlink_before_inspection(tmp_path: Path):
    dmg, env, log = _make_dmg_fixture(tmp_path)
    env["FAKE_MOUNT_APP_SYMLINK"] = "1"

    result = _run_dmg_fixture(dmg, "0.1.2", env)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_DMG_E_APP_SYMLINK"
    calls = log.read_text(encoding="utf-8")
    assert "check:" not in calls


@REAL_MACOS_RELEASE_TEST
@pytest.mark.skipif(
    os.environ.get("PDF2MD_RUN_REAL_DMG_TESTS") != "1",
    reason="set PDF2MD_RUN_REAL_DMG_TESTS=1 for privileged hdiutil integration",
)
def test_dmg_verifier_real_mount_accepts_tauri_like_synthetic_fixture(tmp_path: Path):
    sidecar_tests = runpy.run_path(str(ROOT / "tests/test_release_sidecar.py"))
    app = sidecar_tests["_executable_bundle"](tmp_path / "build")
    main_source = tmp_path / "main.c"
    main_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    main_executable = app / "Contents/MacOS/parsing-core-app"
    _run_checked(
        "/usr/bin/clang",
        "-arch",
        "arm64",
        str(main_source),
        "-o",
        str(main_executable),
    )
    _sign_m0_app(app)

    source = tmp_path / "dmg-source"
    source.mkdir()
    mounted_name = source / "PDF2MD.app"
    app.rename(mounted_name)
    (source / "Applications").symlink_to("/Applications")
    (source / ".background").mkdir()
    (source / ".background/background.png").write_bytes(b"safe synthetic background")
    (source / ".VolumeIcon.icns").write_bytes(b"safe synthetic icon")
    (source / ".DS_Store").write_bytes(b"safe synthetic finder metadata")
    writable_dmg = tmp_path / "PDF2MD-real-writable.dmg"
    _run_checked(
        "/usr/bin/hdiutil",
        "create",
        "-quiet",
        "-fs",
        "HFS+",
        "-volname",
        "PDF2MD",
        "-srcfolder",
        str(source),
        "-format",
        "UDRW",
        str(writable_dmg),
    )
    writable_mount = tmp_path / "writable-mount"
    writable_mount.mkdir()
    attached = False
    try:
        _run_checked(
            "/usr/bin/hdiutil",
            "attach",
            "-readwrite",
            "-nobrowse",
            "-mountpoint",
            str(writable_mount),
            str(writable_dmg),
        )
        attached = True
        (writable_mount / ".DS_Store").write_bytes(b"safe synthetic finder metadata")
    finally:
        if attached:
            _run_checked("/usr/bin/hdiutil", "detach", str(writable_mount))
    dmg = tmp_path / "PDF2MD-real.dmg"
    _run_checked(
        "/usr/bin/hdiutil",
        "convert",
        "-quiet",
        "-format",
        "UDZO",
        "-o",
        str(dmg),
        str(writable_dmg),
    )

    result = _run(str(DMG_SCRIPT), str(dmg), "0.1.2")

    assert result.returncode == 0, result.stderr


@REAL_MACOS_RELEASE_TEST
@pytest.mark.skipif(
    os.environ.get("PDF2MD_RUN_REAL_ARTIFACT_TESTS") != "1",
    reason="set PDF2MD_RUN_REAL_ARTIFACT_TESTS=1 with actual packaged paths",
)
def test_packaged_pdf2md_artifacts_pass_production_release_gates():
    version = os.environ["PDF2MD_REAL_VERSION"]
    bundle = ROOT / "parsing-core-app/src-tauri/target/release/bundle"
    app = Path(os.environ["PDF2MD_REAL_APP_PATH"]).resolve()
    dmg = Path(os.environ["PDF2MD_REAL_DMG_PATH"]).resolve()
    expected_app = (bundle / "macos/PDF2MD.app").resolve()
    expected_dmg = (bundle / f"dmg/PDF2MD_{version}_aarch64.dmg").resolve()

    assert app == expected_app
    assert dmg == expected_dmg
    assert (app / "Contents/MacOS/parsing-core-app").is_file()
    assert (app / "Contents/Resources/python-runtime/bin/python3.12").is_file()
    assert dmg.is_file()

    app_result = _run(str(BUNDLE_SCRIPT), str(app))
    assert app_result.returncode == 0, app_result.stderr
    assert app_result.stdout.strip() == "PDF2MD_BUNDLE_POLICY_M0_ADHOC_HARDENED_NOT_NOTARIZED"

    dmg_result = _run(str(DMG_SCRIPT), str(dmg), version)
    assert dmg_result.returncode == 0, dmg_result.stderr


def _macos_tool(path: str) -> str:
    assert Path(path).is_file(), f"required macOS tool is unavailable: {path}"
    return path


def _run_checked(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True)


def _sign_m0_app(app: Path) -> None:
    macho_magics = {
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
    }
    for target in sorted(app.joinpath("Contents").rglob("*")):
        if target.is_symlink() or not target.is_file():
            continue
        with target.open("rb") as handle:
            if handle.read(4) not in macho_magics:
                continue
        _run_checked(
            _macos_tool("/usr/bin/codesign"),
            "--force",
            "--options",
            "runtime",
            "--sign",
            "-",
            str(target),
        )
    _run_checked(
        _macos_tool("/usr/bin/codesign"),
        "--force",
        "--deep",
        "--options",
        "runtime",
        "--sign",
        "-",
        str(app),
    )


def _build_real_m0_app(tmp_path: Path) -> Path:
    app = tmp_path / "Real PDF2MD.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    runtime_bin = resources / "python-runtime/bin"
    frameworks = contents / "Frameworks"
    source = tmp_path / "native-source"
    for directory in (macos, runtime_bin, frameworks, source):
        directory.mkdir(parents=True, exist_ok=True)

    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "python3",
                "CFBundleIdentifier": "com.pdf2md.release-gate-fixture",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1.2",
            },
            handle,
        )

    library_source = source / "clean.c"
    library_source.write_text(
        "int pdf2md_release_gate_fixture(void) { return 0; }\n",
        encoding="utf-8",
    )
    executable_source = source / "main.c"
    executable_source.write_text(
        "extern int pdf2md_release_gate_fixture(void);\n"
        "int main(void) { return pdf2md_release_gate_fixture(); }\n",
        encoding="utf-8",
    )
    library = frameworks / "libclean.dylib"
    _run_checked(
        _macos_tool("/usr/bin/clang"),
        "-arch",
        "arm64",
        "-dynamiclib",
        str(library_source),
        "-Wl,-install_name,@rpath/libclean.dylib",
        "-Wl,-rpath,@loader_path",
        "-o",
        str(library),
    )
    runtime_python = runtime_bin / "python3.12"
    _run_checked(
        _macos_tool("/usr/bin/clang"),
        "-arch",
        "arm64",
        str(executable_source),
        "-L",
        str(frameworks),
        "-lclean",
        "-Wl,-rpath,@executable_path/../Frameworks",
        "-o",
        str(runtime_python),
    )
    shutil.copy2(runtime_python, macos / "python3")
    (runtime_bin / "python").symlink_to("python3.12")
    (runtime_bin / "python3").symlink_to("python3.12")
    package = resources / "src/parsing_core"
    package.mkdir(parents=True)
    (package / "module.py").write_text("VALUE = 'clean fixture'\n", encoding="utf-8")
    _sign_m0_app(app)
    return app


def _run_production_bundle_gate(
    app: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BUNDLE_SCRIPT), str(app)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_clean_shebang_ignores_bash_env_exit_zero(tmp_path: Path):
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("exit 0\n", encoding="utf-8")
    missing = tmp_path / "missing.app"

    result = _run_production_bundle_gate(missing, env=os.environ | {"BASH_ENV": str(bash_env)})

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_REQUIRED_PATH"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_ignores_pythonpath_sitecustomize_bypass(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    runtime = app / "Contents/Resources/python-runtime/bin/python3.12"
    _run_checked(
        _macos_tool("/usr/bin/install_name_tool"),
        "-change",
        "@rpath/libclean.dylib",
        "/Library/Untrusted/libuntrusted.dylib",
        str(runtime),
    )
    _sign_m0_app(app)
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")

    result = _run_production_bundle_gate(app, env=os.environ | {"PYTHONPATH": str(injection)})

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize(
    "dependency_target",
    ["@executable_path", "@rpath", "@rpath/", "@loader_path/not-macho.txt"],
)
def test_bundle_gate_rejects_dependency_target_that_is_not_regular_macho(
    tmp_path: Path, dependency_target: str
):
    app = _build_real_m0_app(tmp_path)
    runtime = app / "Contents/Resources/python-runtime/bin/python3.12"
    if dependency_target in {"@executable_path", "@rpath", "@rpath/"}:
        _run_checked(
            "/usr/bin/install_name_tool",
            "-change",
            "@rpath/libclean.dylib",
            dependency_target,
            str(runtime),
        )
    else:
        (runtime.parent / "not-macho.txt").write_text(
            "ordinary file, not Mach-O\n", encoding="utf-8"
        )
        _run_checked(
            "/usr/bin/install_name_tool",
            "-change",
            "@rpath/libclean.dylib",
            dependency_target,
            str(runtime),
        )
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_secret_in_extended_attribute(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    resource = app / "Contents/Resources/src/parsing_core/module.py"
    secret = b"sk-" + (b"X" * 40)
    _run_checked(
        "/usr/bin/xattr",
        "-w",
        "com.pdf2md.release-gate-fixture",
        secret.decode(),
        str(resource),
    )

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret.decode() not in result.stderr


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_secret_in_app_root_extended_attribute(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    secret = b"sk-" + (b"R" * 40)
    _run_checked(
        "/usr/bin/xattr",
        "-w",
        "com.pdf2md.release-gate-root-fixture",
        secret.decode(),
        str(app),
    )

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret.decode() not in result.stderr


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_secret_in_symlink_extended_attribute(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    resources = app / "Contents/Resources"
    target = resources / "clean-target.txt"
    target.write_text("clean\n", encoding="utf-8")
    link = resources / "clean-link"
    link.symlink_to(target.name)
    _sign_m0_app(app)
    secret = "sk-" + ("Y" * 40)
    _run_checked(
        "/usr/bin/xattr",
        "-s",
        "-w",
        "com.pdf2md.release-gate-fixture",
        secret,
        str(link),
    )

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret not in result.stderr


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_secret_inside_zip_member(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    archive = app / "Contents/Resources/payload.zip"
    secret = "token=" + "9iQKvf2xP7mZa4Bc8Dd0Gh6Jr1Ls5NtU"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("nested/config.env", secret)
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CREDENTIAL"
    assert secret not in result.stderr


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize(
    "content",
    [
        "BAIDU_SECRET_KEY='9iQKvf2xP7mZa4Bc8Dd0Gh6Jr1Ls5NtU'\n",
        "cache = '/private/tmp/pdf2md-release-build/runtime'\n",
        "cache = '/var/folders/aa/bb/pdf2md-release-build/runtime'\n",
    ],
)
def test_bundle_gate_rejects_extended_secret_and_development_path_patterns(
    tmp_path: Path, content: str
):
    app = _build_real_m0_app(tmp_path)
    (app / "Contents/Resources/src/parsing_core/module.py").write_text(content, encoding="utf-8")
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    expected = (
        "PDF2MD_BUNDLE_E_CREDENTIAL"
        if "SECRET_KEY" in content
        else "PDF2MD_BUNDLE_E_DEVELOPMENT_PATH"
    )
    assert result.stderr.strip() == expected


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_universal_macho_even_when_it_contains_arm64(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    source = tmp_path / "universal.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    arm64 = tmp_path / "arm64"
    x86_64 = tmp_path / "x86_64"
    universal = app / "Contents/Frameworks/universal-macho"
    _run_checked("/usr/bin/clang", "-arch", "arm64", str(source), "-o", str(arm64))
    _run_checked("/usr/bin/clang", "-arch", "x86_64", str(source), "-o", str(x86_64))
    _run_checked("/usr/bin/lipo", "-create", str(arm64), str(x86_64), "-o", str(universal))
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_ARCH"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_executable_non_macho_runtime(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    runtime = app / "Contents/Resources/python-runtime/bin/python3.12"
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o755)
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_RUNTIME"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_reports_explicit_m0_adhoc_hardened_policy(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)

    result = _run_production_bundle_gate(app)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("PDF2MD_BUNDLE_POLICY_M0_ADHOC_HARDENED_NOT_NOTARIZED")


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_resource_mutation_after_real_codesign(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    resource = app / "Contents/Resources/src/parsing_core/module.py"
    resource.write_text("VALUE = 'signature changed'\n", encoding="utf-8")

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_CODESIGN"


@REAL_MACOS_RELEASE_TEST
@pytest.mark.parametrize("archive_attack", ["traversal", "encrypted", "bomb", "nested"])
def test_bundle_gate_rejects_unsafe_or_unbounded_zip_archives(tmp_path: Path, archive_attack: str):
    app = _build_real_m0_app(tmp_path)
    archive = app / "Contents/Resources/attack.zip"
    if archive_attack == "traversal":
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../outside.txt", "clean")
        expected = "PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE"
    elif archive_attack == "encrypted":
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("config.txt", "clean")
        payload = bytearray(archive.read_bytes())
        local = payload.index(b"PK\x03\x04")
        central = payload.index(b"PK\x01\x02")
        payload[local + 6] |= 0x01
        payload[central + 8] |= 0x01
        archive.write_bytes(payload)
        expected = "PDF2MD_BUNDLE_E_ARCHIVE_UNSAFE"
    elif archive_attack == "bomb":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("zeros.bin", b"\0" * (2 * 1024 * 1024))
        expected = "PDF2MD_BUNDLE_E_ARCHIVE_LIMIT"
    else:
        payload = b"clean"
        for depth in range(4):
            nested = tmp_path / f"nested-{depth}.zip"
            with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr(f"level-{depth}.zip", payload)
            payload = nested.read_bytes()
        archive.write_bytes(payload)
        expected = "PDF2MD_BUNDLE_E_ARCHIVE_LIMIT"
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == expected


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_single_stream_compression_bomb(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    archive = app / "Contents/Resources/attack.gz"
    with gzip.open(archive, "wb") as stream:
        stream.write(b"\0" * (2 * 1024 * 1024))
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_ARCHIVE_LIMIT"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_counts_tar_directories_toward_member_limit(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    archive = app / "Contents/Resources/too-many-directories.tar"
    with tarfile.open(archive, "w") as bundle:
        for index in range(10_001):
            member = tarfile.TarInfo(f"directory-{index}/")
            member.type = tarfile.DIRTYPE
            bundle.addfile(member)
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_ARCHIVE_LIMIT"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_tree_added_after_initial_scan(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    core = runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))

    def mutate_after_inspection(_: str) -> None:
        (app / "Contents/Resources/late-file.txt").write_text("late\n", encoding="utf-8")

    core["verify_bundle"].__globals__["_verify_signature_policy"] = mutate_after_inspection

    with pytest.raises(core["GateError"]) as captured:
        core["verify_bundle"](str(app))

    assert captured.value.code == "PDF2MD_BUNDLE_E_TREE_DRIFT"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_fails_closed_when_regular_file_becomes_symlink_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = _build_real_m0_app(tmp_path)
    target = app / "Contents/Resources/src/parsing_core/module.py"
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    core = runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))
    original_stat = core["os"].stat
    attacked = False

    def replacing_stat(path, *args, **kwargs):
        nonlocal attacked
        value = original_stat(path, *args, **kwargs)
        if path == "module.py" and kwargs.get("dir_fd") is not None and not attacked:
            attacked = True
            target.unlink()
            target.symlink_to(outside)
        return value

    monkeypatch.setattr(core["os"], "stat", replacing_stat)

    with pytest.raises(core["GateError"]) as captured:
        core["TreeScanner"](str(app), inspect_content=False).scan()

    assert captured.value.code in {
        "PDF2MD_BUNDLE_E_SCAN",
        "PDF2MD_BUNDLE_E_TREE_DRIFT",
    }


@REAL_MACOS_RELEASE_TEST
def test_bundle_scanner_rejects_regular_mutation_during_xattr_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = _build_real_m0_app(tmp_path)
    target = app / "Contents/Resources/src/parsing_core/module.py"
    identity = (target.stat().st_dev, target.stat().st_ino)
    core = runpy.run_path(str(ROOT / "scripts/check_release_sidecar.py"))
    original_xattrs = core["_fd_xattrs"]
    attacked = False

    def mutating_xattrs(fd: int, *, inspect_content: bool):
        nonlocal attacked
        result = original_xattrs(fd, inspect_content=inspect_content)
        value = os.fstat(fd)
        if not attacked and (value.st_dev, value.st_ino) == identity:
            attacked = True
            target.write_text("VALUE = 'changed during xattr scan'\n", encoding="utf-8")
        return result

    monkeypatch.setitem(
        core["TreeScanner"]._scan_regular.__globals__,
        "_fd_xattrs",
        mutating_xattrs,
    )

    with pytest.raises(core["GateError"]) as captured:
        core["TreeScanner"](str(app), inspect_content=False).scan()

    assert captured.value.code == "PDF2MD_BUNDLE_E_TREE_DRIFT"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_adhoc_signature_without_hardened_runtime(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    _run_checked(
        "/usr/bin/codesign",
        "--force",
        "--deep",
        "--sign",
        "-",
        str(app),
    )

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_HARDENED_RUNTIME"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_nested_macho_without_hardened_runtime(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    nested = app / "Contents/Frameworks/libclean.dylib"
    _run_checked(
        "/usr/bin/codesign",
        "--force",
        "--sign",
        "-",
        str(nested),
    )
    _run_checked(
        "/usr/bin/codesign",
        "--force",
        "--options",
        "runtime",
        "--sign",
        "-",
        str(app),
    )

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_HARDENED_RUNTIME"


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_allows_internal_symlink_to_enumerated_arm64_macho(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    library = app / "Contents/Frameworks/libclean.dylib"
    real_library = library.with_name("libclean-real.dylib")
    library.rename(real_library)
    library.symlink_to(real_library.name)
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("PDF2MD_BUNDLE_POLICY_M0_ADHOC_HARDENED_NOT_NOTARIZED")


@REAL_MACOS_RELEASE_TEST
def test_bundle_gate_rejects_empty_rpath_dependency_suffix(tmp_path: Path):
    app = _build_real_m0_app(tmp_path)
    runtime = app / "Contents/Resources/python-runtime/bin/python3.12"
    _run_checked(
        "/usr/bin/install_name_tool",
        "-change",
        "@rpath/libclean.dylib",
        "@rpath",
        str(runtime),
    )
    _sign_m0_app(app)

    result = _run_production_bundle_gate(app)

    assert result.returncode != 0
    assert result.stderr.strip() == "PDF2MD_BUNDLE_E_MACHO_DEPENDENCY"
