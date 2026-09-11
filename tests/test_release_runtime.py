from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PREPARE = REPO / "parsing-core-app/scripts/prepare-sidecar-python.sh"
RUNTIME_HELPER = REPO / "parsing-core-app/scripts/sidecar_runtime.py"
ARCHIVE_NAME = "cpython-3.12.13+20260510-aarch64-apple-darwin-install_only.tar.gz"
ARCHIVE_SHA256 = "5a30271f8d345a5b02b0c9e4e31e0f1e1455a8e4a04fba95cd9762472abc3b17"


@dataclass
class Fixture:
    repo: Path
    prepare: Path
    uv: Path
    target: Path
    export_log: Path
    source: Path
    wheelhouse_root: Path
    environment: dict[str, str]


@dataclass
class PrefetchFixture:
    repo: Path
    python: Path
    uv: Path
    wheelhouse_root: Path
    requirements: bytes
    requirements_path: Path
    pip_log: Path
    uv_log: Path
    command: list[str]
    environment: dict[str, str]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _host_arm64_python(tmp_path: Path) -> tuple[Path, str]:
    host = Path(sys.executable).resolve()
    shim_source = tmp_path / "python-shim.c"
    shim = tmp_path / "python3.12"
    quoted_host = str(host).replace("\\", "\\\\").replace('"', '\\"')
    shim_source.write_text(
        "#include <stdio.h>\n"
        "#include <unistd.h>\n"
        f'static const char *host = "{quoted_host}";\n'
        "int main(int argc, char **argv) {\n"
        "  (void)argc;\n"
        "  argv[0] = (char *)host;\n"
        "  execv(host, argv);\n"
        '  perror("execv");\n'
        "  return 70;\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["/usr/bin/clang", "-arch", "arm64", "-O2", "-o", str(shim), str(shim_source)],
        capture_output=True,
        text=True,
        check=True,
    )
    version = platform.python_version()
    return shim, version


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, old
    return source.replace(old, new)


def _build_fixture_wheel(directory: Path, name: str, version: str) -> tuple[str, str, int]:
    filename = f"{name}-{version}-py3-none-any.whl"
    path = directory / filename
    dist_info = f"{name}-{version}.dist-info"
    entries = {
        f"{name}/__init__.py": f"__version__ = {version!r}\n",
        f"{dist_info}/METADATA": (f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: pdf2md-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    entries[f"{dist_info}/RECORD"] = "".join(f"{entry},,\n" for entry in entries)
    entries[f"{dist_info}/RECORD"] += f"{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry, payload in entries.items():
            archive.writestr(entry, payload)
    return filename, _digest(path), path.stat().st_size


def _create_prefetch_fixture(
    tmp_path: Path,
    *,
    bad_wheel_mode: bool = False,
    bad_wheel_xattr: bool = False,
    corrupt_download: bool = False,
) -> PrefetchFixture:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        """\
[project]
name = "parsing-core"
version = "9.8.7"
requires-python = ">=3.12,<3.13"

[project.optional-dependencies]
serve = ["fastapi==0.115.0", "jsonschema==4.23.0"]
""",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\nrevision = 1\n", encoding="utf-8")

    wheel_source = tmp_path / "wheel-source"
    wheel_source.mkdir()
    wheels = [
        _build_fixture_wheel(wheel_source, "fastapi", "0.115.0"),
        _build_fixture_wheel(wheel_source, "jsonschema", "4.23.0"),
    ]
    requirements = "".join(
        f"{name.split('-', 1)[0]}=={name.split('-', 2)[1]} --hash=sha256:{digest}\n"
        for name, digest, _size in sorted(wheels)
    ).encode()
    requirements_path = tmp_path / "requirements-serve.txt"
    requirements_path.write_bytes(requirements)
    if corrupt_download:
        corrupt = wheel_source / wheels[0][0]
        corrupt.write_bytes(corrupt.read_bytes() + b"not-locked")

    tools = tmp_path / "tools"
    tools.mkdir()
    uv_log = tmp_path / "uv-prefetch.log"
    uv = tools / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(uv_log))}\n"
        "if [[ ${1:-} == --version ]]; then printf 'uv 0.12.3 fixture\\n'; exit 0; fi\n"
        "[[ ${1:-} == export ]] || exit 64\n"
        "output=''\n"
        "while (( $# )); do\n"
        "  if [[ $1 == --output-file ]]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "[[ -n $output ]]\n"
        f'/bin/cp {shlex.quote(str(requirements_path))} "$output"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)

    pip_log = tmp_path / "pip-download.log"
    python = tools / "python3.12"
    python_identity = str(python.resolve())
    copy_commands = "".join(
        f'/bin/cp {shlex.quote(str(wheel_source / name))} "$destination/{name}"\n'
        for name, _digest_value, _size in wheels
    )
    bad_mode_command = f'/bin/chmod 666 "$destination/{wheels[0][0]}"\n' if bad_wheel_mode else ""
    bad_xattr_command = (
        f'/usr/bin/xattr -w com.pdf2md.fixture unsafe "$destination/{wheels[0][0]}"\n'
        if bad_wheel_xattr
        else ""
    )
    version_document = json.dumps(
        {"executable": python_identity, "machine": "arm64", "version": "3.12.13"},
        separators=(",", ":"),
        sort_keys=True,
    )
    python.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "if [[ ${1:-} == -I && ${2:-} == -S && ${3:-} == -B && ${4:-} == -c ]]; then\n"
        f"  printf '%s\\n' {shlex.quote(version_document)}\n"
        "  exit 0\n"
        "fi\n"
        "[[ ${1:-} == -I && ${2:-} == -B && ${3:-} == -m && ${4:-} == pip && "
        "${5:-} == download ]] || exit 64\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(pip_log))}\n"
        "destination=''\n"
        "while (( $# )); do\n"
        "  if [[ $1 == --dest ]]; then destination=$2; shift 2; else shift; fi\n"
        "done\n"
        "[[ -n $destination ]]\n"
        f"{copy_commands}"
        f"{bad_mode_command}"
        f"{bad_xattr_command}",
        encoding="utf-8",
    )
    python.chmod(0o755)

    wheelhouse_root = tmp_path / "wheelhouse"
    command = [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        str(RUNTIME_HELPER),
        "prefetch-wheelhouse",
        "--repo",
        str(repo),
        "--python-path",
        str(python),
        "--python-version",
        "3.12.13",
        "--uv-path",
        str(uv),
        "--uv-version",
        "0.12.3",
        "--wheelhouse-root",
        str(wheelhouse_root),
    ]
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
    }
    return PrefetchFixture(
        repo=repo,
        python=python,
        uv=uv,
        wheelhouse_root=wheelhouse_root,
        requirements=requirements,
        requirements_path=requirements_path,
        pip_log=pip_log,
        uv_log=uv_log,
        command=command,
        environment=environment,
    )


