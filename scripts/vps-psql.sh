#!/usr/bin/env bash
# Query production-ish Postgres from a laptop / Cursor agent.
#
# Prefer live VPS (compose service: db). Optional fallback: Aiven cold DR
# mirror via AIVEN_DATABASE_URL (env or repo .env) — may lag overnight.
#
# Usage:
#   ./scripts/vps-psql.sh -c "SELECT 1"
#   ./scripts/vps-psql.sh -At -c "SELECT user_id FROM users LIMIT 3"
#   echo "SELECT 1" | ./scripts/vps-psql.sh
#   ./scripts/vps-psql.sh --aiven -c "SELECT 1"
#   VPS_PSQL_SOURCE=aiven ./scripts/vps-psql.sh -c "SELECT 1"
#
# VPS SSH auth (first match wins):
#   1) VPS_SSH_KEY — path to a private key
#   2) ssh without -i — ~/.ssh/config Host / ssh-agent (recommended on other PCs)
#   3) ~/.ssh/artalk_vps — convenience fallback on the primary machine only
#
# Other env overrides (optional):
#   VPS_SSH_HOST        default bot.themarfa.name
#   VPS_SSH_USER        default root
#   VPS_APP_DIR         default /opt/twitch-telegram-bot
#   VPS_PSQL_SOURCE     vps | aiven | auto (default auto)
#   AIVEN_DATABASE_URL  postgres URL (never logged)
#   AIVEN_PG_IMAGE      default postgres:18-alpine (docker fallback if no local psql)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VPS_SSH_HOST="${VPS_SSH_HOST:-bot.themarfa.name}"
VPS_SSH_USER="${VPS_SSH_USER:-root}"
VPS_APP_DIR="${VPS_APP_DIR:-/opt/twitch-telegram-bot}"
VPS_PSQL_SOURCE="${VPS_PSQL_SOURCE:-auto}"
AIVEN_PG_IMAGE="${AIVEN_PG_IMAGE:-postgres:18-alpine}"

# Optional leading --aiven / --vps (also via VPS_PSQL_SOURCE).
if (($# > 0)); then
  case "$1" in
    --aiven)
      VPS_PSQL_SOURCE=aiven
      shift
      ;;
    --vps)
      VPS_PSQL_SOURCE=vps
      shift
      ;;
  esac
fi

load_aiven_url() {
  if [[ -n "${AIVEN_DATABASE_URL:-}" ]]; then
    return 0
  fi
  local env_file="${AIVEN_ENV_FILE:-$REPO_ROOT/.env}"
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi
  # Single KEY=value line; strip optional quotes. Do not source the whole .env.
  AIVEN_DATABASE_URL="$(
    grep -E '^[[:space:]]*AIVEN_DATABASE_URL=' "$env_file" \
      | tail -n1 \
      | sed -e 's/^[[:space:]]*AIVEN_DATABASE_URL=//' -e 's/^["'\'']//' -e 's/["'\'']$//' \
      | tr -d '\r'
  )"
  AIVEN_DATABASE_URL="${AIVEN_DATABASE_URL:-}"
  [[ -n "$AIVEN_DATABASE_URL" ]]
}

