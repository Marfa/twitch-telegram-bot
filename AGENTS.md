# Agent / contributor rules

## Runtime

Production image is `python:3.12-slim` (see `Dockerfile`). Local Python must be **>= 3.10** so security-pinned packages (`requests>=2.33`, `urllib3>=2.7`) install.

## Dependencies (Python)

This repo uses `requirements.txt` (not npm). Treat dependency hygiene as mandatory.

1. **Before adding a package** — run the `/check-dep` skill (registry card, repo activity, advisories, typosquatting).
2. **Install only explicit current versions** — resolve from PyPI at install time, do not invent versions from memory:
   ```bash
   pip index versions <package>   # or: pip install <package>==  # lists candidates
   pip install "<package>==<chosen>"
   pip freeze | grep -i "^<package>=="
   ```
   Pin the chosen version in `requirements.txt`.
3. **Immediately after any install/upgrade**:
   ```bash
   pip-audit -r requirements.txt
   ```
   Do not leave known critical/high findings unfixed.
4. **Regularly check staleness** (local):
   ```bash
   pip list --outdated
   ```
5. **CI gates prod** — `.github/workflows/security.yml` runs `pip-audit` and `gitleaks` on every push/PR. Do not weaken those checks.

## Secrets

- Never commit `.env` or real tokens. Use `.env.example` for placeholders only.
- `gitleaks` must pass locally before commit (see below) and in CI.

## Local gitleaks pre-commit

```bash
# one-time (requires: brew install gitleaks)
cp scripts/pre-commit-gitleaks.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

The hook runs `gitleaks protect --staged` and blocks the commit on findings.
