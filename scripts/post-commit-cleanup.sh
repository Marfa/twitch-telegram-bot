#!/usr/bin/env bash
# Install: cp scripts/post-commit-cleanup.sh .git/hooks/post-commit && chmod +x .git/hooks/post-commit
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
exec "$ROOT/scripts/cleanup-artifacts.sh"
