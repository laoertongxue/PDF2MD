import stat
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
VERIFY_SCRIPTS = (
    ROOT / "scripts/verify-fast.sh",
    ROOT / "scripts/verify-architecture.sh",
    ROOT / "scripts/verify-security.sh",
)
PYTHON_VERSION = ROOT / ".python-version"
NODE_VERSION = ROOT / ".node-version"
RUST_TOOLCHAIN = ROOT / "rust-toolchain.toml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow_document(workflow: str | None = None) -> dict:
    return yaml.load(workflow or _workflow(), Loader=yaml.BaseLoader)


def _job_steps(job: str, workflow: str | None = None) -> list[dict]:
    return _workflow_document(workflow)["jobs"][job]["steps"]


def _named_step(job: str, name: str, workflow: str | None = None) -> dict:
    return next(step for step in _job_steps(job, workflow) if step.get("name") == name)


def _active_shell_lines(run: str) -> set[str]:
    return {
        line.strip()
        for line in run.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _action_refs(workflow: str) -> list[str]:
    document = _workflow_document(workflow)
    return [
        step["uses"].split("@", 1)[1]
        for job in document["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]


def _release_supply_chain_errors(workflow: str) -> list[str]:
    errors = []
    build_steps = _job_steps("macos-apple-silicon", workflow)
    release_steps = _job_steps("release", workflow)
    verify_run = next(
        (
            step.get("run", "")
            for step in build_steps
            if step.get("name") == "Verify and stage release assets"
        ),
        "",
    )
    dmg_command = './scripts/verify-release-dmg.sh "$dmg" "$VERSION"'
    shell_commands = _active_shell_lines(verify_run)
    if dmg_command not in shell_commands:
        errors.append(f"missing:{dmg_command}")
    if "hdiutil" in verify_run or "mounted_app=" in verify_run:
        errors.append("inline-dmg-lifecycle")

    attest = next(
        (
            index
            for index, step in enumerate(build_steps)
            if step.get("uses", "").startswith("actions/attest@")
        ),
        -1,
    )
    stage = next(
        (
            index
            for index, step in enumerate(build_steps)
            if any(
                "steps.attestation.outputs.bundle-path" in command
                for command in _active_shell_lines(step.get("run", ""))
            )
        ),
        -1,
    )
    upload = next(
        (
            index
            for index, step in enumerate(build_steps)
            if step.get("uses", "").startswith("actions/upload-artifact@")
        ),
        -1,
    )
    if min(attest, stage, upload) < 0 or not attest < stage < upload:
        errors.append("attestation-order")
    elif build_steps[attest].get("id") != "attestation":
        errors.append("attestation-id")

    release_verify = next(
        (
            step.get("run", "")
            for step in release_steps
            if step.get("name") == "Verify downloaded asset set"
        ),
        "",
    )
    release_commands = _active_shell_lines(release_verify)
    verification_command = next(
        (command for command in release_commands if command.startswith("gh attestation verify ")),
        "",
    )
    if 'gh attestation verify "$subject"' not in verification_command:
        errors.append('missing:gh attestation verify "$subject"')
    if '--bundle "$bundle"' not in verification_command:
        errors.append('missing:--bundle "$bundle"')
    return errors


def test_release_uses_native_apple_silicon_runner():
    workflow = _workflow()

    assert "runs-on: macos-15\n" in workflow
    assert "macos-15-xlarge" not in workflow
    assert "macos-14" not in workflow
    assert 'test "$(uname -m)" = "arm64"' in workflow
    assert "rustup target add aarch64-apple-darwin" not in workflow
    assert "--target aarch64-apple-darwin" not in workflow


def test_packaged_sidecar_cold_start_is_between_signature_checks():
    run = _named_step("macos-apple-silicon", "Verify and stage release assets")["run"]
    first_verify = run.index("codesign --verify --deep --strict")
    cold_start = run.index('./scripts/test-release-sidecar.sh "$app"')
    second_verify = run.index("codesign --verify --deep --strict", first_verify + 1)

    assert first_verify < cold_start < second_verify


def test_build_is_attested_and_attestation_bundle_is_uploaded_before_release_job():
    document = _workflow_document()
    build_steps = document["jobs"]["macos-apple-silicon"]["steps"]
    release_steps = document["jobs"]["release"]["steps"]
    attest = next(
        i
        for i, step in enumerate(build_steps)
        if step.get("uses", "").startswith("actions/attest@")
    )
    stage_bundle = next(
        i
        for i, step in enumerate(build_steps)
        if "steps.attestation.outputs.bundle-path" in step.get("run", "")
    )
    upload = next(
        i
        for i, step in enumerate(build_steps)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    download = next(
        i
        for i, step in enumerate(release_steps)
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    publish = next(
        i
        for i, step in enumerate(release_steps)
        if step.get("uses", "").startswith("softprops/action-gh-release@")
    )

    assert attest < stage_bundle < upload
    assert download < publish
    assert build_steps[attest]["id"] == "attestation"
    assert "sigstore.jsonl" in build_steps[stage_bundle]["run"]
    workflow = _workflow()
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "artifact-metadata: write" in workflow
    assert "attestations: read" in workflow
    assert "needs: macos-apple-silicon" in workflow
    assert "github.run_id" in workflow
    assert 'gh attestation verify "$subject"' in workflow
    assert '--bundle "$bundle"' in workflow


def test_release_artifact_contains_dmg_zip_and_checksums():
    workflow = _workflow()

    assert "PDF2MD_${VERSION}_aarch64.dmg" in workflow
    assert "PDF2MD_${VERSION}_aarch64.app.zip" in workflow
    assert "*.sha256" in workflow
    assert "sigstore.jsonl" in workflow
    assert 'has("verificationMaterial")' in workflow
    assert 'has("dsseEnvelope")' in workflow


def test_release_delegates_dmg_mount_and_verification_to_executable_script():
    run = _named_step("macos-apple-silicon", "Verify and stage release assets")["run"]

    assert './scripts/verify-release-dmg.sh "$dmg" "$VERSION"' in run
    assert "hdiutil" not in run
    assert "mounted_app=" not in run
    assert (ROOT / "scripts/verify-release-dmg.sh").stat().st_mode & stat.S_IXUSR


def test_release_supply_chain_policy_rejects_adversarial_workflow_fixtures():
    workflow = _workflow()
    assert _release_supply_chain_errors(workflow) == []

    without_mount = workflow.replace(
        "./scripts/verify-release-dmg.sh", "./scripts/inspect-release-dmg.sh", 1
    )
    assert 'missing:./scripts/verify-release-dmg.sh "$dmg" "$VERSION"' in (
        _release_supply_chain_errors(without_mount)
    )

    commented_mount = workflow.replace(
        '          ./scripts/verify-release-dmg.sh "$dmg" "$VERSION"',
        '          # ./scripts/verify-release-dmg.sh "$dmg" "$VERSION"',
        1,
    )
    assert 'missing:./scripts/verify-release-dmg.sh "$dmg" "$VERSION"' in (
        _release_supply_chain_errors(commented_mount)
    )

    swapped_order = workflow.replace(
        "uses: actions/upload-artifact@", "uses: actions/attest-placeholder@", 1
    )
    swapped_order = swapped_order.replace(
        "uses: actions/attest@", "uses: actions/upload-artifact@", 1
    )
    swapped_order = swapped_order.replace(
        "uses: actions/attest-placeholder@", "uses: actions/attest@", 1
    )
    assert "attestation-order" in _release_supply_chain_errors(swapped_order)

    without_verification = workflow.replace("gh attestation verify", "gh attestation inspect", 1)
    assert 'missing:gh attestation verify "$subject"' in _release_supply_chain_errors(
        without_verification
    )

    commented_verification = workflow.replace(
        '            gh attestation verify "$subject"',
        '            # gh attestation verify "$subject"',
        1,
    )
    assert 'missing:gh attestation verify "$subject"' in _release_supply_chain_errors(
        commented_verification
    )

    commented_bundle_stage = workflow.replace(
        '        run: cp "${{ steps.attestation.outputs.bundle-path }}"',
        '        run: # cp "${{ steps.attestation.outputs.bundle-path }}"',
        1,
    )
    assert "attestation-order" in _release_supply_chain_errors(commented_bundle_stage)


def test_release_passes_locked_to_tauri_cargo_build():
    assert "npm run tauri -- build -- --locked" in _workflow()


def test_release_reclaims_rust_test_artifacts_before_native_release_build():
    steps = _job_steps("macos-apple-silicon")
    quality = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Run repository quality and security gates"
    )
    clean = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Reclaim Rust test artifacts"
    )
    prefetch = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Prefetch locked Python wheels"
    )
    runtime = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Prepare embedded Python runtime"
    )
    native_build = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build desktop app natively"
    )

    assert quality < clean < prefetch < runtime < native_build
    assert steps[clean].get("working-directory") == "parsing-core-app/src-tauri"
    assert _active_shell_lines(steps[clean].get("run", "")) == {"cargo clean"}


