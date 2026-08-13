#!/usr/bin/env bash
# After agent `git commit`, purge build artifacts older than 7 days.
set -euo pipefail
input=$(cat || true)
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if [[ -x "$ROOT/scripts/cleanup-artifacts.sh" ]]; then
  "$ROOT/scripts/cleanup-artifacts.sh" >&2 || true
fi
echo '{}'
