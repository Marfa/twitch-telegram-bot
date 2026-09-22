#!/usr/bin/env bash
# Cold DR mirror: live VPS Postgres → Aiven (user/bot data only).
# IGDB dump tables are excluded — they are huge and rebuilt on VPS via igdb_dumps.
# Reads AIVEN_DATABASE_URL from $APP_DIR/.env — never logs the URL.
# Failures/skips emit PostHog ops_job_* (via scripts/posthog-ops-event.py).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
PG_IMAGE="${PG_IMAGE:-postgres:18-alpine}"
JOB_NAME="pg_sync_aiven"

_ops_posthog() {
  local status="$1" reason="${2:-}" code="${3:-0}"
  local py="$APP_DIR/scripts/posthog-ops-event.py"
  [[ -f "$py" ]] || return 0
  (
    cd "$APP_DIR"
    if docker compose -f "$COMPOSE_FILE" exec -T bot \
      python scripts/posthog-ops-event.py \
      --job "$JOB_NAME" --status "$status" --reason "$reason" --exit-code "$code"
    then
      return 0
    fi
    python3 "$py" \
      --job "$JOB_NAME" --status "$status" --reason "$reason" --exit-code "$code" \
      --env-file "$ENV_FILE" || true
  ) >/dev/null 2>&1 || true
}

_on_err() {
  local ec=$?
  _ops_posthog failed "exit_${ec}" "$ec"
  exit "$ec"
}
trap _on_err ERR

if [[ ! -f "$ENV_FILE" ]]; then
  trap - ERR
  echo "skip: no $ENV_FILE"
  _ops_posthog skipped "no_env_file" 0
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
  trap - ERR
  echo "skip: AIVEN_DATABASE_URL unset"
  _ops_posthog skipped "aiven_url_unset" 0
  exit 0
fi

cd "$APP_DIR"

# Dump live primary excluding regenerable IGDB partner dumps (and temp load tables).
# --clean/--if-exists so Aiven schema matches VPS for included tables.
#
# Orphans not in this dump (e.g. legacy render_status_seen) are dropped explicitly
# after restore — pg_dump --clean only drops objects that are in the dump.
EXCLUDE_ARGS=(
  --exclude-table='igdb_*'
  --exclude-table='_igdb_*'
)

# --network host: bridge DNS on some VPSes cannot resolve Aiven hostnames
docker compose -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U bot -d bot --clean --if-exists --no-owner --no-acl \
  "${EXCLUDE_ARGS[@]}" \
  | docker run --rm -i --network host "$PG_IMAGE" \
    psql "$AIVEN_DATABASE_URL" -v ON_ERROR_STOP=1 >/dev/null

docker run --rm -i --network host "$PG_IMAGE" \
  psql "$AIVEN_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "DROP TABLE IF EXISTS render_status_seen;" >/dev/null

trap - ERR
_ops_posthog ok "" 0
echo "sync ok: live VPS → aiven (igdb_* excluded)"
