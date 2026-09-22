#!/usr/bin/env bash
# Nightly Postgres dump for compose.vps.yml (service: db).
# Keeps the newest KEEP_COUNT dumps (default 7 ≈ one week of nightly runs).
# Failures emit PostHog ops_job_failed (via scripts/posthog-ops-event.py).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/twitch-telegram-bot}"
KEEP_COUNT="${KEEP_COUNT:-7}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/bot-${STAMP}.sql.gz"
JOB_NAME="pg_backup"

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

mkdir -p "$BACKUP_DIR"
cd "$APP_DIR"

docker compose -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U bot -d bot --clean --if-exists --no-owner --no-acl \
  | gzip -c >"$OUT.tmp"

mv -f "$OUT.tmp" "$OUT"

# Keep only the newest KEEP_COUNT dumps
# ponytail: O(n) listing is fine — a handful of files, not thousands
mapfile -t old < <(ls -1t "$BACKUP_DIR"/bot-*.sql.gz 2>/dev/null | tail -n +"$((KEEP_COUNT + 1))" || true)
if ((${#old[@]} > 0)); then
  rm -f "${old[@]}"
fi

trap - ERR
_ops_posthog ok "" 0
echo "backup ok: $OUT ($(du -h "$OUT" | awk '{print $1}'))"
