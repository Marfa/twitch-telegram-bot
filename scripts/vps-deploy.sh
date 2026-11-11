#!/usr/bin/env bash
# Deploy / update the bot on the VPS from the local git checkout.
# Expected layout: /opt/twitch-telegram-bot with compose.vps.yml and .env
#
# If only static site/guide/assets changed since the last successful deploy,
# sync those trees into the running bot container (no image rebuild / recreate).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
BRANCH="${DEPLOY_BRANCH:-main}"
DEPLOY_SHA_FILE="${DEPLOY_SHA_FILE:-$APP_DIR/.deploy-sha}"

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: $APP_DIR/.env is missing" >&2
  exit 1
fi

# Keep secrets across hard reset
cp -a .env /tmp/twitch-telegram-bot.env.bak

PREV_SHA=""
if [[ -f "$DEPLOY_SHA_FILE" ]]; then
  PREV_SHA="$(tr -d '[:space:]' <"$DEPLOY_SHA_FILE" || true)"
fi

git fetch --prune origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

cp -a /tmp/twitch-telegram-bot.env.bak .env
rm -f /tmp/twitch-telegram-bot.env.bak

NEW_SHA="$(git rev-parse HEAD)"

mark_deployed() {
  printf '%s\n' "$NEW_SHA" >"$DEPLOY_SHA_FILE"
}

classify_mode() {
  local mode
  if [[ -z "$PREV_SHA" ]]; then
    echo "full"
    return 0
  fi
  if [[ "$PREV_SHA" == "$NEW_SHA" ]]; then
    echo "noop"
    return 0
  fi
  if ! git cat-file -e "${PREV_SHA}^{commit}" 2>/dev/null; then
    echo "full"
    return 0
  fi
  mapfile -t _changed < <(git diff --name-only "$PREV_SHA" "$NEW_SHA")
  if [[ ${#_changed[@]} -eq 0 ]]; then
    echo "noop"
    return 0
  fi
  mode="$(python3 scripts/classify_deploy_paths.py "${_changed[@]}")"
  echo "$mode"
}

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

# Production product flags (source defaults are off / OSS-free).
_ensure_env_flag() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s/^${key}=.*/${key}=${val}/" .env
  else
    printf '\n%s=%s\n' "$key" "$val" >> .env
  fi
  echo "env: ${key}=${val}"
}
_ensure_env_flag ENABLE_PREMIUM 1
_ensure_env_flag ENABLE_HELP 1
_ensure_env_flag ENABLE_PARTNER 1

DEPLOY_MODE="$(classify_mode)"
echo "deploy: mode=${DEPLOY_MODE} prev=${PREV_SHA:-none} new=${NEW_SHA}"

wait_bot_health() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

sync_site_into_bot() {
  local ctr
  ctr="$(docker compose -f "$COMPOSE_FILE" ps -q bot 2>/dev/null || true)"
  if [[ -z "$ctr" ]]; then
    echo "deploy: bot container not running" >&2
    return 1
  fi
  if ! wait_bot_health; then
    echo "deploy: bot /health not ready before site sync" >&2
    return 1
  fi
  docker cp webapp/site/. "${ctr}:/app/webapp/site/"
  docker cp webapp/guide/. "${ctr}:/app/webapp/guide/"
  if [[ -d assets ]]; then
    docker cp assets/. "${ctr}:/app/assets/"
  fi
  # Cheap smoke: landing still served.
  if ! curl -fsS "http://127.0.0.1:8080/" >/dev/null 2>&1; then
    echo "deploy: GET / failed after site sync" >&2
    return 1
  fi
  return 0
}

if [[ "$DEPLOY_MODE" == "noop" ]]; then
  echo "deploy: already at ${NEW_SHA}; nothing to do"
  mark_deployed
  exit 0
fi

if [[ "$DEPLOY_MODE" == "site" ]]; then
  if sync_site_into_bot; then
    echo "deploy: site-only sync ok (bot not recreated)"
    docker compose -f "$COMPOSE_FILE" ps
    mark_deployed
    exit 0
  fi
  echo "deploy: site-only sync failed; falling back to full recreate" >&2
fi

# Dump 17 and restore into 18 before compose up (no-op once db is already 18).
if [[ -f scripts/pg-upgrade-to-18.sh ]]; then
  bash scripts/pg-upgrade-to-18.sh
fi

# Build-first: image is compiled while the old bot container still runs;
# then only bot is recreated (~5–15s gap vs stopping during the whole build).
wait_db() {
  local i
  for i in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U bot -d bot >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "ERROR: db not ready after 60s" >&2
  return 1
}

docker compose -f "$COMPOSE_FILE" up -d db
wait_db

# Snapshot the running bot image so a bad release can roll back.
BOT_CTR="$(docker compose -f "$COMPOSE_FILE" ps -q bot 2>/dev/null || true)"
PREV_IMAGE_ID=""
BOT_IMAGE_REF="twitch-telegram-bot-bot:latest"
if [[ -n "${BOT_CTR}" ]]; then
  PREV_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$BOT_CTR" 2>/dev/null || true)"
  BOT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$BOT_CTR" 2>/dev/null || echo "$BOT_IMAGE_REF")"
  case "$BOT_IMAGE_REF" in
    *:*) ;;
    *) BOT_IMAGE_REF="${BOT_IMAGE_REF}:latest" ;;
  esac
