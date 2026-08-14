#!/usr/bin/env bash
# One-shot Postgres 17 → 18 dump/restore for compose.vps.yml.
# No-op if db is already 18. Refuses to start an empty 18 cluster.
#
# Frozen dump (not rotated by nightly): /var/backups/twitch-telegram-bot/pre-pg18-upgrade.sql.gz
# Rollback if restore fails: compose from git HEAD^ (17 + pg-data volume).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/twitch-telegram-bot}"
FROZEN="$BACKUP_DIR/pre-pg18-upgrade.sql.gz"
COUNTS_BEFORE="$BACKUP_DIR/pre-pg18-counts.txt"
COUNTS_AFTER="$BACKUP_DIR/post-pg18-counts.txt"

cd "$APP_DIR"

if ! grep -q 'image: postgres:18-alpine' "$COMPOSE_FILE"; then
  exit 0
fi

db_id() {
  docker compose -f "$COMPOSE_FILE" ps -aq db 2>/dev/null || true
}

running_image() {
  local id
  id="$(db_id)"
  [[ -n "$id" ]] || return 1
  docker inspect -f '{{.Config.Image}}' "$id" 2>/dev/null || return 1
}

wait_db() {
  local i
  for i in $(seq 1 60); do
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U bot -d bot >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "ERROR: pg-upgrade-to-18: db never became ready" >&2
  return 1
}

write_counts() {
  local out="$1"
  docker compose -f "$COMPOSE_FILE" exec -T db psql -U bot -d bot -v ON_ERROR_STOP=1 -At -c "
SELECT t || E'\t' || c::text FROM (
  SELECT 'alert_history'::text AS t, COUNT(*)::bigint AS c FROM alert_history
  UNION ALL SELECT 'lucky_templates', COUNT(*) FROM lucky_templates
  UNION ALL SELECT 'referral_credits', COUNT(*) FROM referral_credits
  UNION ALL SELECT 'referral_withdrawals', COUNT(*) FROM referral_withdrawals
  UNION ALL SELECT 'scheduled_broadcasts', COUNT(*) FROM scheduled_broadcasts
  UNION ALL SELECT 'subscriptions', COUNT(*) FROM subscriptions
  UNION ALL SELECT 'twitch_sync', COUNT(*) FROM twitch_sync
  UNION ALL SELECT 'users', COUNT(*) FROM users
) x ORDER BY t;
" >"$out"
}

rollback_17() {
  echo "pg-upgrade-to-18: rolling back to Postgres 17" >&2
  local rollback="/tmp/compose.vps.pg17.yml"
  if git show HEAD^:"$COMPOSE_FILE" >"$rollback" 2>/dev/null; then
    docker compose -f "$rollback" up -d --remove-orphans || true
  else
    echo "ERROR: could not load $COMPOSE_FILE from HEAD^" >&2
  fi
}

img="$(running_image || true)"
if [[ "$img" == *postgres:18* ]]; then
  echo "pg-upgrade-to-18: already on 18"
  exit 0
fi

if [[ -z "$img" ]]; then
  echo "ERROR: pg-upgrade-to-18: db is not running; will not create an empty 18 cluster" >&2
  exit 1
fi

if [[ "$img" != *postgres:17* ]]; then
  echo "ERROR: pg-upgrade-to-18: unexpected db image: $img" >&2
  exit 1
fi

echo "pg-upgrade-to-18: dumping $img"
mkdir -p "$BACKUP_DIR"
docker compose -f "$COMPOSE_FILE" stop bot
bash "$APP_DIR/scripts/pg-backup.sh"
latest="$(ls -1t "$BACKUP_DIR"/bot-*.sql.gz | head -n 1)"
cp -a "$latest" "$FROZEN"
if [[ -d /root && -w /root ]]; then
  cp -a "$latest" /root/pre-pg18-upgrade.sql.gz
fi
echo "pg-upgrade-to-18: frozen dump $FROZEN ($(du -h "$FROZEN" | awk '{print $1}'))"

write_counts "$COUNTS_BEFORE"
echo "pg-upgrade-to-18: counts before:"
cat "$COUNTS_BEFORE"

docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate db
wait_db

echo "pg-upgrade-to-18: restoring into 18"
if ! gunzip -c "$FROZEN" | docker compose -f "$COMPOSE_FILE" exec -T db psql -U bot -d bot -v ON_ERROR_STOP=1 >/tmp/pg18-restore.log; then
  echo "ERROR: pg-upgrade-to-18: restore failed (log /tmp/pg18-restore.log)" >&2
  rollback_17
  exit 1
fi

write_counts "$COUNTS_AFTER"
echo "pg-upgrade-to-18: counts after:"
cat "$COUNTS_AFTER"

if ! diff -u "$COUNTS_BEFORE" "$COUNTS_AFTER"; then
  echo "ERROR: pg-upgrade-to-18: row counts changed after restore" >&2
  rollback_17
  exit 1
fi

echo "pg-upgrade-to-18: restore ok (17 volume left in place for rollback)"
