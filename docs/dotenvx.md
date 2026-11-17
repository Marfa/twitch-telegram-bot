# dotenvx + Bitwarden (local secrets)

Encrypted `.env` on Windows / macOS; private key in Bitwarden (not git).
Cross-device ciphertext sync: **Resilio** folder (on this Mac: `~/Dropbox/.env`).
Production VPS keeps a **plaintext** `.env` next to compose (existing deploy scripts).

Cursor agent rules: `.cursor/rules/dotenvx-bitwarden.mdc` (always on) · critical key sync: `.cursor/rules/twitch-client-secret.mdc`.

Official docs: [dotenvx](https://dotenvx.com/) · [Bitwarden custody](https://dotenvx.com/docs/custody/bitwarden/)

## Security rules (this public repo)

| Keep out of git / Resilio | OK |
|---|---|
| `.env.keys`, plaintext prod secrets, `BW_SESSION` | Encrypted `.env` values (`encrypted:…`) in Resilio |
| Private key in chat / commits | Bitwarden login item (see naming below) |

**Bitwarden item name:** dotenvx always creates `dotenvx (02A 49E)`-style titles (short public key). That is correct — do **not** rename to `DOTENV_PRIVATE_KEY`. `DOTENV_PRIVATE_KEY` is only the field name inside `.env.keys` / process env, not the vault item title. `bitwarden pull`/`run` use a local custody UUID reference, not the display name.

Do **not** push real production ciphertext to the public GitHub remote unless you accept that anyone with the Bitwarden key (or a leaked key) can decrypt every historical commit.

`.env.example` stays the public contract. Docker/`compose*.yml` still use plaintext `env_file: .env`.

## Resilio sync (source of truth for ciphertext)

| Role | What |
|---|---|
| SoR | Encrypted `.env` in the Resilio-shared folder |
| This Mac | `/Users/Blogger/Dropbox/.env` (`~/Dropbox` — Resilio, not Dropbox Inc.) |
| Other PCs | Same Resilio share; **ask for the local path** if `~/Dropbox` is missing |
| Never sync | `.env.keys`, plaintext dumps |

Workflow:

1. **Pull** current ciphertext into the repo: `cp -f ~/Dropbox/.env .env` (or the path the user gave).
2. Edit with `dotenvx set KEY "value"` (needs matching private key via Bitwarden / `.env.keys`).
3. **Publish** back: `cp -f .env ~/Dropbox/.env` so other devices get the update via Resilio.
4. VPS plaintext remains a separate step ([`twitch-client-secret.mdc`](.cursor/rules/twitch-client-secret.mdc)).

## One-time setup (PC #1)

1. Install tools  
   - **dotenvx:** Windows `winget install dotenvx.dotenvx` · macOS `brew install dotenvx/brew/dotenvx` · or binary from [GitHub releases](https://github.com/dotenvx/dotenvx/releases)  
   - **Bitwarden CLI:** `bw` ([install](https://bitwarden.com/help/cli/)) → `bw login`  
   - **Resilio Sync** sharing the Dropbox-named folder (already used on this Mac)

2. Create local secrets from the template (if needed):

```bash
cp .env.example .env
# fill TELEGRAM_BOT_TOKEN, TWITCH_*, …
```

3. Encrypt and move the private key into Bitwarden:

```bash
dotenvx encrypt
export BW_SESSION="$(bw unlock --raw)"
dotenvx bitwarden up
```

`bitwarden up` creates a personal vault item named like `dotenvx (02A 49E)` (short public key), stores a **local** custody reference, verifies, and **removes** the key from `.env.keys`.

Note: `dotenvx bitwarden pull` only works when that local reference already exists on this machine. On a new PC, use `bw get password <item-id>` into `.env.keys` (do not print the key), or run `bitwarden up` once after a local encrypt.

4. Publish ciphertext to Resilio and run:

```bash
cp -f .env ~/Dropbox/.env
dotenvx run -- python main.py
```

Helpers: `scripts/env-run.sh` / `scripts/env-run.ps1`.

## Second machine (Windows or macOS)

1. Install `dotenvx` + `bw`, `bw login` (same Bitwarden account). Ensure Resilio has the shared folder.
2. If the Resilio path is not `~/Dropbox`, **ask / note the local path**.
3. `cp -f "$RESILIO_ENV" .env` (encrypted only).
4. Materialize the private key (Bitwarden item / `bw get password <id>` → `.env.keys`, or custody `up`/`pull` when the local reference exists).
5. `dotenvx run -- python main.py`.

`config.py` / `dotenv_loader.py` call `dotenvx decrypt --stdout` when they see `encrypted:` values.

## Update a secret

```bash
cp -f ~/Dropbox/.env .env          # start from SoR
dotenvx set SOME_KEY "new-value"
cp -f .env ~/Dropbox/.env          # publish ciphertext
# then sync VPS plaintext if the key is critical (twitch-client-secret.mdc)
```

## Rotate the keypair (after key leak)

```bash
cp -f ~/Dropbox/.env .env
dotenvx decrypt -f .env --stdout > /tmp/env-plain.env   # umask 077; delete ASAP
# replace .env with plaintext (no DOTENV_PUBLIC_KEY); remove .env.keys
dotenvx encrypt
export BW_SESSION="$(bw unlock --raw)"
dotenvx bitwarden up               # new vault item; delete the old leaked item in Bitwarden UI
cp -f .env ~/Dropbox/.env
```

Old ciphertext encrypted with a leaked private key is obsolete once Dropbox/Resilio has the new file — delete leftover Finder copies (`— копия.env`, etc.).

## Docker / VPS

Leave server `.env` **plaintext** (current `scripts/vps-deploy.sh` / compose). Decrypt on a trusted machine if you need a one-shot plaintext export:

```bash
dotenvx decrypt   # writes plaintext .env — do not commit / do not put in Resilio
```

Never copy `.env.keys` into the Docker image (`.dockerignore` blocks it).

## Protect commits

```bash
dotenvx protect
```

CI already runs gitleaks + omerta. Local hook: `scripts/pre-commit-gitleaks.sh` (also calls `dotenvx protect` when available).
