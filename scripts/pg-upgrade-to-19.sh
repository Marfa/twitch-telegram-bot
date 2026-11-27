#!/usr/bin/env bash
# One-shot Postgres 18 → 19 dump/restore for compose.vps.yml.
# No-op if db is already 19. Refuses to start an empty 19 cluster.
#
# Frozen dump (not rotated by nightly): /var/backups/twitch-telegram-bot/pre-pg19-upgrade.sql.gz
# Rollback if restore fails: compose from git HEAD^ (18 + pg-data-18 volume).
#
# Run through scripts/bot-maintenance.sh, which stops the bot for the dump.
#
# Requires compose.vps.yml: image postgres:19-alpine and volume pg-data-19
# (new empty volume — do not reuse pg-data-18; PG 18+ stores
# /var/lib/postgresql/<major>/docker under the mount).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/twitch-telegram-bot}"
FROZEN="$BACKUP_DIR/pre-pg19-upgrade.sql.gz"
COUNTS_BEFORE="$BACKUP_DIR/pre-pg19-counts.txt"
COUNTS_AFTER="$BACKUP_DIR/post-pg19-counts.txt"

cd "$APP_DIR"

db_id() {
  docker compose -f "$COMPOSE_FILE" ps -aq db 2>/dev/null || true
}

running_image() {
  local id
  id="$(db_id)"
  [[ -n "$id" ]] || return 1
  docker inspect -f '{{.Config.Image}}' "$id" 2>/dev/null || return 1
}

# --check-needed: 0 when the dump/restore still has to run, 1 when this database
# already runs the target major. scripts/vps-deploy.sh asks before it opens the
# maintenance window, which would otherwise restart the bot for nothing.
if [[ "${1:-}" == "--check-needed" ]]; then
  grep -q 'image: postgres:19-alpine' "$COMPOSE_FILE" || exit 1
  img="$(running_image || true)"
  if [[ "$img" == *postgres:19* ]]; then
    exit 1
  fi
  exit 0
fi

if ! grep -q 'image: postgres:19-alpine' "$COMPOSE_FILE"; then
  exit 0
fi

if ! grep -q 'pg-data-19:' "$COMPOSE_FILE"; then
  echo "ERROR: pg-upgrade-to-19: compose must use volume pg-data-19 (not pg-data-18)" >&2
  exit 1
fi

wait_db() {
  local i
  for i in $(seq 1 60); do
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U bot -d bot >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "ERROR: pg-upgrade-to-19: db never became ready" >&2
  return 1
}

rollback_18() {
  echo "pg-upgrade-to-19: rolling back to Postgres 18" >&2
  local rollback="/tmp/compose.vps.pg18.yml"
  if git show HEAD^:"$COMPOSE_FILE" >"$rollback" 2>/dev/null; then
    docker compose -f "$rollback" up -d --remove-orphans || true
  else
    echo "ERROR: could not load $COMPOSE_FILE from HEAD^" >&2
  fi
}

img="$(running_image || true)"
if [[ "$img" == *postgres:19* ]]; then
  echo "pg-upgrade-to-19: already on 19"
  exit 0
fi

if [[ -z "$img" ]]; then
  echo "ERROR: pg-upgrade-to-19: db is not running; will not create an empty 19 cluster" >&2
  exit 1
fi

if [[ "$img" != *postgres:18* ]]; then
  echo "ERROR: pg-upgrade-to-19: unexpected db image: $img" >&2
  exit 1
fi

# The dump needs the bot stopped, which scripts/bot-maintenance.sh does.
if [[ "${BOT_MAINTENANCE_WINDOW:-0}" != "1" ]]; then
  echo "ERROR: pg-upgrade-to-19: run through scripts/bot-maintenance.sh (the bot must be stopped for the dump)" >&2
  exit 1
fi

echo "pg-upgrade-to-19: dumping $img"
mkdir -p "$BACKUP_DIR"
bash "$APP_DIR/scripts/pg-backup.sh"
latest="$(ls -1t "$BACKUP_DIR"/bot-*.sql.gz | head -n 1)"
cp -a "$latest" "$FROZEN"
if [[ -d /root && -w /root ]]; then
  cp -a "$latest" /root/pre-pg19-upgrade.sql.gz
fi
echo "pg-upgrade-to-19: frozen dump $FROZEN ($(du -h "$FROZEN" | awk '{print $1}'))"

bash "$APP_DIR/scripts/pg-row-counts.sh" "$COUNTS_BEFORE"
echo "pg-upgrade-to-19: counts before:"
cat "$COUNTS_BEFORE"

docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate db
wait_db

echo "pg-upgrade-to-19: restoring into 19"
if ! gunzip -c "$FROZEN" | docker compose -f "$COMPOSE_FILE" exec -T db psql -U bot -d bot -v ON_ERROR_STOP=1 >/tmp/pg19-restore.log; then
  echo "ERROR: pg-upgrade-to-19: restore failed (log /tmp/pg19-restore.log)" >&2
  rollback_18
  exit 1
fi

bash "$APP_DIR/scripts/pg-row-counts.sh" "$COUNTS_AFTER"
echo "pg-upgrade-to-19: counts after:"
cat "$COUNTS_AFTER"

if ! diff -u "$COUNTS_BEFORE" "$COUNTS_AFTER"; then
  echo "ERROR: pg-upgrade-to-19: row counts changed after restore" >&2
  rollback_18
  exit 1
fi

echo "pg-upgrade-to-19: restore ok (18 volume pg-data-18 left in place for rollback)"
