#!/usr/bin/env bash
# Live production Postgres on the VPS (compose service: db).
# This is the only supported way to query prod from a laptop / Cursor agent.
#
# Usage:
#   ./scripts/vps-psql.sh -c "SELECT 1"
#   ./scripts/vps-psql.sh -At -c "SELECT user_id FROM users LIMIT 3"
#   echo "SELECT 1" | ./scripts/vps-psql.sh
#
# Auth (first match wins):
#   1) VPS_SSH_KEY — path to a private key
#   2) ssh without -i — ~/.ssh/config Host / ssh-agent (recommended on other PCs)
#   3) ~/.ssh/artalk_vps — convenience fallback on the primary machine only
#
# Other env overrides (optional):
#   VPS_SSH_HOST   default bot.themarfa.name
#   VPS_SSH_USER   default root
#   VPS_APP_DIR    default /opt/twitch-telegram-bot
set -euo pipefail

VPS_SSH_HOST="${VPS_SSH_HOST:-bot.themarfa.name}"
VPS_SSH_USER="${VPS_SSH_USER:-root}"
VPS_APP_DIR="${VPS_APP_DIR:-/opt/twitch-telegram-bot}"

ssh_identity_args=()
if [[ -n "${VPS_SSH_KEY:-}" ]]; then
  if [[ ! -f "$VPS_SSH_KEY" ]]; then
    echo "error: VPS_SSH_KEY is set but file missing: $VPS_SSH_KEY" >&2
    exit 1
  fi
  ssh_identity_args=(-i "$VPS_SSH_KEY" -o IdentitiesOnly=yes)
elif [[ -f "$HOME/.ssh/artalk_vps" ]]; then
  ssh_identity_args=(-i "$HOME/.ssh/artalk_vps" -o IdentitiesOnly=yes)
fi
# else: rely on ssh-agent / IdentityFile in ~/.ssh/config for this host

remote_psql_args=""
if (($# > 0)); then
  remote_psql_args=$(printf '%q ' "$@")
fi

if ! ssh "${ssh_identity_args[@]}" \
  -o BatchMode=yes \
  -o ConnectTimeout=15 \
  "${VPS_SSH_USER}@${VPS_SSH_HOST}" \
  "cd $(printf '%q' "$VPS_APP_DIR") && docker compose -f compose.vps.yml exec -T db psql -U bot -d bot ${remote_psql_args}"; then
  cat >&2 <<'EOF'
error: cannot reach VPS Postgres via SSH.

On this machine, set up one of:
  export VPS_SSH_KEY=/path/to/private_key
  # or ~/.ssh/config, e.g.:
  #   Host bot.themarfa.name
  #     User root
  #     IdentityFile ~/.ssh/your_vps_key
  #     IdentitiesOnly yes
EOF
  exit 1
fi
