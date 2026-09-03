from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        os.environ.setdefault(key, value)


load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
# Schedule reminders need one Helix call per channel; poll slower than live checks.
SCHEDULE_CHECK_INTERVAL = int(os.getenv("SCHEDULE_CHECK_INTERVAL", "180"))
MAX_SUBSCRIPTIONS_PER_OWNER = int(os.getenv("MAX_SUBSCRIPTIONS_PER_OWNER", "25"))
# Product kill switches. Default off in source (OSS / local): Premium + Help unlock
# all paid features; Partner is fully unavailable. Set to 1 on VPS for production.
ENABLE_PREMIUM = _env_bool("ENABLE_PREMIUM", False)
ENABLE_HELP = _env_bool("ENABLE_HELP", False)
ENABLE_PARTNER = _env_bool("ENABLE_PARTNER", False)
PREMIUM_FREE_ACTIVE_LIMIT = int(os.getenv("PREMIUM_FREE_ACTIVE_LIMIT", "5"))
PREMIUM_STARS_AMOUNT = int(os.getenv("PREMIUM_STARS_AMOUNT", "100"))
PREMIUM_STARS_YEAR = int(os.getenv("PREMIUM_STARS_YEAR", "1000"))
PREMIUM_STARS_LIFETIME = int(os.getenv("PREMIUM_STARS_LIFETIME", "2000"))
PREMIUM_STARS_FEATURE = int(os.getenv("PREMIUM_STARS_FEATURE", "20"))
PREMIUM_CHANNEL_STARS = int(os.getenv("PREMIUM_CHANNEL_STARS", "1500"))
PREMIUM_TRIAL_DAYS = int(os.getenv("PREMIUM_TRIAL_DAYS", "7"))
PREMIUM_SUBSCRIPTION_PERIOD = int(os.getenv("PREMIUM_SUBSCRIPTION_PERIOD", "2592000"))
PREMIUM_YEAR_SECONDS = int(os.getenv("PREMIUM_YEAR_SECONDS", str(365 * 24 * 3600)))
PREMIUM_TWITCH_LOGIN = (os.getenv("PREMIUM_TWITCH_LOGIN", "marfapr") or "marfapr").strip().lower()
_free_chat = os.getenv("FREE_CHAT_ID", "-1002155969539").strip()
FREE_CHAT_ID: int | None = int(_free_chat) if _free_chat else None
REFERRAL_COMMISSION_PERCENT = int(os.getenv("REFERRAL_COMMISSION_PERCENT", "10"))
REFERRAL_WITHDRAW_MIN_STARS = int(os.getenv("REFERRAL_WITHDRAW_MIN_STARS", "500"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bot.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or None
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def paid_features_free() -> bool:
    """True when Premium and/or Help is off — all paid capabilities unlocked."""
    return not ENABLE_PREMIUM or not ENABLE_HELP


def show_premium_ui() -> bool:
    """Premium shop/button only when monetization is fully on."""
    return ENABLE_PREMIUM and ENABLE_HELP


def show_help_button() -> bool:
    return ENABLE_HELP


def show_partner_ui() -> bool:
    return ENABLE_PARTNER


def twitch_oauth_redirect_uri() -> str:
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/oauth/twitch/callback"


def twitch_eventsub_callback_url() -> str:
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/hooks/eventsub"


def parse_admin_user_ids(raw: str | None = None) -> frozenset[int]:
    source = os.getenv("ADMIN_USER_IDS", "") if raw is None else raw
    ids: list[int] = []
    for part in source.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return frozenset(ids)


ADMIN_USER_IDS = parse_admin_user_ids()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
HF_TOKEN = (
    os.getenv("HF_TOKEN", "").strip()
    or os.getenv("HUGGING_FACE_API", "").strip()
)
HF_TEXT_MODEL = os.getenv("HF_TEXT_MODEL", "Qwen/Qwen2.5-7B-Instruct").strip() or (
    "Qwen/Qwen2.5-7B-Instruct"
)
GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY", "").strip()
    or os.getenv("GROQ_API", "").strip()
    or os.getenv("GROK_API", "").strip()
)
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-20b").strip() or (
    "openai/gpt-oss-20b"
)
BOT_VERSION = (os.getenv("BOT_VERSION") or "dev").strip() or "dev"
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY", "").strip()
# US default; EU projects: https://eu.i.posthog.com
POSTHOG_HOST = (
    os.getenv("POSTHOG_HOST", "").strip() or "https://us.i.posthog.com"
).rstrip("/")
# Shared secret for PostHog → bot Issue alerts (Bearer / query). Empty = endpoint off.
POSTHOG_ISSUE_WEBHOOK_SECRET = os.getenv("POSTHOG_ISSUE_WEBHOOK_SECRET", "").strip()
# Personal API key for polling Inbox reports API. Empty = polling off.
POSTHOG_PERSONAL_API_KEY = os.getenv("POSTHOG_API_KEY_PERSONAL", "").strip()
POSTHOG_PROJECT_ID = os.getenv("POSTHOG_PROJECT_ID", "554824").strip()


def validate() -> None:
    _require("TELEGRAM_BOT_TOKEN")
    _require("TWITCH_CLIENT_ID")
    _require("TWITCH_CLIENT_SECRET")
