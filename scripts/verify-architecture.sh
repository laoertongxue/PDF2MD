#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

architecture_packages=(
  src/parsing_core/workbench/domain
  src/parsing_core/workbench/application
  src/parsing_core/workbench/ports
)

for package in "${architecture_packages[@]}"; do
  if [[ ! -d "$package" ]]; then
    printf 'Missing architecture package directory: %s\n' "$package" >&2
    exit 1
  fi
  if [[ ! -f "$package/__init__.py" ]]; then
    printf 'Missing architecture package boundary: %s/__init__.py\n' "$package" >&2
    exit 1
  fi
done

UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pdf2md-uv-cache}" \
  uv run pytest tests/test_architecture.py -q

UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pdf2md-uv-cache}" \
  uv run mypy --strict "${architecture_packages[@]}"
