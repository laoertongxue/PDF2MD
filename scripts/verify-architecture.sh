#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pdf2md-uv-cache}" \
  uv run pytest tests/test_architecture.py -q

mypy_paths=()
for path in src/parsing_core/workbench/domain src/parsing_core/workbench/application; do
  if [[ -d "$path" ]]; then
    mypy_paths+=("$path")
  else
    printf 'Skipping strict mypy: %s does not exist.\n' "$path"
  fi
done

if ((${#mypy_paths[@]})); then
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pdf2md-uv-cache}" \
    uv run mypy --strict "${mypy_paths[@]}"
fi
