import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREPARE = REPO / "parsing-core-app/scripts/prepare-sidecar-python.sh"
RUNTIME_HELPER = REPO / "parsing-core-app/scripts/sidecar_runtime.py"
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


def _archive(path: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            data = None if not member.isfile() else io.BytesIO(b"runtime")
            if data is not None:
                member.size = len(data.getvalue())
            archive.addfile(member, data)


def _validate_archive(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(RUNTIME_HELPER), "validate-archive", str(path)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def _acquire_lock(lock: Path, token: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(RUNTIME_HELPER), "acquire-lock", str(lock), token],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def _isolated_prepare(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    scripts = repo / "parsing-core-app/scripts"
    scripts.mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='fixture'\ndependencies=['fixture-dependency>=1']\n",
        encoding="utf-8",
    )
    (scripts / PREPARE.name).write_bytes(PREPARE.read_bytes())
    (scripts / RUNTIME_HELPER.name).write_bytes(RUNTIME_HELPER.read_bytes())

    payload = tmp_path / "payload/python"
    (payload / "bin").mkdir(parents=True)
    (payload / "lib/python3.13/ctypes/macholib").mkdir(parents=True)
    (payload / "lib/python3.13/site-packages/markitdown").mkdir(parents=True)
    fake_python = payload / "bin/python3.13"
    fake_python.write_text(
        textwrap.dedent(
            """\
            #!/bin/bash
            if [[ ${1:-} == -c ]]; then printf '3.13.13\\n'; exit 0; fi
            if [[ ${1:-} == -m && ${2:-} == pip ]]; then
              printf 'install\\n' >> "$PDF2MD_TEST_INSTALL_LOG"
              sleep "${PDF2MD_TEST_INSTALL_DELAY:-0}"
              [[ ${PDF2MD_TEST_PIP_FAIL:-0} != 1 ]]
              exit
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (payload / "bin/python").symlink_to("python3.13")
    (payload / "bin/python3").symlink_to("python3.13")
    (payload / "lib/python3.13/os.py").write_text("", encoding="utf-8")
    (payload / "lib/python3.13/ctypes/macholib/dyld.py").write_text("", encoding="utf-8")
    (payload / "lib/python3.13/site-packages/markitdown/_markitdown.py").write_text(
        "", encoding="utf-8"
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / ARCHIVE_NAME
    with tarfile.open(archive, "w:gz") as output:
        output.add(payload, arcname="python", recursive=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_file = fake_bin / "file"
    fake_file.write_text(
        '#!/bin/bash\necho "$1: Mach-O 64-bit executable arm64"\n', encoding="utf-8"
    )
    fake_file.chmod(0o755)
    install_log = tmp_path / "installs.log"
    env = {
        "PDF2MD_BUILD_CACHE": str(cache),
        "PDF2MD_MACHINE": "arm64",
        "PDF2MD_TEST_PYTHON_SHA256": _digest(archive),
        "PDF2MD_TEST_INSTALL_LOG": str(install_log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return scripts / PREPARE.name, env, install_log


def test_prepare_supports_x86_64_cross_packaging(tmp_path):
    prepare, env, install_log = _isolated_prepare(tmp_path)
    fake_host_python = tmp_path / "bin/python3"
    fake_host_python.write_text(
        "#!/bin/bash\n"
        'printf \'CALL %s\\n\' "$*" >> "$PDF2MD_TEST_INSTALL_LOG"\n'
        "if [[ ${1:-} == -m && ${2:-} == pip ]]; then\n"
        "  exit 0\n"
        "fi\n"
        f'exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    fake_host_python.chmod(0o755)

    result = subprocess.run(
        ["bash", str(prepare)],
        cwd=prepare.parents[2],
        env={**os.environ, **env, "PDF2MD_MACHINE": "x86_64"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    install = install_log.read_text(encoding="utf-8")
    assert "--platform macosx_11_0_arm64" in install
    assert "--python-version 3.13" in install
    assert "--only-binary=:all:" in install


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
        'cp "$PDF2MD_TEST_ARCHIVE_SOURCE" "$output"\n',
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


def test_archive_validation_accepts_contained_symlink(tmp_path):
    archive = tmp_path / "runtime.tar.gz"
    directory = tarfile.TarInfo("python/bin")
    directory.type = tarfile.DIRTYPE
    executable = tarfile.TarInfo("python/bin/python3.13")
    link = tarfile.TarInfo("python/bin/python3")
    link.type = tarfile.SYMTYPE
    link.linkname = "python3.13"
    _archive(archive, [directory, executable, link])

    result = _validate_archive(archive)

    assert result.returncode == 0, result.stderr


def test_archive_validation_rejects_unsafe_members(tmp_path):
    unsafe_members = []

    traversal = tarfile.TarInfo("../../outside")
    traversal.size = 0
    unsafe_members.append(traversal)

    absolute = tarfile.TarInfo("/tmp/outside")
    absolute.size = 0
    unsafe_members.append(absolute)

    symlink = tarfile.TarInfo("python/bin/python3")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "../../../outside"
    unsafe_members.append(symlink)

    hardlink = tarfile.TarInfo("python/bin/python")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "../../outside"
    unsafe_members.append(hardlink)

    fifo = tarfile.TarInfo("python/runtime.pipe")
    fifo.type = tarfile.FIFOTYPE
    unsafe_members.append(fifo)

    for index, member in enumerate(unsafe_members):
        archive = tmp_path / f"unsafe-{index}.tar.gz"
        _archive(archive, [member])
        result = _validate_archive(archive)
        assert result.returncode != 0, member.name
        assert "unsafe archive member" in result.stderr


def test_prepare_failure_preserves_existing_runtime(tmp_path):
    prepare, env, _ = _isolated_prepare(tmp_path)
    target = prepare.parents[1] / "src-tauri/sidecar-runtime"
    target.mkdir(parents=True)
    marker = target / "old-runtime"
    marker.write_text("keep", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(prepare)],
        cwd=prepare.parents[2],
        env={**os.environ, **env, "PDF2MD_TEST_PIP_FAIL": "1"},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "keep"


def test_prepare_rejects_malicious_archive_before_replacing_runtime(tmp_path):
    prepare, env, _ = _isolated_prepare(tmp_path)
    target = prepare.parents[1] / "src-tauri/sidecar-runtime"
    target.mkdir(parents=True)
    marker = target / "old-runtime"
    marker.write_text("keep", encoding="utf-8")
    archive = Path(env["PDF2MD_BUILD_CACHE"]) / ARCHIVE_NAME
    traversal = tarfile.TarInfo("../../outside")
    traversal.size = 0
    _archive(archive, [traversal])
    env["PDF2MD_TEST_PYTHON_SHA256"] = _digest(archive)

    result = subprocess.run(
        ["bash", str(prepare)],
        cwd=prepare.parents[2],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe archive member" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "outside").exists()


def test_prepare_atomically_replaces_existing_runtime(tmp_path):
    prepare, env, _ = _isolated_prepare(tmp_path)
    target = prepare.parents[1] / "src-tauri/sidecar-runtime"
    target.mkdir(parents=True)
    marker = target / "old-runtime"
    marker.write_text("replace", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(prepare)],
        cwd=prepare.parents[2],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (target / "python/bin/python3").exists()


def test_concurrent_prepare_builds_runtime_once(tmp_path):
    prepare, env, install_log = _isolated_prepare(tmp_path)
    process_env = {
        **os.environ,
        **env,
        "PDF2MD_TEST_INSTALL_DELAY": "1",
    }

    processes = [
        subprocess.Popen(
            ["bash", str(prepare)],
            cwd=prepare.parents[2],
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert install_log.read_text(encoding="utf-8").splitlines() == ["install"]


def test_lock_recovers_empty_directory_left_before_owner_metadata(tmp_path):
    lock = tmp_path / "runtime.lock"
    lock.mkdir()
    token = uuid.uuid4().hex

    result = _acquire_lock(lock, token)

    assert result.returncode == 0, result.stderr
    assert json.loads((lock / "owner.json").read_text(encoding="utf-8"))["token"] == token


def test_only_one_waiter_claims_and_replaces_stale_lock(tmp_path):
    for attempt in range(5):
        lock = tmp_path / f"runtime-{attempt}.lock"
        lock.mkdir()
        tokens = [uuid.uuid4().hex for _ in range(8)]

        processes = [
            subprocess.Popen(
                ["python3", str(RUNTIME_HELPER), "acquire-lock", str(lock), token],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for token in tokens
        ]
        results = [process.communicate(timeout=10) for process in processes]

        assert [process.returncode for process in processes].count(0) == 1, results
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
        assert owner["token"] in tokens
        assert not list(tmp_path.glob(f"{lock.name}.claim.*"))


def test_lock_treats_reused_pid_with_different_process_identity_as_stale(tmp_path):
    lock = tmp_path / "runtime.lock"
    lock.mkdir()
    (lock / "owner.json").write_text(
        json.dumps(
            {
                "token": "previous-owner",
                "pid": os.getpid(),
                "process_start": "different-process-instance",
            }
        ),
        encoding="utf-8",
    )
    token = uuid.uuid4().hex

    result = _acquire_lock(lock, token)

    assert result.returncode == 0, result.stderr
    owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
    assert owner["token"] == token


def test_previous_owner_token_cannot_release_replacement_lock(tmp_path):
    lock = tmp_path / "runtime.lock"
    current_token = uuid.uuid4().hex
    acquired = _acquire_lock(lock, current_token)
    assert acquired.returncode == 0, acquired.stderr

    released = subprocess.run(
        [
            "python3",
            str(RUNTIME_HELPER),
            "release-lock",
            str(lock),
            "previous-owner-token",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )

    assert released.returncode == 0, released.stderr
    owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
    assert owner["token"] == current_token