def _run_prefetch(fixture: PrefetchFixture) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        fixture.command,
        cwd=fixture.repo,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _load_runtime_helper() -> object:
    spec = importlib.util.spec_from_file_location("pdf2md_release_runtime", RUNTIME_HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prefetch_input_payload(helper: object, fixture: PrefetchFixture) -> bytes:
    return helper._prefetch_input_bytes(
        fixture.repo,
        fixture.python,
        "3.12.13",
        fixture.uv,
        "0.12.3",
    )


def _path_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _seed_prefetch_transaction(
    fixture: PrefetchFixture,
) -> tuple[object, str, Path, Path]:
    helper = _load_runtime_helper()
    input_payload = _prefetch_input_payload(helper, fixture)
    transaction_id = helper._prefetch_transaction_id(input_payload)
    fixture.wheelhouse_root.mkdir(mode=0o700)
    intent = fixture.wheelhouse_root / f".prefetch.{transaction_id}.intent"
    intent.write_bytes(helper._prefetch_intent_bytes(transaction_id, input_payload))
    intent.chmod(0o600)
    workspace = fixture.wheelhouse_root / f".prefetch.{transaction_id}.workspace"
    workspace.mkdir(mode=0o700)
    return helper, transaction_id, intent, workspace


def _canonical_json_bytes(document: object) -> bytes:
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _legacy_workspace_receipt_bytes(
    transaction_id: str,
    workspace_identity: tuple[int, int, int],
) -> bytes:
    return _canonical_json_bytes(
        {
            "schema": 1,
            "transaction_id": transaction_id,
            "workspace_identity": list(workspace_identity),
        }
    )


def _bound_workspace_receipt_bytes(
    transaction_id: str,
    workspace_identity: tuple[int, int, int],
    receipt_identity: tuple[int, int, int],
) -> bytes:
    return _canonical_json_bytes(
        {
            "receipt_identity": list(receipt_identity),
            "schema": 2,
            "transaction_id": transaction_id,
            "workspace_identity": list(workspace_identity),
        }
    )


def _write_receipt_candidate(
    workspace: Path,
    transaction_id: str,
    *,
    seed_name: str,
) -> tuple[Path, bytes, tuple[int, int, int]]:
    seed = workspace / seed_name
    descriptor = os.open(seed, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        receipt_identity = _path_identity(seed)
        payload = _bound_workspace_receipt_bytes(
            transaction_id,
            _path_identity(workspace),
            receipt_identity,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    candidate = workspace / (
        f".prefetch-workspace.{transaction_id}.receipt.candidate."
        f"{receipt_identity[0]:016x}.{receipt_identity[1]:016x}."
        f"{receipt_identity[2]:08x}"
    )
    seed.rename(candidate)
    return candidate, payload, receipt_identity


def _scan_prefetch_namespace(helper: object, wheelhouse_root: Path) -> object:
    lock = wheelhouse_root.parent / ".prefetch-scan.lock"
    with helper.secure_build_lock(lock, cleanup_root=wheelhouse_root) as cleanup_guard:
        return helper._scan_prefetch_namespace(wheelhouse_root, cleanup_guard)


def _run_prefetch_with_real_exit(
    fixture: PrefetchFixture,
    tmp_path: Path,
    checkpoint: str,
) -> subprocess.CompletedProcess[str]:
    source = RUNTIME_HELPER.read_text(encoding="utf-8")
    suffix = hashlib.sha256(checkpoint.encode()).hexdigest()
    crashing_helper = tmp_path / f"sidecar_runtime_crash_{suffix}.py"
    indentation = checkpoint[: len(checkpoint) - len(checkpoint.lstrip())]
    crashing_helper.write_text(
        _replace_once(source, checkpoint, f"{indentation}os._exit(93)\n{checkpoint}"),
        encoding="utf-8",
    )
    command = list(fixture.command)
    command[4] = str(crashing_helper)
    return subprocess.run(
        command,
        cwd=fixture.repo,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _crash_prefetch_after_workspace_receipt_delete(
    fixture: PrefetchFixture,
) -> subprocess.CompletedProcess[str]:
    program = f"""
import importlib.util
import os
import pathlib
import stat

helper_path = pathlib.Path({str(RUNTIME_HELPER)!r})
spec = importlib.util.spec_from_file_location("pdf2md_prefetch_cleanup_crash", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
original_remove = helper._remove_private_cleanup_claim
crashed = False

def crash_after_receipt(descriptor, name, expected, **kwargs):
    global crashed
    is_receipt = False
    if expected.file_type == stat.S_IFREG:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            is_receipt = b'workspace_identity' in os.read(child, 4096)
        finally:
            os.close(child)
    original_remove(descriptor, name, expected, **kwargs)
    if is_receipt and not crashed:
        crashed = True
        os._exit(94)

helper._remove_private_cleanup_claim = crash_after_receipt
helper.prefetch_wheelhouse(
    repo=pathlib.Path({str(fixture.repo)!r}),
    python_path=pathlib.Path({str(fixture.python)!r}),
    python_version='3.12.13',
    uv_path=pathlib.Path({str(fixture.uv)!r}),
    uv_version='0.12.3',
    wheelhouse_root=pathlib.Path({str(fixture.wheelhouse_root)!r}),
)
"""
    return subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", "-c", program],
        cwd=fixture.repo,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_prefetch_builds_the_exact_content_addressed_wheelhouse_without_live_pypi(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)

    result = _run_prefetch(fixture)

    assert result.returncode == 0, result.stderr
    requirements_sha256 = hashlib.sha256(fixture.requirements).hexdigest()
    wheelhouse = fixture.wheelhouse_root / requirements_sha256
    assert stat.S_IMODE(fixture.wheelhouse_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(wheelhouse.stat().st_mode) == 0o700
    manifest = json.loads((wheelhouse / ".wheelhouse-manifest.json").read_bytes())
    assert manifest == {
        "files": [
            {
                "name": path.name,
                "sha256": _digest(path),
                "size": path.stat().st_size,
            }
            for path in sorted(wheelhouse.glob("*.whl"))
        ],
        "lock_sha256": _digest(fixture.repo / "uv.lock"),
        "requirements_sha256": requirements_sha256,
        "schema": 1,
    }
    for path in wheelhouse.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert path.stat().st_nlink == 1
        xattrs = subprocess.run(
            ["/usr/bin/xattr", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert set(xattrs.stdout.splitlines()) <= {"com.apple.provenance"}
    uv_calls = fixture.uv_log.read_text(encoding="utf-8").splitlines()
    exports = [line for line in uv_calls if line.startswith("export ")]
    assert len(exports) == 2
    for export in exports:
        assert "--frozen" in export
        assert "--offline" in export
        assert "--extra serve" in export
        assert "--no-emit-project" in export
        assert "--no-header" in export
        assert "--no-annotate" in export
    pip_call = fixture.pip_log.read_text(encoding="utf-8")
    assert "-m pip download" in pip_call
    assert "--require-hashes" in pip_call
    assert "--only-binary=:all:" in pip_call
    assert "--no-deps" in pip_call
    assert "--no-cache-dir" in pip_call


def test_prefetch_is_idempotent_for_concurrent_and_repeated_calls(tmp_path: Path) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    processes = [
        subprocess.Popen(
            fixture.command,
            cwd=fixture.repo,
            env=fixture.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]

    outputs = [process.communicate(timeout=90) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], outputs
    repeated = _run_prefetch(fixture)
    assert repeated.returncode == 0, repeated.stderr
    assert len(fixture.pip_log.read_text(encoding="utf-8").splitlines()) == 1
    requirements_sha256 = hashlib.sha256(fixture.requirements).hexdigest()
    assert {path.name for path in fixture.wheelhouse_root.iterdir()} == {requirements_sha256}


def test_prefetch_fails_closed_on_a_tampered_published_wheelhouse(tmp_path: Path) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    first = _run_prefetch(fixture)
    assert first.returncode == 0, first.stderr
    wheel = next(fixture.wheelhouse_root.glob("*/*.whl"))
    wheel.chmod(0o600)
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    wheel.chmod(0o400)
    tampered = wheel.read_bytes()

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "wheelhouse file" in result.stderr
    assert wheel.read_bytes() == tampered
    assert len(fixture.pip_log.read_text(encoding="utf-8").splitlines()) == 1


def test_prefetch_fails_closed_on_a_conflicting_existing_digest_directory(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    requirements_sha256 = hashlib.sha256(fixture.requirements).hexdigest()
    fixture.wheelhouse_root.mkdir(mode=0o700)
    conflict = fixture.wheelhouse_root / requirements_sha256
    conflict.mkdir(mode=0o700)
    marker = conflict / "foreign.txt"
    marker.write_text("do not replace\n", encoding="utf-8")

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "do not replace\n"
    assert not fixture.pip_log.exists()


@pytest.mark.parametrize(
    ("fixture_options", "expected_error"),
    [
        ({"bad_wheel_mode": True}, "wheel is group/world writable"),
        ({"bad_wheel_xattr": True}, "wheel has untrusted extended attributes"),
        ({"corrupt_download": True}, "wheel hash is not locked"),
    ],
)
def test_prefetch_rejects_untrusted_or_unlocked_downloads(
    tmp_path: Path,
    fixture_options: dict[str, bool],
    expected_error: str,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path, **fixture_options)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert expected_error in result.stderr
    requirements_sha256 = hashlib.sha256(fixture.requirements).hexdigest()
    assert not (fixture.wheelhouse_root / requirements_sha256).exists()


def test_prefetch_requires_canonical_pinned_python_and_uv_paths(tmp_path: Path) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    python_link = fixture.python.with_name("python-link")
    python_link.symlink_to(fixture.python.name)
    linked_command = list(fixture.command)
    linked_command[linked_command.index("--python-path") + 1] = str(python_link)

    noncanonical = subprocess.run(
        linked_command,
        cwd=fixture.repo,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    wrong_version = list(fixture.command)
    wrong_version[wrong_version.index("--python-version") + 1] = "3.12.12"
    unpinned = subprocess.run(
        wrong_version,
        cwd=fixture.repo,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert noncanonical.returncode != 0
    assert "canonical" in noncanonical.stderr
    assert unpinned.returncode != 0
    assert "invalid choice" in unpinned.stderr


@pytest.mark.parametrize(
    "checkpoint",
    [
        "        workspace_identity = file_identity(workspace)\n",
        (
            "            if _read_bound_regular(lock_path, "
            "max_bytes=64 * 1024 * 1024) != lock_before:\n"
        ),
        "            records = _harden_and_record_wheels(\n",
    ],
    ids=["workspace-open", "requirements-write", "wheel-download"],
)
def test_prefetch_real_exits_reuse_one_bound_workspace_and_then_recover(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    snapshots: list[list[str]] = []

    for _ in range(3):
        crashed = _run_prefetch_with_real_exit(fixture, tmp_path, checkpoint)
        assert crashed.returncode == 93, crashed.stderr
        snapshots.append(
            sorted(
                path.name
                for path in fixture.wheelhouse_root.iterdir()
                if path.name.startswith(".prefetch") or path.name.startswith(".sidecar-runtime.")
            )
        )

    assert snapshots[0] == snapshots[1] == snapshots[2]
    recovered = _run_prefetch(fixture)
    assert recovered.returncode == 0, recovered.stderr
    assert not [
        path
        for path in fixture.wheelhouse_root.iterdir()
        if path.name.startswith(".prefetch") or path.name.startswith(".sidecar-runtime.")
    ]


def test_prefetch_rejects_foreign_or_malformed_workspace_without_deleting_it(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    helper = _load_runtime_helper()
    transaction_id = helper._prefetch_transaction_id(_prefetch_input_payload(helper, fixture))
    fixture.wheelhouse_root.mkdir(mode=0o700)
    foreign = fixture.wheelhouse_root / f".prefetch.{transaction_id}.workspace"
    foreign.mkdir(mode=0o700)
    marker = foreign / "foreign"
    marker.write_text("preserve", encoding="utf-8")

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "prefetch workspace" in result.stderr
    assert marker.read_text(encoding="utf-8") == "preserve"
    shutil.rmtree(foreign)

    malformed = fixture.wheelhouse_root / ".prefetch.not-a-transaction.workspace"
    malformed.mkdir(mode=0o700)
    malformed_marker = malformed / "foreign"
    malformed_marker.write_text("preserve", encoding="utf-8")
    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "malformed prefetch" in result.stderr
    assert malformed_marker.read_text(encoding="utf-8") == "preserve"


def test_prefetch_rejects_forged_intent_for_an_empty_workspace(tmp_path: Path) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    helper = _load_runtime_helper()
    transaction_id = helper._prefetch_transaction_id(_prefetch_input_payload(helper, fixture))
    fixture.wheelhouse_root.mkdir(mode=0o700)
    intent = fixture.wheelhouse_root / f".prefetch.{transaction_id}.intent"
    intent.write_text(
        json.dumps(
            {
                "input_sha256": "0" * 64,
                "schema": 1,
                "transaction_id": transaction_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    intent.chmod(0o600)
    workspace = fixture.wheelhouse_root / f".prefetch.{transaction_id}.workspace"
    workspace.mkdir(mode=0o700)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "invalid prefetch intent" in result.stderr
    assert intent.is_file()
    assert workspace.is_dir()
    assert not list(workspace.iterdir())


def test_prefetch_rejects_an_intent_with_a_matching_prefix_and_forged_digest_tail(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    helper = _load_runtime_helper()
    input_payload = _prefetch_input_payload(helper, fixture)
    input_sha256 = hashlib.sha256(input_payload).hexdigest()
    transaction_id = input_sha256[:32]
    forged_tail = "0" * 32 if input_sha256[32:] != "0" * 32 else "1" * 32
    fixture.wheelhouse_root.mkdir(mode=0o700)
    intent = fixture.wheelhouse_root / f".prefetch.{transaction_id}.intent"
    intent.write_text(
        json.dumps(
            {
                "input": json.loads(input_payload),
                "input_sha256": transaction_id + forged_tail,
                "schema": 1,
                "transaction_id": transaction_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    intent.chmod(0o600)
    workspace = fixture.wheelhouse_root / f".prefetch.{transaction_id}.workspace"
    workspace.mkdir(mode=0o700)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "invalid prefetch intent" in result.stderr
    assert intent.is_file()
    assert workspace.is_dir()


def test_prefetch_rejects_a_self_hashed_noncanonical_input_document(tmp_path: Path) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    helper = _load_runtime_helper()
    input_document = json.loads(_prefetch_input_payload(helper, fixture))
    input_document["unexpected"] = "must-not-be-accepted"
    input_payload = (
        json.dumps(input_document, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    input_sha256 = hashlib.sha256(input_payload).hexdigest()
    transaction_id = input_sha256[:32]
    fixture.wheelhouse_root.mkdir(mode=0o700)
    intent = fixture.wheelhouse_root / f".prefetch.{transaction_id}.intent"
    intent.write_text(
        json.dumps(
            {
                "input": input_document,
                "input_sha256": input_sha256,
                "schema": 1,
                "transaction_id": transaction_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    intent.chmod(0o600)
    workspace = fixture.wheelhouse_root / f".prefetch.{transaction_id}.workspace"
    workspace.mkdir(mode=0o700)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "invalid prefetch intent" in result.stderr
    assert intent.is_file()
    assert workspace.is_dir()


@pytest.mark.parametrize(
    "variant",
    [
        "pretty-print",
        "key-order",
        "missing-newline",
        "leading-whitespace",
        "trailing-whitespace",
        "duplicate-key",
        "noncanonical-number",
        "noncanonical-escape",
    ],
)
def test_prefetch_rejects_and_preserves_noncanonical_intent_bytes(
    tmp_path: Path,
    variant: str,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    helper, transaction_id, intent, workspace = _seed_prefetch_transaction(fixture)
    canonical = intent.read_bytes()
    document = json.loads(canonical)
    if variant == "pretty-print":
        mutated = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    elif variant == "key-order":
        reordered = dict(reversed(list(document.items())))
        mutated = (json.dumps(reordered, separators=(",", ":"), sort_keys=False) + "\n").encode()
    elif variant == "missing-newline":
        mutated = canonical[:-1]
    elif variant == "leading-whitespace":
        mutated = b" " + canonical
    elif variant == "trailing-whitespace":
        mutated = canonical + b"\n"
    elif variant == "duplicate-key":
        input_bytes = json.dumps(document["input"], separators=(",", ":"), sort_keys=True).encode()
        prefix = b'{"input":'
        assert canonical.startswith(prefix + input_bytes + b",")
        mutated = prefix + input_bytes + b',"input":' + canonical[len(prefix) :]
    elif variant == "noncanonical-number":
        marker = b'"schema":1,"transaction_id"'
        position = canonical.rfind(marker)
        assert position >= 0
        mutated = (
            canonical[:position]
            + b'"schema":1.0,"transaction_id"'
            + canonical[position + len(marker) :]
        )
    else:
        assert variant == "noncanonical-escape"
        assert b"/" in canonical
        mutated = canonical.replace(b"/", b"\\u002f", 1)
    assert mutated != canonical
    intent.write_bytes(mutated)
    intent.chmod(0o600)
    intent_identity = _path_identity(intent)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "invalid prefetch intent" in result.stderr
    assert intent.read_bytes() == mutated
    assert _path_identity(intent) == intent_identity
    assert workspace.is_dir()
    assert not list(workspace.iterdir())


@pytest.mark.parametrize("prefix_size", [0, 1, 12, -1, -2])
def test_prefetch_recovers_a_legacy_final_receipt_canonical_prefix_in_place(
    tmp_path: Path,
    prefix_size: int,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, _transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    transaction_id = _transaction_id
    receipt = workspace / ".prefetch-workspace.json"
    legacy = _legacy_workspace_receipt_bytes(transaction_id, _path_identity(workspace))
    if prefix_size == -1:
        resolved_size = len(legacy) - 1
    elif prefix_size == -2:
        resolved_size = len(legacy)
    else:
        resolved_size = prefix_size
    receipt.write_bytes(legacy[:resolved_size])
    receipt.chmod(0o600)
    receipt_identity = _path_identity(receipt)
    checkpoint = "                _retire_cleanup_candidate(\n                    workspace,\n"

    crashed = _run_prefetch_with_real_exit(fixture, tmp_path, checkpoint)

    assert crashed.returncode == 93, crashed.stderr
    assert _path_identity(receipt) == receipt_identity
    payload = receipt.read_bytes()
    document = json.loads(payload)
    assert payload == _canonical_json_bytes(document)
    assert document == {
        "receipt_identity": list(receipt_identity),
        "schema": 2,
        "transaction_id": transaction_id,
        "workspace_identity": list(_path_identity(workspace)),
    }

    recovered = _run_prefetch(fixture)

    assert recovered.returncode == 0, recovered.stderr


def test_prefetch_rejects_a_noncanonical_final_receipt_without_rewriting_it(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    receipt = workspace / ".prefetch-workspace.json"
    receipt.touch(mode=0o600)
    receipt_identity = _path_identity(receipt)
    canonical = _bound_workspace_receipt_bytes(
        transaction_id,
        _path_identity(workspace),
        receipt_identity,
    )
    document = json.loads(canonical)
    noncanonical = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    receipt.write_bytes(noncanonical)
    receipt.chmod(0o600)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "workspace identity receipt" in result.stderr
    assert receipt.read_bytes() == noncanonical
    assert _path_identity(receipt) == receipt_identity


@pytest.mark.parametrize("entry_type", ["mode", "symlink", "directory"])
def test_prefetch_rejects_an_untrusted_final_receipt_type_or_mode(
    tmp_path: Path,
    entry_type: str,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    receipt = workspace / ".prefetch-workspace.json"
    if entry_type == "symlink":
        foreign = tmp_path / "foreign-receipt"
        foreign.write_text("preserve", encoding="utf-8")
        receipt.symlink_to(foreign)
    elif entry_type == "directory":
        receipt.mkdir(mode=0o700)
    else:
        receipt.touch(mode=0o600)
        receipt_identity = _path_identity(receipt)
        receipt.write_bytes(
            _bound_workspace_receipt_bytes(
                transaction_id,
                _path_identity(workspace),
                receipt_identity,
            )
        )
        receipt.chmod(0o644)
    identity = _path_identity(receipt)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "workspace identity receipt" in result.stderr
    assert _path_identity(receipt) == identity


def test_prefetch_rejects_a_byte_identical_replacement_final_receipt(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    receipt = workspace / ".prefetch-workspace.json"
    receipt.touch(mode=0o600)
    original_identity = _path_identity(receipt)
    payload = _bound_workspace_receipt_bytes(
        transaction_id,
        _path_identity(workspace),
        original_identity,
    )
    receipt.write_bytes(payload)
    receipt.chmod(0o600)
    displaced = tmp_path / "displaced-final-receipt"
    receipt.rename(displaced)
    replacement = tmp_path / "replacement-final-receipt"
    replacement.write_bytes(payload)
    replacement.chmod(0o600)
    replacement.rename(receipt)
    replacement_identity = _path_identity(receipt)
    assert replacement_identity != original_identity

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "workspace identity receipt" in result.stderr
    assert receipt.read_bytes() == payload
    assert _path_identity(receipt) == replacement_identity
    assert displaced.read_bytes() == payload


@pytest.mark.parametrize("phase", ["empty", "partial", "complete"])
def test_prefetch_recovers_a_bound_receipt_allocation_without_replacing_it(
    tmp_path: Path,
    phase: str,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    allocation = workspace / (f".prefetch-workspace.{transaction_id}.receipt.allocating")
    allocation.touch(mode=0o600)
    allocation_identity = _path_identity(allocation)
    expected = _bound_workspace_receipt_bytes(
        transaction_id,
        _path_identity(workspace),
        allocation_identity,
    )
    if phase == "partial":
        allocation.write_bytes(expected[:12])
    elif phase == "complete":
        allocation.write_bytes(expected)
    allocation.chmod(0o600)

    result = _run_prefetch(fixture)

    assert result.returncode == 0, result.stderr


def test_prefetch_recovers_a_complete_unpublished_receipt_candidate(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    _write_receipt_candidate(
        workspace,
        transaction_id,
        seed_name="candidate-seed",
    )

    result = _run_prefetch(fixture)

    assert result.returncode == 0, result.stderr


def test_prefetch_rejects_and_preserves_an_invalid_receipt_allocation(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    allocation = workspace / (f".prefetch-workspace.{transaction_id}.receipt.allocating")
    allocation.write_bytes(b"not-a-canonical-prefix")
    allocation.chmod(0o600)
    identity = _path_identity(allocation)

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "workspace identity receipt" in result.stderr
    assert allocation.read_bytes() == b"not-a-canonical-prefix"
    assert _path_identity(allocation) == identity


def test_prefetch_rejects_a_replaced_receipt_candidate_without_overwriting_it(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    candidate, payload, original_identity = _write_receipt_candidate(
        workspace,
        transaction_id,
        seed_name="original-candidate",
    )
    displaced = tmp_path / "displaced-candidate"
    candidate.rename(displaced)
    replacement = workspace / "replacement-candidate"
    replacement.write_bytes(payload)
    replacement.chmod(0o600)
    replacement.rename(candidate)
    replacement_identity = _path_identity(candidate)
    assert replacement_identity != original_identity

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "workspace identity receipt" in result.stderr
    assert candidate.read_bytes() == payload
    assert _path_identity(candidate) == replacement_identity
    assert displaced.read_bytes() == payload


def test_prefetch_rejects_multiple_receipt_candidates_without_deleting_them(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    first, _first_payload, _first_identity = _write_receipt_candidate(
        workspace,
        transaction_id,
        seed_name="first-candidate",
    )
    second, _second_payload, _second_identity = _write_receipt_candidate(
        workspace,
        transaction_id,
        seed_name="second-candidate",
    )

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "multiple workspace receipt candidates" in result.stderr
    assert first.is_file()
    assert second.is_file()


def test_prefetch_rejects_unbound_content_beside_a_receipt_candidate(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    _helper, transaction_id, _intent, workspace = _seed_prefetch_transaction(fixture)
    candidate, payload, candidate_identity = _write_receipt_candidate(
        workspace,
        transaction_id,
        seed_name="candidate-with-foreign-content",
    )
    marker = workspace / "foreign-content"
    marker.write_text("preserve", encoding="utf-8")

    result = _run_prefetch(fixture)

    assert result.returncode != 0
    assert "workspace lacks its identity receipt" in result.stderr
    assert candidate.read_bytes() == payload
    assert _path_identity(candidate) == candidate_identity
    assert marker.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    "checkpoint",
    [
        "    candidate_identity = _promote_prefetch_receipt_allocation(\n",
        "    published_identity = _publish_prefetch_workspace_receipt_candidate(\n",
        "    receipt_identity = _read_prefetch_workspace_receipt(\n",
    ],
    ids=["allocation-ready", "candidate-ready", "published-unverified"],
)
def test_prefetch_receipt_real_exits_keep_one_bounded_state_and_then_recover(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    snapshots: list[list[str]] = []

    for _ in range(3):
        crashed = _run_prefetch_with_real_exit(fixture, tmp_path, checkpoint)
        assert crashed.returncode == 93, crashed.stderr
        workspace = next(fixture.wheelhouse_root.glob(".prefetch.*.workspace"))
        snapshots.append(sorted(path.name for path in workspace.iterdir()))

    assert snapshots[0] == snapshots[1] == snapshots[2]

    recovered = _run_prefetch(fixture)

    assert recovered.returncode == 0, recovered.stderr
    assert not [
        path
        for path in fixture.wheelhouse_root.iterdir()
        if path.name.startswith(".prefetch") or path.name.startswith(".sidecar-runtime.")
    ]


def test_prefetch_namespace_scan_is_bounded(tmp_path: Path) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    fixture.wheelhouse_root.mkdir(mode=0o700)
    for index in range(5):
        (fixture.wheelhouse_root / f"{index:064x}").mkdir(mode=0o700)
    helper_source = RUNTIME_HELPER.read_text(encoding="utf-8")
    bounded_helper = tmp_path / "sidecar_runtime_bounded_prefetch.py"
    bounded_helper.write_text(
        _replace_once(
            helper_source,
            "MAX_CLEANUP_NAMESPACE_ENTRIES = 4_096",
            "MAX_CLEANUP_NAMESPACE_ENTRIES = 4",
        ),
        encoding="utf-8",
    )
    command = list(fixture.command)
    command[4] = str(bounded_helper)

    result = subprocess.run(
        command,
        cwd=fixture.repo,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert result.returncode != 0
    assert "prefetch namespace entry limit" in result.stderr


def test_prefetch_namespace_stops_consuming_at_limit_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    for index in range(12):
        (wheelhouse_root / f"{index:064x}").mkdir(mode=0o700)
    monkeypatch.setattr(helper, "MAX_CLEANUP_NAMESPACE_ENTRIES", 4)
    original_scandir = helper.os.scandir
    consumed = 0

    class CountingScandir:
        def __init__(self, target: object) -> None:
            self._context = original_scandir(target)
            self._iterator: object | None = None

        def __enter__(self) -> CountingScandir:
            self._iterator = self._context.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._context.__exit__(*args)

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> object:
            nonlocal consumed
            assert self._iterator is not None
            entry = next(self._iterator)
            consumed += 1
            return entry

    monkeypatch.setattr(helper.os, "scandir", CountingScandir)

    with pytest.raises(ValueError, match="prefetch namespace entry limit"):
        _scan_prefetch_namespace(helper, wheelhouse_root)

    assert consumed == 5


def test_prefetch_workspace_scan_stops_consuming_at_limit_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_runtime_helper()
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    monkeypatch.setattr(helper, "MAX_CLEANUP_NAMESPACE_ENTRIES", 4)
    consumed = 0

    class Entry:
        def __init__(self, index: int) -> None:
            self.name = f"entry-{index}"

    class CountingScandir:
        def __enter__(self) -> CountingScandir:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> Entry:
            nonlocal consumed
            if consumed >= 10_000:
                raise StopIteration
            entry = Entry(consumed)
            consumed += 1
            return entry

    monkeypatch.setattr(helper.os, "scandir", lambda _target: CountingScandir())

    with pytest.raises(ValueError, match="prefetch workspace namespace entry limit"):
        helper._scan_prefetch_workspace_entries(workspace)

    assert consumed == 5


def test_prefetch_accepts_a_private_stable_published_wheelhouse_directory(
    tmp_path: Path,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    published = wheelhouse_root / ("a" * 64)
    published.mkdir(mode=0o700)

    families = _scan_prefetch_namespace(helper, wheelhouse_root)

    assert families == {}


@pytest.mark.parametrize("mode", [0o707, 0o770, 0o755])
def test_prefetch_rejects_a_published_wheelhouse_without_owner_only_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    published = wheelhouse_root / ("b" * 64)
    published.mkdir(mode=mode)

    with pytest.raises(ValueError, match="published wheelhouse owner/mode"):
        _scan_prefetch_namespace(helper, wheelhouse_root)

    assert published.is_dir()


def test_prefetch_rejects_a_published_wheelhouse_owned_by_another_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    published = wheelhouse_root / ("c" * 64)
    published.mkdir(mode=0o700)
    original_stat = helper.os.stat

    def report_foreign_owner(path: object, *args: object, **kwargs: object) -> os.stat_result:
        metadata = original_stat(path, *args, **kwargs)
        if path == published.name and kwargs.get("dir_fd") is not None:
            values = list(metadata)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(helper.os, "stat", report_foreign_owner)

    with pytest.raises(ValueError, match="published wheelhouse owner/mode"):
        _scan_prefetch_namespace(helper, wheelhouse_root)

    assert published.is_dir()


def test_prefetch_rejects_a_published_wheelhouse_with_untrusted_xattrs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    published = wheelhouse_root / ("d" * 64)
    published.mkdir(mode=0o700)
    original_xattrs = helper._xattrs

    def report_xattrs(path: Path) -> list[str]:
        if path == published:
            return ["com.pdf2md.untrusted"]
        return original_xattrs(path)

    monkeypatch.setattr(helper, "_xattrs", report_xattrs)

    with pytest.raises(ValueError, match="published wheelhouse owner/mode"):
        _scan_prefetch_namespace(helper, wheelhouse_root)

    assert published.is_dir()


@pytest.mark.parametrize("entry_type", ["file", "symlink", "fifo"])
def test_prefetch_rejects_a_non_directory_published_wheelhouse_entry(
    tmp_path: Path,
    entry_type: str,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    published = wheelhouse_root / ("e" * 64)
    if entry_type == "file":
        published.write_text("foreign", encoding="utf-8")
    elif entry_type == "symlink":
        target = tmp_path / "foreign"
        target.mkdir()
        published.symlink_to(target)
    else:
        os.mkfifo(published, 0o600)

    with pytest.raises(ValueError, match="published wheelhouse entry is not a directory"):
        _scan_prefetch_namespace(helper, wheelhouse_root)


def test_prefetch_rejects_a_published_wheelhouse_replaced_after_scan_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_runtime_helper()
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    published = wheelhouse_root / ("f" * 64)
    published.mkdir(mode=0o700)
    displaced = tmp_path / "displaced-wheelhouse"
    original_stat = helper.os.stat
    replaced = False

    def replace_after_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal replaced
        metadata = original_stat(path, *args, **kwargs)
        if path == published.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            published.rename(displaced)
            published.mkdir(mode=0o700)
        return metadata

    monkeypatch.setattr(helper.os, "stat", replace_after_stat)

    with pytest.raises(ValueError, match="published wheelhouse identity changed"):
        _scan_prefetch_namespace(helper, wheelhouse_root)

    assert displaced.is_dir()
    assert published.is_dir()


def test_production_prefetch_uses_the_transaction_id_source_of_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)
    helper = _load_runtime_helper()
    original_transaction_id = helper._prefetch_transaction_id
    original_ensure_root = helper._ensure_private_wheelhouse_root
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    events: list[str] = []

    def observe_transaction_id(*args: object, **kwargs: object) -> str:
        events.append("transaction-id")
        calls.append((args, kwargs))
        return original_transaction_id(*args, **kwargs)

    def observe_ensure_root(path: Path) -> None:
        events.append("ensure-root")
        original_ensure_root(path)

    monkeypatch.setattr(helper, "_prefetch_transaction_id", observe_transaction_id)
    monkeypatch.setattr(helper, "_ensure_private_wheelhouse_root", observe_ensure_root)

    wheelhouse = helper.prefetch_wheelhouse(
        repo=fixture.repo,
        python_path=fixture.python,
        python_version="3.12.13",
        uv_path=fixture.uv,
        uv_version="0.12.3",
        wheelhouse_root=fixture.wheelhouse_root,
    )

    assert wheelhouse.path.is_dir()
    expected_payload = _prefetch_input_payload(helper, fixture)
    assert events[0] == "transaction-id"
    assert calls
    assert all(args == (expected_payload,) and not kwargs for args, kwargs in calls)


def test_prefetch_cleanup_recovers_when_receipt_was_deleted_before_root(
    tmp_path: Path,
) -> None:
    fixture = _create_prefetch_fixture(tmp_path)

    crashed = _crash_prefetch_after_workspace_receipt_delete(fixture)

    assert crashed.returncode == 94, crashed.stderr
    assert list(fixture.wheelhouse_root.glob(".prefetch.*.intent"))
    assert list(fixture.wheelhouse_root.glob(".sidecar-runtime.claim.*"))

    recovered = _run_prefetch(fixture)

    assert recovered.returncode == 0, recovered.stderr
    assert not [
        path
        for path in fixture.wheelhouse_root.iterdir()
        if path.name.startswith(".prefetch") or path.name.startswith(".sidecar-runtime.")
    ]


def _create_fixture(tmp_path: Path, *, blocked_export: bool = False) -> Fixture:
    host_python, host_version = _host_arm64_python(tmp_path)
    repo = tmp_path / "repo"
    scripts = repo / "parsing-core-app/scripts"
    scripts.mkdir(parents=True)
    package = repo / "src/parsing_core"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("VALUE = 'v1'\n", encoding="utf-8")
    (package / "data.json").write_text('{"fixture":true}\n', encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "parsing-core"
version = "9.8.7"
requires-python = ">=3.12,<3.13"
dependencies = ["jsonschema>=4.23,<5"]

[project.optional-dependencies]
serve = ["fastapi>=0.115"]

[project.scripts]
parsing-core = "parsing_core.cli:main"
""",
        encoding="utf-8",
    )
    lock = repo / "uv.lock"
    lock.write_text("version = 1\nrevision = 1\n", encoding="utf-8")
    shutil.copy2(RUNTIME_HELPER, scripts / RUNTIME_HELPER.name)

    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir(mode=0o700)
    wheel_staging = tmp_path / "wheel-staging"
    wheel_staging.mkdir()
    wheels = [
        _build_fixture_wheel(wheel_staging, "fastapi", "0.115.0"),
        _build_fixture_wheel(wheel_staging, "jsonschema", "4.23.0"),
    ]
    requirements = "".join(
        f"{name.split('-', 1)[0]}=={name.split('-', 2)[1]} --hash=sha256:{digest}\n"
        for name, digest, _size in sorted(wheels)
    ).encode()
    requirements_sha256 = hashlib.sha256(requirements).hexdigest()
    wheelhouse = wheelhouse_root / requirements_sha256
    wheelhouse.mkdir(mode=0o700)
    for filename, _digest_value, _size in wheels:
        shutil.copy2(wheel_staging / filename, wheelhouse / filename)
        (wheelhouse / filename).chmod(0o400)
    manifest_document = {
        "files": [
            {"name": filename, "sha256": digest, "size": size}
            for filename, digest, size in sorted(wheels)
        ],
        "lock_sha256": _digest(lock),
        "requirements_sha256": requirements_sha256,
        "schema": 1,
    }
    manifest = wheelhouse / ".wheelhouse-manifest.json"
    manifest.write_text(
        json.dumps(manifest_document, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o400)
    requirements_fixture = tmp_path / "requirements-serve.txt"
    requirements_fixture.write_bytes(requirements)

    payload = tmp_path / "payload/python"
    (payload / "bin").mkdir(parents=True)
    (payload / "lib/python3.12/ctypes/macholib").mkdir(parents=True)
    (payload / "lib/python3.12/site-packages/markitdown").mkdir(parents=True)
    shutil.copy2(host_python, payload / "bin/python3.12")
    (payload / "bin/python3.12").chmod(0o775)
    (payload / "bin/python").symlink_to("python3.12")
    (payload / "bin/python3").symlink_to("python3.12")
    (payload / "lib/python3.12/os.py").write_text("# fixture stdlib\n", encoding="utf-8")
    (payload / "lib/python3.12/ctypes/macholib/dyld.py").write_text(
        'paths = ["/usr/local/bin", "/Library/Frameworks"]\n', encoding="utf-8"
    )
    (payload / "lib/python3.12/site-packages/markitdown/_markitdown.py").write_text(
        'paths = ["/opt/homebrew/bin", "/usr/bin"]\n', encoding="utf-8"
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / f"cpython-{host_version}+20260510-aarch64-apple-darwin-install_only.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(payload, arcname="python", recursive=True)
    archive_checksum = _digest(archive)

    export_log = tmp_path / "uv-export.log"
    ready = tmp_path / "uv-ready"
    gate = tmp_path / "uv-gate"
    if blocked_export:
        os.mkfifo(gate, 0o600)
    uv = tmp_path / "tools/uv"
    uv.parent.mkdir()
    block = (
        f": > {shlex.quote(str(ready))}\nIFS= read -r _ < {shlex.quote(str(gate))}\n"
        if blocked_export
        else ""
    )
    uv.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "if [[ ${1:-} == --version ]]; then printf 'uv 0.12.3 fixture\\n'; exit 0; fi\n"
        "command=${1:-}\n"
        "shift || true\n"
        "if [[ $command != export ]]; then exit 64; fi\n"
        f"printf 'export\\n' >> {shlex.quote(str(export_log))}\n"
        f"{block}"
        "output=''\n"
        "while (( $# )); do\n"
        "  if [[ $1 == --output-file ]]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "[[ -n $output ]]\n"
        f'/bin/cp {shlex.quote(str(requirements_fixture))} "$output"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)

    prepare = scripts / PREPARE.name
    prepare_source = PREPARE.read_text(encoding="utf-8")
    prepare_source = _replace_once(
        prepare_source,
        'readonly PYTHON_VERSION="3.12.13"',
        f'readonly PYTHON_VERSION="{host_version}"',
    )
    prepare_source = _replace_once(
        prepare_source,
        f'readonly PYTHON_SHA256="{ARCHIVE_SHA256}"',
        f'readonly PYTHON_SHA256="{archive_checksum}"',
    )
    prepare_source = _replace_once(
        prepare_source,
        'readonly CACHE_DIR="${HOME}/Library/Caches/PDF2MD-build"',
        f"readonly CACHE_DIR={shlex.quote(str(cache))}",
    )
    prepare.write_text(prepare_source, encoding="utf-8")
    prepare.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": str(tmp_path / "attacker-path"),
            "PDF2MD_MACHINE": "x86_64",
            "PDF2MD_RUNTIME_LOCKED": "forged",
            "PDF2MD_TEST_PYTHON_SHA256": "0" * 64,
            "PDF2MD_UV_BIN": str(uv),
            "PDF2MD_WHEELHOUSE_ROOT": str(wheelhouse_root),
            "PIP_INDEX_URL": "https://attacker.invalid/simple",
            "UV_INDEX_URL": "https://attacker.invalid/simple",
        }
    )
    return Fixture(
        repo=repo,
        prepare=prepare,
        uv=uv,
        target=repo / "parsing-core-app/src-tauri/sidecar-runtime",
        export_log=export_log,
        source=source,
        wheelhouse_root=wheelhouse_root,
        environment=environment,
    )


def _run(
    fixture: Fixture,
    *,
    uv: Path | None = None,
    deny_network: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = dict(fixture.environment)
    if uv is not None:
        environment["PDF2MD_UV_BIN"] = str(uv)
    command = ["/bin/bash", str(fixture.prepare)]
    if deny_network:
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            *command,
        ]
    return subprocess.run(
        command,
        cwd=fixture.repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_prepare_builds_runtime_without_pep517_and_publishes_launcher(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)

    result = _run(fixture, deny_network=True)

    assert result.returncode == 0, result.stderr
    assert fixture.target.joinpath("python/bin/python3.12").stat().st_mode & 0o777 == 0o755
    package = fixture.target / "python/lib/python3.12/site-packages/parsing_core"
    assert (package / "__init__.py").read_text(encoding="utf-8") == "VALUE = 'v1'\n"
    metadata = fixture.target / "python/lib/python3.12/site-packages/parsing_core-9.8.7.dist-info"
    assert "Name: parsing-core" in (metadata / "METADATA").read_text(encoding="utf-8")
    assert "Requires-Dist: jsonschema>=4.23,<5" in (metadata / "METADATA").read_text(
        encoding="utf-8"
    )
    assert 'Requires-Dist: fastapi>=0.115 ; extra == "serve"' in (metadata / "METADATA").read_text(
        encoding="utf-8"
    )
    assert (fixture.target / ".runtime-manifest.json").is_file()
    assert (fixture.target / ".runtime-stamp.json").is_file()
    launcher = fixture.repo / "parsing-core-app/src-tauri/binaries/python3"
    assert launcher.stat().st_mode & 0o111
    assert launcher.read_bytes().startswith(b"#!/bin/bash -p\n")
    assert 'export PATH="/usr/bin:/bin"' in launcher.read_text(encoding="utf-8")
    assert "parsing_core.serving.lifecycle" in launcher.read_text(encoding="utf-8")
    assert (launcher.parent / "python3-aarch64-apple-darwin").readlink() == Path("python3")
    assert fixture.export_log.read_text(encoding="utf-8").splitlines() == ["export"]
    assert (
        fixture.target / "python/lib/python3.12/site-packages/fastapi-0.115.0.dist-info"
    ).is_dir()
    assert (
        fixture.target / "python/lib/python3.12/site-packages/jsonschema-4.23.0.dist-info"
    ).is_dir()
    stamp = json.loads((fixture.target / ".runtime-stamp.json").read_text(encoding="utf-8"))
    assert stamp["wheelhouse_manifest_sha256"]
    assert stamp["requirements_sha256"]


def test_prepare_reuses_valid_runtime_and_source_change_forces_rebuild(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)
    first = _run(fixture)
    assert first.returncode == 0, first.stderr
    second = _run(fixture)
    assert second.returncode == 0, second.stderr
    assert fixture.export_log.read_text(encoding="utf-8").splitlines() == ["export"]

    fixture.source.write_text("VALUE = 'v2'\n", encoding="utf-8")
    changed = _run(fixture)

    assert changed.returncode == 0, changed.stderr
    assert fixture.export_log.read_text(encoding="utf-8").splitlines() == ["export", "export"]
    installed = fixture.target / "python/lib/python3.12/site-packages/parsing_core/__init__.py"
    assert installed.read_text(encoding="utf-8") == "VALUE = 'v2'\n"


def test_prepare_rejects_tampered_content_addressed_wheelhouse(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)
    first = _run(fixture, deny_network=True)
    assert first.returncode == 0, first.stderr
    wheel = next(fixture.wheelhouse_root.glob("*/*.whl"))
    wheel.chmod(0o600)
    tampered = bytearray(wheel.read_bytes())
    tampered[-1] ^= 0x01
    wheel.write_bytes(tampered)
    wheel.chmod(0o400)
    fixture.source.write_text("VALUE = 'forces-rebuild'\n", encoding="utf-8")

    result = _run(fixture, deny_network=True)

    assert result.returncode != 0
    assert "wheelhouse file checksum mismatch" in result.stderr
    installed = fixture.target / "python/lib/python3.12/site-packages/parsing_core/__init__.py"
    assert installed.read_text(encoding="utf-8") == "VALUE = 'v1'\n"


def test_prepare_tampered_mode_or_symlink_forces_rebuild(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)
    first = _run(fixture)
    assert first.returncode == 0, first.stderr
    runtime_python = fixture.target / "python/bin/python3.12"
    runtime_python.chmod(0o700)

    repaired_mode = _run(fixture)

    assert repaired_mode.returncode == 0, repaired_mode.stderr
    assert runtime_python.stat().st_mode & 0o777 == 0o755
    link = fixture.target / "python/bin/python3"
    link.unlink()
    link.symlink_to("missing")
    repaired_link = _run(fixture)
    assert repaired_link.returncode == 0, repaired_link.stderr
    assert link.readlink() == Path("python3.12")
    manifest = fixture.target / ".runtime-manifest.json"
    manifest.chmod(0o644)
    repaired_manifest = _run(fixture)
    assert repaired_manifest.returncode == 0, repaired_manifest.stderr
    assert manifest.stat().st_mode & 0o777 == 0o444
    assert fixture.export_log.read_text(encoding="utf-8").splitlines() == [
        "export",
        "export",
        "export",
        "export",
    ]


def test_prepare_failure_preserves_existing_runtime(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)
    first = _run(fixture)
    assert first.returncode == 0, first.stderr
    marker = fixture.target / "preserve-me"
    marker.write_text("old", encoding="utf-8")
    failing_uv = tmp_path / "tools/failing-uv"
    failing_uv.write_text(
        "#!/bin/bash\n"
        "if [[ ${1:-} == --version ]]; then printf 'uv 0.12.3 fixture\\n'; exit 0; fi\n"
        "exit 73\n",
        encoding="utf-8",
    )
    failing_uv.chmod(0o755)
    fixture.source.write_text("VALUE = 'requires-rebuild'\n", encoding="utf-8")

    failed = _run(fixture, uv=failing_uv)

    assert failed.returncode != 0
    assert marker.read_text(encoding="utf-8") == "old"


def test_prepare_cleanup_validation_failure_preserves_protected_staging(
    tmp_path: Path,
) -> None:
    fixture = _create_fixture(tmp_path)
    helper_path = fixture.repo / "parsing-core-app/scripts/sidecar_runtime.py"
    helper_source = helper_path.read_text(encoding="utf-8")
    helper_path.write_text(
        _replace_once(
            helper_source,
            "            atomic_install(\n",
            "            target_parent.chmod(0o777)\n            atomic_install(\n",
        ),
        encoding="utf-8",
    )

    result = _run(fixture)
    target_parent = fixture.target.parent
    target_parent.chmod(0o755)

    assert result.returncode != 0
    assert "cleanup root owner/mode is not trusted" in result.stderr
    staged = list(target_parent.glob(".sidecar-runtime.staged.*"))
    tool_sandboxes = list(target_parent.glob(".sidecar-tools.*"))
    assert len(staged) == 1
    assert len(tool_sandboxes) == 1
    transaction_directory = target_parent / ".sidecar-runtime-transactions"
    assert not list(transaction_directory.glob("*.finished"))


def test_concurrent_prepare_builds_runtime_once(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path, blocked_export=True)
    ready = tmp_path / "uv-ready"
    gate = tmp_path / "uv-gate"
    processes = [
        subprocess.Popen(
            ["/bin/bash", str(fixture.prepare)],
            cwd=fixture.repo,
            env=fixture.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            break
        time.sleep(0.01)
    assert ready.exists(), [process.communicate(timeout=1) for process in processes]
    with gate.open("w", encoding="utf-8") as release:
        release.write("continue\n")
    results = [process.communicate(timeout=30) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert fixture.export_log.read_text(encoding="utf-8").splitlines() == ["export"]


def test_prepare_requires_explicit_absolute_uv_path(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)
    missing = dict(fixture.environment)
    missing.pop("PDF2MD_UV_BIN")
    result = subprocess.run(
        ["/bin/bash", str(fixture.prepare)],
        cwd=fixture.repo,
        env=missing,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert "PDF2MD_UV_BIN" in result.stderr

    relative = dict(fixture.environment)
    relative["PDF2MD_UV_BIN"] = "uv"
    result = subprocess.run(
        ["/bin/bash", str(fixture.prepare)],
        cwd=fixture.repo,
        env=relative,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert "absolute" in result.stderr


def test_prepare_requires_explicit_absolute_wheelhouse_root(tmp_path: Path) -> None:
    fixture = _create_fixture(tmp_path)
    missing = dict(fixture.environment)
    missing.pop("PDF2MD_WHEELHOUSE_ROOT")
    result = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            "/bin/bash",
            str(fixture.prepare),
        ],
        cwd=fixture.repo,
        env=missing,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert "PDF2MD_WHEELHOUSE_ROOT" in result.stderr

    relative = dict(fixture.environment)
    relative["PDF2MD_WHEELHOUSE_ROOT"] = "wheelhouse"
    result = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            "/bin/bash",
            str(fixture.prepare),
        ],
        cwd=fixture.repo,
        env=relative,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert "absolute" in result.stderr


def test_production_constants_remain_pinned() -> None:
    source = PREPARE.read_text(encoding="utf-8")

    assert PREPARE.read_bytes().startswith(b"#!/bin/bash -p\n")
    assert RUNTIME_HELPER.read_bytes().startswith(b"#!/usr/bin/python3 -I\n")
    assert 'readonly PYTHON_VERSION="3.12.13"' in source
    assert f'readonly PYTHON_SHA256="{ARCHIVE_SHA256}"' in source
    assert 'readonly UV_VERSION="0.12.3"' in source
    assert "PDF2MD_TEST_PYTHON_SHA256" not in source
    assert "PDF2MD_MACHINE" not in source
    assert "PDF2MD_RUNTIME_LOCKED" not in source
    assert "PDF2MD_WHEELHOUSE_ROOT" in source
    assert "uv build" not in source
    assert platform.machine() == "arm64"


def test_fat_header_slices_are_parsed_and_bounded() -> None:
    helper = _load_runtime_helper()

    def fat_header(*entries: tuple[int, int, int]) -> bytes:
        header = b"\xca\xfe\xba\xbe" + len(entries).to_bytes(4, "big")
        for cpu_type, offset, size in entries:
            header += cpu_type.to_bytes(4, "big") + b"\x00" * 4
            header += offset.to_bytes(4, "big") + size.to_bytes(4, "big") + b"\x00" * 4
        return header

    header = fat_header((0x0100000C, 0, 4096), (0x01000007, 4096, 4096))
    slices = helper._fat_slices(header, total_size=8192)
    assert [entry.cpu_type for entry in slices] == [0x0100000C, 0x01000007]
    assert slices[1].offset == 4096 and slices[1].size == 4096
    assert helper._fat_slices(b"\xcf\xfa\xed\xfe" + b"\x00" * 8, total_size=16) is None
    with pytest.raises(ValueError, match="architecture table"):
        helper._fat_slices(b"\xca\xfe\xba\xbe" + (2).to_bytes(4, "big"), total_size=8)
    with pytest.raises(ValueError, match="slice bounds"):
        helper._fat_slices(fat_header((0x0100000C, 0, 9999)), total_size=16)


def test_thin_universal_runtime_binaries_extracts_arm64_slice(tmp_path: Path) -> None:
    helper = _load_runtime_helper()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = tmp_path / "shim.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    slices = {}
    for architecture in ("arm64", "x86_64"):
        output = tmp_path / f"{architecture}.bin"
        subprocess.run(
            ["/usr/bin/clang", "-arch", architecture, "-O2", "-o", str(output), str(source)],
            capture_output=True,
            text=True,
            check=True,
        )
        slices[architecture] = output
    universal = runtime / "universal.so"
    subprocess.run(
        [
            "/usr/bin/lipo",
            "-create",
            str(slices["arm64"]),
            str(slices["x86_64"]),
            "-output",
            str(universal),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    universal.chmod(0o755)
    arm64_only = runtime / "arm64-only.so"
    shutil.copy2(slices["arm64"], arm64_only)
    arm64_only.chmod(0o755)

    helper.thin_universal_runtime_binaries(runtime)

    def architectures(path: Path) -> list[str]:
        result = subprocess.run(
            ["/usr/bin/lipo", "-archs", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.split()

    assert architectures(universal) == ["arm64"]
    assert stat.S_IMODE(universal.stat().st_mode) & 0o111
    assert architectures(arm64_only) == ["arm64"]
    assert list(runtime.glob(".*.thin.*")) == []


@pytest.mark.skipif(
    os.environ.get("PDF2MD_RUN_REAL_RUNTIME_BUILD") != "1",
    reason="set PDF2MD_RUN_REAL_RUNTIME_BUILD=1 to run the production runtime build",
)
def test_real_production_runtime_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    app_scripts = repo / "parsing-core-app/scripts"
    app_scripts.mkdir(parents=True)
    shutil.copy2(REPO / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy2(REPO / "uv.lock", repo / "uv.lock")
    shutil.copytree(REPO / "src", repo / "src", symlinks=True)
    shutil.copy2(PREPARE, app_scripts / PREPARE.name)
    shutil.copy2(RUNTIME_HELPER, app_scripts / RUNTIME_HELPER.name)
    uv = shutil.which("uv")
    assert uv is not None
    wheelhouse_root = os.environ.get("PDF2MD_WHEELHOUSE_ROOT")
    if not wheelhouse_root:
        pytest.skip("PDF2MD_WHEELHOUSE_ROOT must point to the prefetched locked wheelhouse")
    environment = os.environ.copy()
    environment["PDF2MD_UV_BIN"] = str(Path(uv).resolve(strict=True))
    environment["PDF2MD_WHEELHOUSE_ROOT"] = wheelhouse_root
    with (REPO / "pyproject.toml").open("rb") as stream:
        expected_version = tomllib.load(stream)["project"]["version"]

    result = subprocess.run(
        [str(app_scripts / PREPARE.name)],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=1200,
    )

    assert result.returncode == 0, result.stderr
    runtime = repo / "parsing-core-app/src-tauri/sidecar-runtime/python"
    smoke = subprocess.run(
        [
            str(runtime / "bin/python3"),
            "-I",
            "-B",
            "-c",
            (
                "import importlib.metadata; import fastapi; import markitdown; "
                "import parsing_core; "
                f"assert importlib.metadata.version('parsing-core') == {expected_version!r}"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert smoke.returncode == 0, smoke.stderr