def test_release_prefetches_a_locked_content_addressed_wheelhouse():
    step = _named_step("macos-apple-silicon", "Prefetch locked Python wheels")
    run = step.get("run", "")

    assert step.get("shell") == "/bin/bash --noprofile --norc -euo pipefail {0}"
    assert "sidecar_runtime.py prefetch-wheelhouse" in run
    assert '--python-version "3.12.13"' in run
    assert '--uv-version "0.12.3"' in run
    assert '--wheelhouse-root "$PDF2MD_WHEELHOUSE_ROOT"' in run
    assert 'PDF2MD_WHEELHOUSE_ROOT="$RUNNER_TEMP/pdf2md-wheelhouse"' in run
    assert "printf 'PDF2MD_WHEELHOUSE_ROOT=%s\\n'" in run
    assert "printf 'PDF2MD_UV_BIN=%s\\n'" in run
    assert "/usr/bin/python3 -I -S -B" in run
    assert "os.path.realpath" in run


def test_release_runtime_assembly_receives_prefetched_paths_in_a_clean_environment():
    prefetch = _named_step("macos-apple-silicon", "Prefetch locked Python wheels")["run"]
    prepare = _named_step("macos-apple-silicon", "Prepare embedded Python runtime")["run"]

    assert 'PDF2MD_WHEELHOUSE_ROOT="$PDF2MD_WHEELHOUSE_ROOT"' in prepare
    assert 'PDF2MD_UV_BIN="$PDF2MD_UV_BIN"' in prepare
    assert "/usr/bin/env -i" in prepare
    assert "command -v uv" not in prepare
    assert "--wheelhouse-root" in prefetch
    assert "prepare-sidecar-python.sh" in prepare


