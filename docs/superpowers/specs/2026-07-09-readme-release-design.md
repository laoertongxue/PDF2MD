# README and Release Design

## Goal

Rewrite the project README into a public-facing bilingual document and make GitHub Release provide a downloadable desktop client.

The first supported release target is macOS Apple Silicon only. This matches the current proven local build artifact: `PDF2MD_0.1.0_aarch64.dmg`.

## Scope

In scope:

- Replace the current technical README with a Chinese-first, English-second README.
- Keep the README close to the style of `palemoky/chinese-poetry-api`: clear title, concise positioning, badges, feature list, quick start, usage, development, release download, license.
- Add a GitHub Actions workflow that builds the Tauri desktop app when a `v*` tag is pushed.
- Upload the generated macOS Apple Silicon `.dmg` to the GitHub Release.
- Ensure the release build does not ship a sidecar launcher with hard-coded local paths such as `/Users/laoer/Documents/PDF2MD`.

Out of scope for this iteration:

- Windows, Linux, and Intel Mac builds.
- macOS code signing and notarization.
- Automatic changelog generation.
- Checksums.
- A full release-management framework.

## README Structure

The README should use this order:

1. Project title: `PDF2MD`
2. Badges: license, release, platform support.
3. One-sentence positioning in Chinese.
4. Language links: `中文 | English`
5. Chinese section:
   - What it is
   - Who it is for
   - Core features
   - Download desktop app
   - Typical workflow
   - Development setup
   - Build desktop app locally
   - Tech stack
   - Roadmap
   - License
6. English section with the same structure, but shorter.

The copy should describe the current product as an MBA/course intensive-reading desktop app, not as a generic document parser. Multi-format support can be described as a target capability, but the download and current workflow should not overpromise unsupported platforms.

## Release Workflow

Add one workflow file:

`.github/workflows/release.yml`

Trigger:

- `push` tags matching `v*`

Build:

- Runner: `macos-latest`
- Install Node dependencies in `parsing-core-app` with `npm ci`.
- Build the Tauri app with `npm run tauri build`.
- Build only after the Python sidecar launcher has no user-machine absolute paths.

Release asset:

- Upload `parsing-core-app/src-tauri/target/release/bundle/dmg/*.dmg`.
- The uploaded `.dmg` must be the macOS Apple Silicon desktop client, not a source archive or web-only build.

GitHub permissions:

- `contents: write`

Implementation should use a maintained off-the-shelf GitHub Action for release asset upload instead of writing custom GitHub API code.

## Acceptance Criteria

- README has complete Chinese and English sections.
- README clearly links users to GitHub Releases for the desktop app.
- README says the downloadable desktop client currently supports macOS Apple Silicon.
- The release-sidecar launcher does not contain `/Users/laoer/` or other development-machine absolute paths.
- A `v*` tag push can create or update a GitHub Release and attach the `.dmg`.
- Local checks still pass:
  - `cd parsing-core-app && npm run build`
  - `cd parsing-core-app && npm run tauri build`

## Risks

- The current app bundle has used a local Python sidecar script during development. Release work must remove hard-coded local paths before publishing a downloadable client.
- GitHub network access from the local machine was unreliable during the previous push attempt. Implementation may be committed locally before remote push succeeds.
