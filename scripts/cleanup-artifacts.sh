#!/usr/bin/env bash
# Remove build artifacts older than ARTIFACT_MAX_AGE_DAYS (default: 7).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

DAYS="${ARTIFACT_MAX_AGE_DAYS:-7}"
removed=0

_prune() {
  local path="$1"
  if [[ -e "$path" ]]; then
    rm -rf "$path"
    echo "removed: $path"
    removed=$((removed + 1))
  fi
}

while IFS= read -r -d '' path; do
  _prune "$path"
done < <(
  find . \
    \( -path './.git' -o -path './.git/*' \
       -o -path './.venv' -o -path './.venv/*' \
       -o -path './venv' -o -path './venv/*' \
       -o -path './.cursor' -o -path './.cursor/*' \) -prune \
    -o -type f \
    \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) \
    -mtime +"$DAYS" -print0 2>/dev/null
)

while IFS= read -r -d '' path; do
  _prune "$path"
done < <(
  find . \
    \( -path './.git' -o -path './.git/*' \
       -o -path './.venv' -o -path './.venv/*' \
       -o -path './venv' -o -path './venv/*' \
       -o -path './.cursor' -o -path './.cursor/*' \) -prune \
    -o -type d \
    \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' \
       -o -name '.ruff_cache' -o -name 'build' -o -name 'dist' \) \
    -mtime +"$DAYS" -print0 2>/dev/null
)

while IFS= read -r -d '' path; do
  _prune "$path"
done < <(
  find . \
    \( -path './.git' -o -path './.git/*' \
       -o -path './.venv' -o -path './.venv/*' \
       -o -path './venv' -o -path './venv/*' \
       -o -path './.cursor' -o -path './.cursor/*' \) -prune \
    -o -type d -name '*.egg-info' \
    -mtime +"$DAYS" -print0 2>/dev/null
)

if [[ "$removed" -eq 0 ]]; then
  echo "no build artifacts older than ${DAYS}d"
fi
