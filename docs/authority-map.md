# Authority map

Canonical sources of truth for humans and AI agents. When docs or rules conflict, this table wins.

| Domain | Canonical SoR | Derived / non-authoritative | Notes |
|---|---|---|---|
| Runtime behavior | `bot.py`, `handlers/`, `db/`, `twitch.py`, … | — | Deployed from `main` |
| UI strings (en/ru) | `locales/*.json` | `i18n.py` loader + keyboards; `docs/user-flow-map.ru.md` | Update the map when user-facing paths change |
| Premium entitlements | `premium.py` (`FEATURE_IDS`, `purchasable_feature_ids()`, gates) | Premium screens in `premium_handlers.py` | `purchasable_feature_ids()` hides ids still in alpha/beta; `active_subscription_slots()` / `may_enable_subscription()` — active-alert gate |
| Beta features (opt-in) | `beta/manifest.json` | `beta.py` runtime view | Lifecycle: `.github/workflows/beta-lifecycle.yml` |
| Beta enrollment state | DB (`beta_enrollments`) | — | Per-user opt-in |
| Config defaults | `config.py` | — | |
| Runtime secrets | `.env` (not in git) | `.env.example` | Contract for devs |
| User / subscription data | DB (`db/` schema) | — | SQLite local; Postgres on VPS |
| Env contract | `.env.example` | README Quick Start | |
| Deploy topology | `compose.yml`, `compose.vps.yml` | VPS scripts in `scripts/` | |
| Onboarding | `README.md`, `README.en.md` | — | Update both when product surface changes |
| Agent execution hints | `.cursor/rules/*.mdc` | — | Cursor adapter; not runtime authority |
| Characterization checks | `self_check/` (`python -m self_check`) | — | CI: `.github/workflows/self-check.yml` |
| Locale strings | `locales/en.json`, `locales/ru.json` | `i18n.py` loader + keyboards | |
| Static marketing assets | `assets/` | — | |
| Runtime analytics | PostHog (external) | `analytics.py` | Observations only — not product authority |
| AI templates («Мне повезёт») | Groq / HF APIs + DB pool | — | Runtime-generated |

## Public (GA) features — no separate manifest

`beta/manifest.json` tracks **opt-in beta** features: enrollment, 7-day lifecycle, GA promotion, beta issue labels.

Stable public capabilities do not need a parallel manifest:

- **Premium-gated:** `FEATURE_IDS` in `premium.py`
- **User-facing catalog:** README feature tables
- **Strings:** `i18n.py` keys

Add a GA feature registry only if you introduce gradual rollout or automated deprecation beyond beta — not required today.

## Cross-reference checks

| From | Must align with |
|---|---|
| `beta/manifest.json` → `premium_feature_id` | `premium.FEATURE_IDS` (+ hidden from à la carte via `purchasable_feature_ids()` until GA) |
| User-facing feature / menu change | `README.md` + `README.en.md` feature tables; `.cursor/rules/readme.mdc` |
| `docs/user-flow-map.ru.md` | `i18n.py` (ru) + `bot.py` handlers |
| `.cursor/rules/active-subscription-gate.mdc` | `premium.may_enable_subscription()` callers |
| `.env.example` | `config.py` env reads |

## External boundaries

| System | Role |
|---|---|
| Telegram Bot API | Delivery, payments (Stars) |
| Twitch Helix / EventSub / OAuth | Stream state, follows, schedule |
| PostgreSQL (VPS) | Production user state |
| PostHog | Usage analytics, error tracking |
| DeepL | Optional admin broadcast translation |
| Groq / Hugging Face | Optional «Мне повезёт» templates |
