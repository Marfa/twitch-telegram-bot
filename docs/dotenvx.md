# dotenvx + Bitwarden (local secrets)

Sync encrypted `.env` between Windows / macOS without putting the private key in git.
Production VPS keeps a **plaintext** `.env` next to compose (existing deploy scripts).

Cursor agent rules: `.cursor/rules/dotenvx-bitwarden.mdc` (always on) · critical key sync: `.cursor/rules/twitch-client-secret.mdc`.

Official docs: [dotenvx](https://dotenvx.com/) · [Bitwarden custody](https://dotenvx.com/docs/custody/bitwarden/)

## Security rules (this public repo)

| Keep out of git | OK locally / in Bitwarden |
|---|---|
| `.env.keys`, plaintext prod secrets | Encrypted `.env` values (`encrypted:…`) |
| Private key in chat / commits | `DOTENV_PRIVATE_KEY` item in Bitwarden |

Do **not** push real production ciphertext to the public GitHub remote unless you accept that anyone with the Bitwarden key (or a leaked key) can decrypt every historical commit. Prefer syncing the encrypted `.env` between your machines via Bitwarden Secure Note / private channel, or a private remote.

`.env.example` stays the public contract. Docker/`compose*.yml` still use plaintext `env_file: .env`.

## One-time setup (PC #1)

1. Install tools  
   - **dotenvx:** Windows `winget install dotenvx.dotenvx` · macOS `brew install dotenvx/brew/dotenvx` · or https://dotenvx.sh  
   - **Bitwarden CLI:** `bw` ([install](https://bitwarden.com/help/cli/)) → `bw login`

2. Create local secrets from the template (if needed):

```bash
cp .env.example .env
# fill TELEGRAM_BOT_TOKEN, TWITCH_*, …
```

3. Encrypt and move the private key into Bitwarden:

```bash
dotenvx encrypt
dotenvx bitwarden up
```

`bitwarden up` creates a vault item, verifies it, and **removes** the key from `.env.keys`. Keep Bitwarden as the only copy of the private key (plus Bitwarden backup / emergency kit).

4. Run the bot (dotenvx unlocks via `bw`):

```bash
dotenvx run -- python main.py
```

Or use the helper scripts: `scripts/env-run.sh` / `scripts/env-run.ps1`.

Non-interactive (CI / scripted shell):

```bash
export BW_SESSION="$(bw unlock --raw)"
dotenvx run -- python main.py
# or:
DOTENV_PRIVATE_KEY="$(bw get password DOTENV_PRIVATE_KEY)" dotenvx run -- python main.py
```

## Second machine (Windows or macOS)

1. Install `dotenvx` + `bw`, `bw login` (same Bitwarden account).
2. Copy the **encrypted** `.env` from PC #1 (git private remote, Bitwarden note, USB — not plaintext).
3. Either:
   - `dotenvx run -- python main.py` (pulls key from Bitwarden when custody was set with `bitwarden up`), or  
   - `dotenvx bitwarden pull` once to materialize `.env.keys` (still gitignored), then `python main.py`.

`config.py` / `dotenv_loader.py` call `dotenvx decrypt --stdout` when they see `encrypted:` values, so a plain `python main.py` also works if the key is available via env, `.env.keys`, or Bitwarden-backed dotenvx.

## Update a secret

```bash
dotenvx set SOME_KEY "new-value"
# sync encrypted .env to the other PC; key stays in Bitwarden
```

## Docker / VPS

Leave server `.env` **plaintext** (current `scripts/vps-deploy.sh` / compose). Decrypt on a trusted machine if you need a one-shot plaintext export:

```bash
dotenvx decrypt   # writes plaintext .env — do not commit
```

Never copy `.env.keys` into the Docker image (`.dockerignore` blocks it).

## Protect commits

```bash
# optional local guard (blocks unencrypted secret commits when dotenvx is installed)
dotenvx protect
```

CI already runs gitleaks + omerta. Local hook: `scripts/pre-commit-gitleaks.sh` (also calls `dotenvx protect` when available).
