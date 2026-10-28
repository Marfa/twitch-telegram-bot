#!/usr/bin/env bash
# Install: cp scripts/pre-commit-gitleaks.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
# Runs gitleaks (pattern) + omerta (your .env values) on staged changes.
set -euo pipefail

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks not found. Install: brew install gitleaks" >&2
  exit 1
fi
gitleaks protect --staged --verbose

run_omerta() {
  if command -v uvx >/dev/null 2>&1; then
    uvx omerta@0.1.2 check
    return
  fi
  if command -v omerta >/dev/null 2>&1; then
    omerta check
    return
  fi
  echo "omerta not found. Install: brew install uv   # then uvx works" >&2
  echo "  or: pipx install omerta==0.1.2" >&2
  exit 1
}
run_omerta