fi

docker compose -f "$COMPOSE_FILE" build bot
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate --remove-orphans bot

# Host uses /etc/cron.d for nightly dump + Aiven DR. Ubuntu images may ship
# without the cron package — without the daemon those files never run and
# PostHog never sees ops_job_* (silence ≠ healthy).
if ! command -v cron >/dev/null 2>&1 || ! systemctl is-active --quiet cron 2>/dev/null; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq cron
  fi
  systemctl enable --now cron 2>/dev/null || service cron start 2>/dev/null || true
fi
if ! systemctl is-active --quiet cron 2>/dev/null; then
  echo "warning: cron is not active; nightly pg-backup / pg-sync-aiven will not run" >&2
fi

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

# Nightly restore of newest dump into Aiven (cold DR; needs AIVEN_DATABASE_URL in .env)
if [[ -f scripts/pg-sync-aiven.sh ]]; then
  install -m 755 scripts/pg-sync-aiven.sh /usr/local/sbin/twitch-telegram-bot-pg-sync-aiven
  cat >/etc/cron.d/twitch-telegram-bot-pg-sync-aiven <<EOF
# Nightly DR sync: VPS dump → Aiven (after pg-backup)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
15 3 * * * root APP_DIR=$APP_DIR /usr/local/sbin/twitch-telegram-bot-pg-sync-aiven >>/var/log/twitch-telegram-bot-pg-sync-aiven.log 2>&1
EOF
  chmod 644 /etc/cron.d/twitch-telegram-bot-pg-sync-aiven
fi

post_health_ok() {
  echo "health: ok"
  if [[ -f scripts/migrate-import-template.py ]]; then
    echo "migration: running import/sync template update..."
    docker compose -f "$COMPOSE_FILE" exec -T bot python scripts/migrate-import-template.py
  fi
  docker compose -f "$COMPOSE_FILE" ps
  mark_deployed
}

if wait_bot_health; then
  post_health_ok
  exit 0
fi

echo "WARNING: /health did not become ready in time" >&2
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" logs --tail=80 bot || true

if [[ -n "$PREV_IMAGE_ID" ]]; then
  echo "ROLLBACK: restoring previous image $PREV_IMAGE_ID as $BOT_IMAGE_REF" >&2
  docker tag "$PREV_IMAGE_ID" "$BOT_IMAGE_REF"
  docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate bot
  if wait_bot_health; then
    echo "ROLLBACK: previous image is healthy again" >&2
    docker compose -f "$COMPOSE_FILE" ps
    exit 1
  fi
  echo "ROLLBACK: previous image also failed health" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail=40 bot || true
fi

exit 1
