# OWASP-oriented security checklist

Adapted for this Telegram bot (handlers + small HTTP surface), not a full web ASVS audit.

**Sources:** [OWASP Top 10](https://owasp.org/www-project-top-ten/), [OWASP API Security Top 10](https://owasp.org/www-project-api-security/), [OWASP ASVS](https://owasp.org/www-project-application-security-verification-standard/) (selective).

**When to run:** quarterly, before a major release, or after touching OAuth / webhooks / AuthZ / payments.

**How:** for each item mark `PASS` / `FAIL` / `RISK` / `N/A`, note evidence (`file:line` or command), and log the run below.

**Out of scope here:** SPA XSS/CSRF, cookie session fixation, classic form CSRF. Prefer manual/code review + existing CI (`pip-audit`, `gitleaks`, `self_check`).

---

## A. Attack surface map (refresh each run)

| # | Check | How |
|---|--------|-----|
| A1 | List HTTP routes in `health.py` (`do_GET` / `do_POST`) | Confirm auth on each mutating path |
| A2 | List mutating Telegram callback prefixes | Grep `CallbackQueryHandler` / `pattern=` in `bot.py` |
| A3 | Confirm public vs admin-only commands | `_is_admin` / `_can_use_admin_tools` |
| A4 | Note deploy exposure of `:8080` | `compose.vps.yml` ports + nginx assumptions |

---

## B. Broken Access Control (OWASP A01 / API1)

| # | Check | Expected |
|---|--------|----------|
| B1 | Subscription read/update/delete always scoped by `owner_id` | `get_subscription(id, owner_id)`; DB `WHERE id AND owner_id` |
| B2 | Handlers that use `get_subscription_by_id` re-check `sub.owner_id == caller` before mutate | No cross-user toggle/edit/delete |
| B3 | Share / copy / restore / import operate only on caller-owned rows (or capability token) | Share token = intentional share, not IDOR |
| B4 | Chat destination setup requires Telegram chat admin/owner | `_user_can_manage_chat` fail-closed |
| B5 | Admin callbacks gated | `_is_admin` / `_can_use_admin_tools` at handler entry |
| B6 | Premium feature toggles cannot be bypassed via crafted callbacks | `has_feature` on **all** paths that set gated fields (`edit_f` **and** `edit_set`) |
| B7 | Active-alert / alert-type gates on enable paths | `may_enable_subscription*` / `alert_type_entitled*` (see `.cursor/rules/active-subscription-gate.mdc`) |
| B8 | Mini App APIs require Telegram `initData` or HMAC chat token | `/app/chat/api/*` |

---

## C. Cryptographic failures / secrets (A02)

| # | Check | Expected |
|---|--------|----------|
| C1 | No secrets in git | `.env` gitignored; `gitleaks` CI green |
| C2 | Twitch refresh tokens encrypted at rest | `token_crypto` / `enc:v1:`; prefer dedicated `TOKEN_ENCRYPTION_KEY` in prod |
| C3 | Logs do not print access/refresh tokens or API keys | OAuth paths avoid `logger.exception` with tokens in locals; PostHog scrubbers |
| C4 | OAuth `state` unpredictable + short TTL + one-time | uuid4 / equivalent; pop-on-use; TTL ≤ 10–15 min |
| C5 | EventSub / PostHog / webhook secrets from env | Fail closed if missing where required |

---

## D. Injection (A03)

| # | Check | Expected |
|---|--------|----------|
| D1 | SQL parameterized; dynamic identifiers allowlisted | No user strings in SQL text |
| D2 | HTML alerts escape Twitch/user placeholders when `parse_mode=HTML` | `escape_html` on delivery path |
| D3 | Subprocess: no `shell=True`; argv lists; validate Twitch logins | `USERNAME_RE` / fixed args |
| D4 | No `eval` / `exec` / `pickle` of untrusted input | — |
| D5 | User-supplied regex (ignore keywords) cannot ReDoS the poll loop | timeout, literal-only, or complexity limits |
| D6 | Bot-side URL fetches (thumbnails, photo fallback) host-allowlisted or size-capped | Limit SSRF / large-body DoS |

---

## E. Insecure design / business logic (A04)

| # | Check | Expected |
|---|--------|----------|
| E1 | Share/clone does not grant Premium-only behavior to free users without a gate | Snapshot resets or gates advanced fields |
| E2 | Stars / gift / refund flows verify charge ownership and admin where needed | No self-refund for others |
| E3 | Free active-alert cap and total subscription cap enforced on create/enable | See active-subscription-gate rule |

---

## F. Security misconfiguration (A05)

| # | Check | Expected |
|---|--------|----------|
| F1 | Health body does not dump versions, env, stack traces | Short status strings only |
| F2 | HTTP listen / publish: prefer localhost host-bind if nginx fronts the bot | `127.0.0.1:8080:8080` or docker-network-only |
| F3 | Dependency audit CI | `pip-audit` on `requirements.txt` |
| F4 | Default/debug modes off in prod | No demo bypass of admin tools for non-admins |

---

## G. Vulnerable components (A06)

| # | Check | Expected |
|---|--------|----------|
| G1 | `pip-audit` clean (or accepted exceptions documented) | `.github/workflows/security.yml` |
| G2 | GitHub Dependabot / code-scanning / secret-scanning reviewed | Pre-push rule |

---

## H. Auth / webhook integrity (A07 / API2)

| # | Check | Expected |
|---|--------|----------|
| H1 | EventSub: HMAC-SHA256 over `id + timestamp + body`, `compare_digest` | Reject bad sig |
| H2 | EventSub: reject stale / future timestamps (no fail-open on parse error) | Freshness required |
| H3 | EventSub: message-id replay protection | Bounded dedupe OK if documented |
| H4 | PostHog webhook: shared secret; prefer Bearer over `?token=` | Fail closed if secret empty |
| H5 | OAuth callbacks bind `state` → telegram user + purpose | Twitch vs DonationAlerts purpose check |
| H6 | Inbound HTTP rate limits on `/hooks/*` and OAuth (nginx or app) | Mitigate DoS / brute |

---

## I. Integrity / supply chain (A08)

| # | Check | Expected |
|---|--------|----------|
| I1 | CI installs pinned deps from `requirements.txt` | No floating latest in prod image |
| I2 | Workflows use least privilege (`permissions:`) | — |

---

## J. Logging / monitoring (A09)

| # | Check | Expected |
|---|--------|----------|
| J1 | Auth failures on webhooks logged without secrets | 401/403 visible in ops |
| J2 | Analytics/export redacts secrets | `analytics.py` scrubbers |
| J3 | Health / Actions catch bot dead states | `/health` + compose healthcheck |

---

## K. SSRF / unsafe egress (A10 / API)

| # | Check | Expected |
|---|--------|----------|
| K1 | Custom button URLs are client-side only (or validated scheme) | Bot does not fetch arbitrary user URLs for buttons |
| K2 | Image/thumbnail download hosts restricted when bot fetches | Twitch CDN / Helix allowlist preferred |

---

## Quick commands

```bash
# Deps + secrets (CI mirrors)
pip-audit -r requirements.txt
# gitleaks via Actions; locally if installed:
# gitleaks detect --source . --no-git

# Mutating callbacks registration
rg -n "CallbackQueryHandler|pattern=" bot.py | head -80

# Owner-scoped DB API
rg -n "def get_subscription|def update_subscription|def toggle_subscription" db/

# HTTP routes
rg -n "path == |startswith\(|/hooks/|/oauth/" health.py

# Shell / eval
rg -n "shell=True|eval\(|exec\(|pickle\." --glob "*.py"

# Premium gate on edit paths
rg -n "edit_set:|has_feature.*delete_prev|delete_previous" handlers/subscriptions.py
```

---

## Run log

### 2026-09-22 — initial pass (code review)

| ID | Result | Notes |
|----|--------|-------|
| A1–A4 | PASS | Single `ThreadingHTTPServer` in `health.py`; compose publishes `8080:8080` (see F2) |
| B1–B5 | PASS | Owner in DB API; chat admin fail-closed; admin gates present |
| B6 | **PASS** (fixed 2026-09-22) | `on_edit_set` gates `delete_old` / `delete_fail` / `delete_other` with `has_feature("delete_prev")` — same as `edit_f` |
| B7 | PASS | Enable paths use subscription gates (spot-checked) |
| B8 | PASS | Mini App APIs use initData / HMAC |
| C1–C5 | PASS | Tokens Fernet; OAuth state uuid4 + 600s TTL + pop; residual: prefer dedicated `TOKEN_ENCRYPTION_KEY` |
| D1–D4 | PASS | Parameterized SQL; no `shell=True`; no untrusted eval/pickle |
| D5 | **RISK** | `should_ignore_stream` compiles user ignore-keywords as regex (`twitch.py` ~2432–2450) — ReDoS on poll |
| D6 / K2 | **RISK** | Photo/thumbnail HTTP fetch without host allowlist (`handlers/delivery.py`, `stream_capture.py`) |
| E1 | **RISK** | Share clone resets pin/delete/dest but still copies delay/ignore/custom_buttons/etc. until edit resets advanced |
| E2–E3 | PASS | Spot-checked; follow active-subscription-gate on future changes |
| F1 | PASS | Health returns short status strings |
| F2 | **RISK** | `0.0.0.0` + host `"8080:8080"` — harden if public without nginx |
| F3–F4 | PASS | `security.yml` pip-audit + gitleaks |
| G1–G2 | N/A this run | Re-check alerts before next push per pre-push rule |
| H1 | PASS | EventSub HMAC + `compare_digest` |
| H2 | **RISK** | `timestamp_fresh` returns `True` if RFC3339 parse fails (`eventsub.py` ~111–115) |
| H3 | PASS | In-memory dedupe (max 500) |
| H4 | PASS | PostHog fail-closed; prefer Bearer over `?token=` |
| H5 | PASS | Purpose-bound OAuth state |
| H6 | **PASS** (fixed 2026-09-22) | In-app sliding window: `/oauth/*` 60/min/IP, `/hooks/*` 300/min/IP → 429 + `Retry-After` (`health.py`) |
| I1–I2 | PASS | Spot-checked workflow permissions |
| J1–J3 | PASS | — |
| K1 | PASS | Custom buttons validated for client use |

**Priority follow-ups (not fixed in this pass):**

1. Harden EventSub timestamp: fail closed on unparseable `Twitch-Eventsub-Message-Timestamp`.
2. ReDoS: treat ignore-keywords as literal substrings, or add timeout/complexity limits.
3. Deploy: bind publish `127.0.0.1:8080:8080` (or drop host publish) + nginx `limit_req` still useful in front of the in-app limiter.
4. Optional: CDN/Helix allowlist for bot-side image downloads; strip advanced fields on share accept for free users.

**Fixed in follow-up (same day):**

1. Gate `on_edit_set` delete_* with `has_feature("delete_prev")`.
2. Inbound HTTP rate limits on `/hooks/*` and `/oauth/*`.

---

## Scoring guide

| Result | Meaning |
|--------|---------|
| PASS | Control present and looks correct |
| FAIL | Missing or clearly bypassable |
| RISK | Partial / defense-in-depth / deploy-dependent |
| N/A | Not applicable this run |

Do not mark PASS for “UI hides the button” if a crafted callback still works.
