#!/usr/bin/env bash
# Encrypt .env and move DOTENV_PRIVATE_KEY into Bitwarden (recommended custody).
# Usage: scripts/env-encrypt.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v dotenvx >/dev/null 2>&1; then
  echo "dotenvx not found. Install: https://dotenvx.com" >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env — copy from .env.example and fill values first." >&2
  exit 1
fi

dotenvx encrypt
echo
echo "Next: store the private key in Bitwarden (removes it from .env.keys):"
echo "  bw login          # if needed"
echo "  dotenvx bitwarden up"
echo
echo "Then run:  dotenvx run -- python main.py"
echo "Docs: docs/dotenvx.md"
