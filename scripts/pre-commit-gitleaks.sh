#!/usr/bin/env bash
# Install: cp scripts/pre-commit-gitleaks.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
set -euo pipefail
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks not found. Install: brew install gitleaks" >&2
  exit 1
fi
exec gitleaks protect --staged --verbose
