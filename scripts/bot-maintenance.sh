#!/usr/bin/env bash
# The one maintenance window for the bot.
#
# Snapshots the running bot image, stops the bot, runs the given step, then
# brings the bot back on every exit path: the snapshot image when the step
# failed, the requested image when it succeeded. It waits for /health and exits
# nonzero when the bot does not come back, so a stopped bot never outlives the
# step that stopped it.
#
# Usage: scripts/bot-maintenance.sh [options] [-- <command> [args...]]
#
# Options:
#   --image REF            image to run the bot with after a successful step
#                          (default: the image the bot ran with before the window)
#   --health-url URL       health endpoint (default http://127.0.0.1:8080/health)
#   --health-tries N       health attempts (default 30)
#   --health-interval SEC  seconds between health attempts (default 2)
#
# With no step the window only recreates the bot, which applies a changed .env.
# The step runs with BOT_MAINTENANCE_WINDOW=1 in its environment, so a step that
# needs the bot stopped can refuse to run outside a window.
#
# Examples:
#   scripts/bot-maintenance.sh -- bash scripts/pg-upgrade-to-19.sh
#   scripts/bot-maintenance.sh            # recreate the bot after a .env change
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}"
TARGET_IMAGE=""
STEP=()

usage() {
  sed -n '2,/^[^#]/p' "$0" | sed -e '/^[^#]/d' -e 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      TARGET_IMAGE="${2:?--image needs an image reference}"
      shift 2
      ;;
    --health-url)
      HEALTH_URL="${2:?--health-url needs a URL}"
      shift 2
      ;;
    --health-tries)
      HEALTH_TRIES="${2:?--health-tries needs a count}"
      shift 2
      ;;
    --health-interval)
      HEALTH_INTERVAL="${2:?--health-interval needs seconds}"
      shift 2
      ;;
    --)
      shift
      STEP=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: bot-maintenance: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

cd "$APP_DIR"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@" </dev/null
}

bot_ctr() {
  compose ps -q bot 2>/dev/null || true
}

wait_health() {
  bash "$APP_DIR/scripts/wait-bot-health.sh" "$HEALTH_URL" "$HEALTH_TRIES" "$HEALTH_INTERVAL"
}

# Stable tag for the image the bot runs before the window. compose build moves
# the single mutable :latest tag, so a snapshot kept only by image ID can be
# gone by the time the bot has to come back.
BOT_IMAGE_REF="twitch-telegram-bot-bot:latest"
SNAPSHOT_IMAGE=""
ctr="$(bot_ctr)"
if [[ -n "$ctr" ]]; then
  image_id="$(docker inspect -f '{{.Image}}' "$ctr" </dev/null 2>/dev/null || true)"
  BOT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$ctr" </dev/null 2>/dev/null || echo "$BOT_IMAGE_REF")"
  case "$BOT_IMAGE_REF" in
    *:*) ;;
    *) BOT_IMAGE_REF="${BOT_IMAGE_REF}:latest" ;;
  esac
  if [[ -n "$image_id" ]]; then
    SNAPSHOT_IMAGE="${BOT_IMAGE_REF%:*}:maintenance-snapshot"
    if ! docker tag "$image_id" "$SNAPSHOT_IMAGE" </dev/null 2>/dev/null; then
      # The running image is already untagged and removed; a snapshot from an
      # earlier window is then the only image that can bring the bot back.
      if docker image inspect "$SNAPSHOT_IMAGE" >/dev/null 2>&1; then
        echo "WARNING: bot-maintenance: running bot image $image_id is gone; using $SNAPSHOT_IMAGE from an earlier window" >&2
      else
        echo "WARNING: bot-maintenance: running bot image $image_id is gone and no snapshot exists" >&2
        SNAPSHOT_IMAGE=""
      fi
    fi
    echo "bot-maintenance: snapshot ${SNAPSHOT_IMAGE:-none} = $image_id" >&2
  fi
else
  echo "WARNING: bot-maintenance: no running bot container to snapshot" >&2
fi

TARGET_IMAGE="${TARGET_IMAGE:-$BOT_IMAGE_REF}"

bring_up() {
  local image="$1"
  if [[ -n "$image" && "$image" != "$BOT_IMAGE_REF" ]]; then
    docker tag "$image" "$BOT_IMAGE_REF" </dev/null
  fi
  compose up -d --no-deps --force-recreate bot
}

RESTORED=0
finish() {
  local rc=$?
  trap - EXIT
  if [[ "$RESTORED" -eq 0 ]]; then
    RESTORED=1
    local image="$TARGET_IMAGE"
    if [[ "$rc" -ne 0 ]]; then
      image="$SNAPSHOT_IMAGE"
      echo "bot-maintenance: step failed (exit $rc); bringing the previous bot back" >&2
    fi
    if ! bring_up "$image"; then
      echo "ERROR: bot-maintenance: could not recreate the bot" >&2
      rc=1
    elif ! wait_health; then
      compose logs --tail=80 bot || true
      rc=1
    fi
  fi
  exit "$rc"
}
trap finish EXIT

echo "bot-maintenance: stopping bot" >&2
compose stop bot

if [[ ${#STEP[@]} -gt 0 ]]; then
  echo "bot-maintenance: running step: ${STEP[*]}" >&2
  BOT_MAINTENANCE_WINDOW=1 "${STEP[@]}"
fi
