#!/usr/bin/env bash
# Materialize the VPS SSH private key from Bitwarden into <repo>/.ssh/artalk_vps.
#
# SoR = Bitwarden SSH key item (e.g. "VPS HOSTKEY"). Never commit the file.
# Usage:
#   export BW_SESSION="$(bw unlock --raw)"
#   ./scripts/vps-ssh-key-pull.sh
#   ./scripts/vps-ssh-key-pull.sh "VPS HOSTKEY"   # optional item name override
#
# Env:
#   BW_SESSION   required (bw unlock --raw)
#   VPS_SSH_BW_ITEM  default item name when argv empty (default: VPS HOSTKEY)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$REPO_ROOT/.ssh"
OUT_FILE="$OUT_DIR/artalk_vps"
ITEM_NAME="${1:-${VPS_SSH_BW_ITEM:-}}"

if ! command -v bw >/dev/null 2>&1; then
  echo "error: bw (Bitwarden CLI) not found" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq not found (needed to extract sshKey.privateKey)" >&2
  exit 1
fi
if [[ -z "${BW_SESSION:-}" ]]; then
  echo "error: BW_SESSION unset. Run: export BW_SESSION=\"\$(bw unlock --raw)\"" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

# Resolve item id by exact name (SSH key items in Bitwarden).
resolve_item_id() {
  local name="$1"
  bw list items --session "$BW_SESSION" \
    | jq -r --arg n "$name" '
        [.[] | select(.name == $n)] | first | .id // empty
      '
}

ITEM_ID=""
if [[ -n "$ITEM_NAME" ]]; then
  ITEM_ID="$(resolve_item_id "$ITEM_NAME")"
else
  # Prefer corrected spelling; accept existing typo in vault.
  for cand in "VPS HOSTKEY" "VPS HOSTKET"; do
    ITEM_ID="$(resolve_item_id "$cand")"
    if [[ -n "$ITEM_ID" ]]; then
      ITEM_NAME="$cand"
      break
    fi
  done
fi
if [[ -z "$ITEM_ID" ]]; then
  echo "error: Bitwarden SSH key item not found (${ITEM_NAME:-VPS HOSTKEY / VPS HOSTKET})" >&2
  echo "hint: pass the vault name: $0 \"VPS HOSTKET\"" >&2
  exit 1
fi

umask 077
TMP="$(mktemp "$OUT_DIR/.artalk_vps.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

bw get item "$ITEM_ID" --session "$BW_SESSION" \
  | jq -r '.sshKey.privateKey // empty' > "$TMP"

if [[ ! -s "$TMP" ]]; then
  echo "error: item has empty sshKey.privateKey (is it an SSH key item?)" >&2
  exit 1
fi
if ! grep -q 'BEGIN OPENSSH PRIVATE KEY\|BEGIN .*PRIVATE KEY' "$TMP"; then
  echo "error: privateKey does not look like a PEM/OpenSSH private key" >&2
  exit 1
fi

# Git OpenSSH rejects CRLF private keys; normalize to LF for cross-tool use.
tr -d '\r' < "$TMP" > "${TMP}.lf"
mv "${TMP}.lf" "$TMP"

mv "$TMP" "$OUT_FILE"
trap - EXIT
chmod 600 "$OUT_FILE"

# Fingerprint only — never print the private key.
if command -v ssh-keygen >/dev/null 2>&1; then
  echo "wrote $OUT_FILE"
  ssh-keygen -lf "$OUT_FILE"
else
  echo "wrote $OUT_FILE (chmod 600)"
fi
echo "Set VPS_SSH_KEY=.ssh/artalk_vps (already typical in .env) and reload vps-db MCP."