def test_release_runs_opt_in_checks_against_the_actual_built_app_and_dmg():
    steps = _job_steps("macos-apple-silicon")
    native_build = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build desktop app natively"
    )
    verify = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Verify and stage release assets"
    )
    run = steps[verify].get("run", "")

    assert native_build < verify
    assert "PDF2MD_RUN_REAL_ARTIFACT_TESTS=1" in run
    assert 'PDF2MD_REAL_APP_PATH="$app"' in run
    assert 'PDF2MD_REAL_DMG_PATH="$dmg"' in run
    assert 'PDF2MD_REAL_VERSION="$VERSION"' in run
    assert "test_packaged_pdf2md_artifacts_pass_production_release_gates" in run
    assert "synthetic" not in run.casefold()


def test_release_artifact_pytests_use_the_frozen_uv_environment():
    run = _named_step("macos-apple-silicon", "Verify and stage release assets")["run"]

    assert (
        'PDF2MD_APP_PATH="$app" uv run --frozen pytest -q tests/test_version_consistency.py' in run
    )
    assert "uv run --frozen pytest -q tests/test_release_supply_chain.py" in run
    active = _active_shell_lines(run)
    assert not any(line.startswith("pytest ") for line in active)


def test_ci_uses_locked_toolchains_and_all_repository_gates():
    assert CI_WORKFLOW.is_file()
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert 'python-version: "3.12.13"' in workflow
    assert 'node-version: "24.19.0"' in workflow
    assert "uv sync --frozen --all-extras" in workflow
    assert "./scripts/verify-fast.sh" in workflow
    assert "./scripts/verify-architecture.sh" in workflow
    assert "./scripts/verify-security.sh" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "contents: read" in workflow
    assert "pull_request:" in workflow
    assert "timeout-minutes:" in workflow
    assert "pull_request_target" not in workflow