run_aiven_psql() {
  if ! load_aiven_url; then
    echo "error: AIVEN_DATABASE_URL unset (env or repo .env)" >&2
    return 1
  fi
  echo "note: querying Aiven DR mirror (nightly sync; may lag behind live VPS)" >&2
  export AIVEN_DATABASE_URL
  if command -v psql >/dev/null 2>&1; then
    if (($# > 0)); then
      psql "$AIVEN_DATABASE_URL" "$@"
    else
      psql "$AIVEN_DATABASE_URL"
    fi
    return
  fi
  # Prefer project helper (psycopg / psycopg2) when local psql is missing — common on Windows.
  # Skip WindowsApps python3 stubs (exit 49 / "Python" with no version).
  pick_aiven_python() {
    local cand
    for cand in python py python3; do
      if [[ "$cand" == py ]]; then
        command -v py >/dev/null 2>&1 || continue
        if py -3 -c "import psycopg" >/dev/null 2>&1 \
          || py -3 -c "import psycopg2" >/dev/null 2>&1; then
          echo "py -3"
          return 0
        fi
        continue
      fi
      command -v "$cand" >/dev/null 2>&1 || continue
      case "$(command -v "$cand")" in
        */WindowsApps/*) continue ;;
      esac
      if "$cand" -c "import psycopg" >/dev/null 2>&1 \
        || "$cand" -c "import psycopg2" >/dev/null 2>&1; then
        echo "$cand"
        return 0
      fi
    done
    return 1
  }
  local py
  if py="$(pick_aiven_python)"; then
    # shellcheck disable=SC2086
    if (($# > 0)); then
      $py "$SCRIPT_DIR/aiven-psql.py" "$@"
    else
      $py "$SCRIPT_DIR/aiven-psql.py"
    fi
    return
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: need local psql, python (psycopg/psycopg2), or docker to query Aiven" >&2
    return 1
  fi
  # --network host helps some environments resolve Aiven hostnames (same as pg-sync-aiven).
  if (($# > 0)); then
    docker run --rm -i --network host "$AIVEN_PG_IMAGE" \
      psql "$AIVEN_DATABASE_URL" "$@"
  else
    docker run --rm -i --network host "$AIVEN_PG_IMAGE" \
      psql "$AIVEN_DATABASE_URL"
  fi
}

run_vps_psql() {
  local ssh_identity_args=()
  if [[ -n "${VPS_SSH_KEY:-}" ]]; then
    if [[ ! -f "$VPS_SSH_KEY" ]]; then
      echo "error: VPS_SSH_KEY is set but file missing: $VPS_SSH_KEY" >&2
      return 1
    fi
    ssh_identity_args=(-i "$VPS_SSH_KEY" -o IdentitiesOnly=yes)
  elif [[ -f "$HOME/.ssh/artalk_vps" ]]; then
    ssh_identity_args=(-i "$HOME/.ssh/artalk_vps" -o IdentitiesOnly=yes)
  fi
  # else: rely on ssh-agent / IdentityFile in ~/.ssh/config for this host

  local remote_psql_args=""
  if (($# > 0)); then
    remote_psql_args=$(printf '%q ' "$@")
  fi

  local ssh_cmd=(ssh)
  if ((${#ssh_identity_args[@]})); then
    ssh_cmd+=("${ssh_identity_args[@]}")
  fi

  "${ssh_cmd[@]}" \
    -o BatchMode=yes \
    -o ConnectTimeout=15 \
    "${VPS_SSH_USER}@${VPS_SSH_HOST}" \
    "cd $(printf '%q' "$VPS_APP_DIR") && docker compose -f compose.vps.yml exec -T db psql -U bot -d bot ${remote_psql_args}"
}

case "$VPS_PSQL_SOURCE" in
  aiven)
    run_aiven_psql "$@"
    ;;
  vps)
    if ! run_vps_psql "$@"; then
      cat >&2 <<'EOF'
error: cannot reach VPS Postgres via SSH.

On this machine, set up one of:
  export VPS_SSH_KEY=/path/to/private_key
  # or ~/.ssh/config, e.g.:
  #   Host bot.themarfa.name
  #     User root
  #     IdentityFile ~/.ssh/your_vps_key
  #     IdentitiesOnly yes
Or use Aiven DR: ./scripts/vps-psql.sh --aiven -c "SELECT …"
EOF
      exit 1
    fi
    ;;
  auto)
    if run_vps_psql "$@"; then
      exit 0
    fi
    echo "warning: VPS SSH failed; trying Aiven DR mirror…" >&2
    if run_aiven_psql "$@"; then
      exit 0
    fi
    cat >&2 <<'EOF'
error: cannot reach VPS Postgres via SSH, and Aiven fallback failed.

VPS auth — set up one of:
  export VPS_SSH_KEY=/path/to/private_key
  # or ~/.ssh/config IdentityFile for bot.themarfa.name

Aiven — set AIVEN_DATABASE_URL in the environment or repo .env
  (and have local psql or docker). Force Aiven: --aiven
EOF
    exit 1
    ;;
  *)
    echo "error: VPS_PSQL_SOURCE must be auto|vps|aiven (got: $VPS_PSQL_SOURCE)" >&2
    exit 1
    ;;
esac
