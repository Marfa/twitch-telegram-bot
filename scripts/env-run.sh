#!/usr/bin/env bash
# Run the bot with dotenvx (Bitwarden / .env.keys / DOTENV_PRIVATE_KEY).
# Usage: scripts/env-run.sh [args...]   →  dotenvx run -- python main.py [args...]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v dotenvx >/dev/null 2>&1; then
  echo "dotenvx not found. Install: https://dotenvx.com  (macOS: brew install dotenvx/brew/dotenvx)" >&2
  exit 1
fi

exec dotenvx run -- python main.py "$@"
