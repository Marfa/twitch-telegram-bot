#!/usr/bin/env bash
# Remove build artifacts older than ARTIFACT_MAX_AGE_DAYS (default: 7).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

DAYS="${ARTIFACT_MAX_AGE_DAYS:-7}"
# find -mtime uses whole days (floor); -mmin is exact "older than N days".
MINS=$((DAYS * 24 * 60))
removed=0

_prune() {
  local path="$1"
  if [[ -e "$path" ]]; then
    rm -rf "$path"
    echo "removed: $path"
    removed=$((removed + 1))
  fi
}

_find_prune_args=(
  \( -path './.git' -o -path './.git/*'
     -o -path './.venv' -o -path './.venv/*'
     -o -path './venv' -o -path './venv/*'
     -o -path './.cursor' -o -path './.cursor/*'
     -o -path './data' -o -path './data/*' \) -prune
)

while IFS= read -r -d '' path; do
  _prune "$path"
done < <(
  find . "${_find_prune_args[@]}" \
    -o -type f \
    \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) \
    -mmin +"$MINS" -print0 2>/dev/null
)

while IFS= read -r -d '' path; do
  _prune "$path"
done < <(
  find . "${_find_prune_args[@]}" \
    -o -type d \
    \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' \
       -o -name '.ruff_cache' -o -name 'build' -o -name 'dist' \) \
    -mmin +"$MINS" -print0 2>/dev/null
)

while IFS= read -r -d '' path; do
  _prune "$path"
done < <(
  find . "${_find_prune_args[@]}" \
    -o -type d -name '*.egg-info' \
    -mmin +"$MINS" -print0 2>/dev/null
)

# Drop empty __pycache__ left after deleting old .pyc
while IFS= read -r -d '' path; do
  if [[ -d "$path" ]] && [[ -z "$(find "$path" -type f 2>/dev/null | head -1)" ]]; then
    _prune "$path"
  fi
done < <(
  find . "${_find_prune_args[@]}" \
    -o -type d -name '__pycache__' -print0 2>/dev/null
)

if [[ "$removed" -eq 0 ]]; then
  echo "no build artifacts older than ${DAYS}d"
fi