def test_repository_toolchain_versions_are_exact_and_match_ci_and_release():
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    release = _workflow()

    assert PYTHON_VERSION.read_text(encoding="utf-8").strip() == "3.12.13"
    assert NODE_VERSION.read_text(encoding="utf-8").strip() == "24.19.0"
    rust = RUST_TOOLCHAIN.read_text(encoding="utf-8")
    assert 'channel = "1.97.1"' in rust

    for workflow in (ci, release):
        assert 'python-version: "3.12.13"' in workflow
        assert 'node-version: "24.19.0"' in workflow


def test_all_workflow_actions_are_pinned_to_full_commit_shas():
    workflows = [WORKFLOW, CI_WORKFLOW]

    for path in workflows:
        refs = _action_refs(path.read_text(encoding="utf-8"))
        assert refs, f"{path.name} must use at least one action"
        assert all(len(ref) == 40 for ref in refs), (path, refs)
        assert all(set(ref) <= set("0123456789abcdef") for ref in refs), (path, refs)


def test_verification_scripts_are_executable_and_cover_required_gates():
    for path in VERIFY_SCRIPTS:
        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR

    fast = VERIFY_SCRIPTS[0].read_text(encoding="utf-8")
    assert "uv sync --frozen --all-extras" in fast
    for gate in (
        "ruff format --check",
        "ruff check",
        "mypy src/parsing_core",
        "pytest -q --cov=parsing_core --cov-fail-under=85",
        "npm ci",
        "npm run format:check",
        "npm run lint",
        "npm run typecheck",
        "npm test",
        "npm run build",
        "cargo fmt --check",
        "cargo clippy --locked --all-targets -- -D warnings",
        "cargo test --locked",
    ):
        assert gate in fast

    security = VERIFY_SCRIPTS[2].read_text(encoding="utf-8")
    assert '"$npm_bin" audit --audit-level=moderate' in security
    assert "tests/test_security.py" in security
    assert "tests/test_serving/test_api_ws_network.py" in security
    assert 'readonly SYSTEM_GIT="/usr/bin/git"' in security
    assert 'readonly SYSTEM_PYTHON="/usr/bin/python3"' in security
    assert '[git_binary, "-C", str(root), *arguments]' in security
    assert "command -v" not in security
    assert "git grep -nE" not in security


def test_release_uses_locked_python_environment_and_shared_verification():
    workflows = (_workflow(), CI_WORKFLOW.read_text(encoding="utf-8"))

    for workflow in workflows:
        assert "astral-sh/setup-uv@" in workflow
        assert 'version: "0.12.3"' in workflow
        assert "uv sync --frozen --all-extras" in workflow
        assert "./scripts/verify-fast.sh" in workflow
        assert "./scripts/verify-architecture.sh" in workflow
        assert "./scripts/verify-security.sh" in workflow
