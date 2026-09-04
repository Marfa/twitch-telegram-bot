#!/usr/bin/env bash
# Restore newest local pg dump into Aiven (cold DR mirror). VPS remains primary.
# Reads AIVEN_DATABASE_URL from $APP_DIR/.env — never logs the URL.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/twitch-telegram-bot}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
PG_IMAGE="${PG_IMAGE:-postgres:18-alpine}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip: no $ENV_FILE"
  exit 0
fi

# Single KEY=value line; strip optional quotes. Do not source the whole .env.
AIVEN_DATABASE_URL="$(
  grep -E '^[[:space:]]*AIVEN_DATABASE_URL=' "$ENV_FILE" \
    | tail -n1 \
    | sed -e 's/^[[:space:]]*AIVEN_DATABASE_URL=//' -e 's/^["'\'']//' -e 's/["'\'']$//' \
    | tr -d '\r'
)"
AIVEN_DATABASE_URL="${AIVEN_DATABASE_URL:-}"

if [[ -z "$AIVEN_DATABASE_URL" ]]; then
  echo "skip: AIVEN_DATABASE_URL unset"
  exit 0
fi

mapfile -t dumps < <(ls -1t "$BACKUP_DIR"/bot-*.sql.gz 2>/dev/null || true)
if ((${#dumps[@]} == 0)); then
  echo "error: no dumps in $BACKUP_DIR" >&2
  exit 1
fi
LATEST="${dumps[0]}"

gunzip -c "$LATEST" \
  | docker run --rm -i "$PG_IMAGE" \
    psql "$AIVEN_DATABASE_URL" -v ON_ERROR_STOP=1 >/dev/null

echo "sync ok: $LATEST ($(du -h "$LATEST" | awk '{print $1}')) → aiven"
