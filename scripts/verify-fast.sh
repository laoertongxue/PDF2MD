#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pdf2md-uv-cache}"
cd "$ROOT"

uv sync --frozen --all-extras
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen mypy src/parsing_core
uv run --frozen pytest -q --cov=parsing_core --cov-fail-under=85

(
  cd parsing-core-app
  npm ci
  npm run format:check
  npm run lint
  npm run typecheck
  npm test
  npm run build
)

(
  cd parsing-core-app/src-tauri
  cargo fmt --check
  cargo clippy --locked --all-targets -- -D warnings
  cargo test --locked
)
