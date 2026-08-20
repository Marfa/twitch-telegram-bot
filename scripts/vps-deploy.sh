#!/usr/bin/env bash
# Deploy / update the bot on the VPS from the local git checkout.
# Expected layout: /opt/twitch-telegram-bot with compose.vps.yml and .env
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BRANCH="${DEPLOY_BRANCH:-main}"

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: $APP_DIR/.env is missing" >&2
  exit 1
fi

# Keep secrets across hard reset
cp -a .env /tmp/twitch-telegram-bot.env.bak

git fetch --prune origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

cp -a /tmp/twitch-telegram-bot.env.bak .env
rm -f /tmp/twitch-telegram-bot.env.bak

# One-shot: typo POSHTOG_API_KEY_PERSONAL on VPS → POSTHOG_API_KEY_PERSONAL
if grep -q '^POSHTOG_API_KEY_PERSONAL=' .env; then
  if grep -qE '^POSTHOG_API_KEY_PERSONAL=.+' .env; then
    sed -i '/^POSHTOG_API_KEY_PERSONAL=/d' .env
  else
    sed -i '/^POSTHOG_API_KEY_PERSONAL=/d' .env
    sed -i 's/^POSHTOG_API_KEY_PERSONAL=/POSTHOG_API_KEY_PERSONAL=/' .env
  fi
  echo "env: POSHTOG_API_KEY_PERSONAL → POSTHOG_API_KEY_PERSONAL"
fi

# Dump 17 and restore into 18 before compose up (no-op once db is already 18).
if [[ -f scripts/pg-upgrade-to-18.sh ]]; then
  bash scripts/pg-upgrade-to-18.sh
fi

docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans

# Nightly Postgres dump (7 newest files under /var/backups/twitch-telegram-bot)
if [[ -f scripts/pg-backup.sh ]]; then
  install -m 755 scripts/pg-backup.sh /usr/local/sbin/twitch-telegram-bot-pg-backup
  cat >/etc/cron.d/twitch-telegram-bot-pg-backup <<EOF
# Nightly dump of twitch-telegram-bot Postgres (keep 7)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3 * * * root APP_DIR=$APP_DIR /usr/local/sbin/twitch-telegram-bot-pg-backup >>/var/log/twitch-telegram-bot-pg-backup.log 2>&1
EOF
  chmod 644 /etc/cron.d/twitch-telegram-bot-pg-backup
fi

# Wait briefly for health
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
    echo "health: ok"
    if [[ -f scripts/migrate-import-template.py ]]; then
      echo "migration: running import/sync template update..."
      docker compose -f "$COMPOSE_FILE" exec -T bot python scripts/migrate-import-template.py
    fi
    docker compose -f "$COMPOSE_FILE" ps
    exit 0
  fi
  sleep 2
done

echo "WARNING: /health did not become ready in time" >&2
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" logs --tail=80 bot || true
exit 1
