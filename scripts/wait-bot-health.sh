#!/usr/bin/env bash
# Wait for the bot's /health to answer.
#
# Usage: scripts/wait-bot-health.sh [url] [tries] [interval-seconds]
#   url      default http://127.0.0.1:8080/health
#   tries    default 30
#   interval default 2
#
# Exits 0 on the first healthy answer and 1 when every attempt failed.
set -euo pipefail

url="${1:-http://127.0.0.1:8080/health}"
tries="${2:-30}"
interval="${3:-2}"

for i in $(seq 1 "$tries"); do
  if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
    echo "health ok after $i checks ($url)" >&2
    exit 0
  fi
  sleep "$interval"
done

echo "ERROR: $url never became healthy after $tries checks" >&2
exit 1
