from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_uses_standard_runner_and_explicit_apple_silicon_target():
    workflow = _workflow()

    assert "runs-on: macos-14\n" in workflow
    assert "macos-14-xlarge" not in workflow
    assert "rustup target add aarch64-apple-darwin" in workflow
    assert "npm run tauri build -- --target aarch64-apple-darwin" in workflow


def test_build_is_uploaded_and_attested_before_release_job():
    workflow = _workflow()
    upload = workflow.index("uses: actions/upload-artifact@")
    attest = workflow.index("uses: actions/attest@")
    release_job = workflow.index("\n  release:\n")
    download = workflow.index("uses: actions/download-artifact@", release_job)
    publish = workflow.index("uses: softprops/action-gh-release@", release_job)

    assert upload < attest < release_job < download < publish
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "needs: macos-apple-silicon" in workflow
    assert "github.run_id" in workflow


def test_release_artifact_contains_dmg_zip_and_checksums():
    workflow = _workflow()

    assert "PDF2MD_${VERSION}_aarch64.dmg" in workflow
    assert "PDF2MD_${VERSION}_aarch64.app.zip" in workflow
    assert "*.sha256" in workflow
