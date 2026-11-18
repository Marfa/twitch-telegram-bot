#!/usr/bin/env bash
# Install (required on every clone / PC):
#   cp scripts/pre-commit-gitleaks.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
# Runs gitleaks (pattern) + dotenvx protect (required) + omerta on staged changes.
# Without dotenvx this hook fails closed — do not commit secrets without it.
set -euo pipefail

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks not found. Install: brew install gitleaks" >&2
  exit 1
fi
gitleaks protect --staged --verbose

if ! command -v dotenvx >/dev/null 2>&1; then
  echo "dotenvx not found — refuse commit." >&2
  echo "Install: https://dotenvx.com  (macOS: brew install dotenvx/brew/dotenvx)" >&2
  echo "Without dotenvx you cannot verify .env ciphertext (dotenvx protect)." >&2
  exit 1
fi
dotenvx protect

# Never allow private key files in the index.
if git diff --cached --name-only | grep -E '(^|/)\.env\.keys(\.|$)' >/dev/null 2>&1; then
  echo "Refusing commit: .env.keys must stay out of git (use Bitwarden: dotenvx bitwarden up)." >&2
  exit 1
fi

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
