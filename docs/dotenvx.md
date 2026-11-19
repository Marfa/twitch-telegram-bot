# dotenvx + Bitwarden (local secrets)

Encrypted `.env` is **committed to git**. The private key lives in Bitwarden (never in git).
Devices sync ciphertext via git; unlock with the same Bitwarden account.
Production VPS keeps a **plaintext** `.env` next to compose (deploy scripts restore it after `git pull`).

Cursor agent rules: `.cursor/rules/dotenvx-bitwarden.mdc` (always on) · critical key sync: `.cursor/rules/twitch-client-secret.mdc`.

Official docs: [dotenvx](https://dotenvx.com/) · [Bitwarden custody](https://dotenvx.com/docs/custody/bitwarden/)

## Security model

| In git | Out of git |
|---|---|
| Encrypted `.env` (`encrypted:…` + `DOTENV_PUBLIC_KEY`) | `.env.keys`, plaintext secrets, `BW_SESSION` |
| `.env.example` (template) | Bitwarden item with the private key |

<!-- realitylint-ignore-next-line RL014 — DOTENV_PRIVATE_KEY is the .env.keys field, never an .env.example entry. -->
**Bitwarden item name:** dotenvx creates titles like `dotenvx (02A 49E)` (short public key). Do **not** rename to `DOTENV_PRIVATE_KEY` — that name is only the field inside `.env.keys` / process env. `bitwarden pull`/`run` use a local custody UUID reference, not the display name.

**Trade-off:** with the private key, ciphertext from git history is decryptable. Keep Bitwarden locked down; rotate after any leak.

Docker/`compose*.yml` on the VPS still use plaintext `env_file: .env` (not the committed ciphertext as runtime).

## One-time setup (PC #1)

1. Install tools (**host CLIs**, not Python/`requirements.txt` deps)  
   - **dotenvx:** Windows `winget install dotenvx.dotenvx` · macOS binary / brew · [releases](https://github.com/dotenvx/dotenvx/releases) — keep reasonably current on each PC; the repo does not pin a version  
   - **Bitwarden CLI:** `bw` ([install](https://bitwarden.com/help/cli/)) → `bw login`

2. Fill secrets (from `.env.example` if needed), then:

```bash
dotenvx encrypt
export BW_SESSION="$(bw unlock --raw)"
dotenvx bitwarden up
git add .env
git commit -m "encrypt .env"
```

`bitwarden up` creates a personal vault item, stores a **local** custody reference, verifies, and **removes** `.env.keys`.

On a machine without that local reference, `dotenvx bitwarden pull` may say `NOT_FOUND`. Then materialize `.env.keys` with `bw get password <item-id>` (do not print the key), or rely on `dotenvx run` after unlock once custody is wired.

3. Run:

```bash
dotenvx run -- python main.py
```

Helpers: `scripts/env-run.sh` / `scripts/env-run.ps1`.

## Second machine

1. `git pull` (gets encrypted `.env`).
2. Install `dotenvx` + `bw`, `bw login` (same Bitwarden account).
3. Unlock / pull private key (custody `pull` when the local reference exists, or `bw get password <item-id>` → `.env.keys`).
4. `dotenvx run -- python main.py`.

`config.py` / `dotenv_loader.py` call `dotenvx decrypt --stdout` when they see `encrypted:` values.

## Update a secret

```bash
dotenvx set SOME_KEY "new-value"
git add .env
git commit -m "encrypt .env"
# push so other devices get ciphertext; sync VPS plaintext if critical (twitch-client-secret.mdc)
```

## Rotate the keypair (after key leak)

```bash
dotenvx decrypt -f .env --stdout > /tmp/env-plain.env   # umask 077; delete ASAP
# replace .env with plaintext (no DOTENV_PUBLIC_KEY); remove .env.keys
dotenvx encrypt
export BW_SESSION="$(bw unlock --raw)"
dotenvx bitwarden up               # new vault item; delete the old leaked item in Bitwarden UI
git add .env
git commit -m "encrypt .env"
```

## VPS SSH key (not dotenvx ciphertext)

OpenSSH needs a **file**, not a PEM string in `.env`. Keep the private key in Bitwarden as an **SSH key** item; materialize locally:

```bash
export BW_SESSION="$(bw unlock --raw)"
./scripts/vps-ssh-key-pull.sh          # → .ssh/artalk_vps (gitignored, 600)
# optional name override if the vault title differs:
# ./scripts/vps-ssh-key-pull.sh "VPS HOSTKET"
```

`.env` / `.env.example` only store the path:

```bash
VPS_SSH_KEY=.ssh/artalk_vps
```

Project MCP `vps-db` (`.cursor/mcp.json`) sets the same `env.VPS_SSH_KEY` so Cursor does not need a shell export. Details: `.cursor/rules/vps-db-query.mdc`.

Do **not** put the private key in Dropbox, commit `.ssh/`, or paste PEM into dotenvx.

## Docker / VPS

Leave server `.env` **plaintext**. `scripts/vps-deploy.sh` backs up and restores it around `git pull` so committed ciphertext does not become runtime env.

```bash
dotenvx decrypt   # local one-shot plaintext — do not commit
```

Never copy `.env.keys` into the Docker image (`.dockerignore` blocks `.env` / `.env.keys`).

## Protect commits (required)

**`dotenvx` is mandatory** on every developer PC. Without it: do not edit `.env`, do not commit. Install from [dotenvx.com](https://dotenvx.com) (macOS brew / winget / releases).

**Pre-commit hook must be installed** (not optional):

```bash
cp scripts/pre-commit-gitleaks.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

The hook runs gitleaks, then **`dotenvx protect`** (fails if dotenvx is missing), blocks `.env.keys`, then omerta. Do not use `git commit --no-verify` to bypass it.

Manual check:

```bash
dotenvx protect
```

CI: gitleaks + omerta (omerta temporarily hides tracked ciphertext `.env` — it is not a plaintext value source). Optional local leak values: `.env.local` (gitignored).

`python -m self_check` and GitHub Actions set `DOTENV_OPTIONAL` / `CI` so imports skip decrypt without Bitwarden. That does **not** relax the local commit gate. Running `python main.py` without a key still fails closed.
