# Changelog

All notable changes to PDF2MD are documented here.

## [0.1.2] - 2026-07-15

### Added

- MBA course workbench flow for multiple PDF textbooks, chapter detection and chapter confirmation.
- Unattended OCR pipeline: Apple Vision, Codex CLI visual observation, deterministic alignment,
  Baidu escalation for conflict/complex/sample pages, and Codex final adjudication.
- Evidence-bound Markdown intensive-reading notes with source references and directly previewable
  Mermaid diagrams.
- DeepSeek intensive-reading generation fixed to `deepseek-v4-pro`.
- macOS Keychain storage for the DeepSeek API key.
- Tauri macOS Apple Silicon release workflow producing `.dmg`, `.app.zip`, checksums, and
  provenance attestation.
- Release gates for bundled Python runtime, OCR schemas, arm64 binaries, sidecar cold start,
  `/health`, signature verification, and development-path scanning.

### Quality and Safety

- Failed, timed-out, cancelled, incomplete, or schema-invalid runs are blocked from publication.
- Baidu escalation is offline by default and is authorized only for conflict, complex, or sample
  pages bound to the matching page evidence.
- API keys and local paths are redacted from errors and logs.
- The application does not claim a real textbook run completed when Codex CLI is unavailable or
  fails the secure direct-executable check.

### Known Validation Boundary

The real scanned textbook checks verified Apple Vision rendering and OCR caching. On the current
validation machine, `/opt/homebrew/bin/codex` is a symlink and is correctly rejected by the
security gate. Therefore full unattended Codex review, optional Baidu escalation, DeepSeek
generation, and publishable Markdown output still require a secure direct Codex CLI executable.

The public macOS package is ad-hoc signed and not notarized. See the Release notes for installation
and verification instructions.
