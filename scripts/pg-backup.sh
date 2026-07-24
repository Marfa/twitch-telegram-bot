#!/usr/bin/env bash
# Nightly Postgres dump for compose.vps.yml (service: db).
# Keeps the newest KEEP_COUNT dumps (default 7 ≈ one week of nightly runs).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/twitch-telegram-bot}"
KEEP_COUNT="${KEEP_COUNT:-7}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/bot-${STAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"
cd "$APP_DIR"

docker compose -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U bot -d bot --clean --if-exists \
  | gzip -c >"$OUT.tmp"

mv -f "$OUT.tmp" "$OUT"

# Keep only the newest KEEP_COUNT dumps
# ponytail: O(n) listing is fine — a handful of files, not thousands
mapfile -t old < <(ls -1t "$BACKUP_DIR"/bot-*.sql.gz 2>/dev/null | tail -n +"$((KEEP_COUNT + 1))" || true)
if ((${#old[@]} > 0)); then
  rm -f "${old[@]}"
fi

echo "backup ok: $OUT ($(du -h "$OUT" | awk '{print $1}'))"
