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
        if key == "render":
            os.environ.setdefault("RENDER_API_KEY", value)
        else:
            os.environ.setdefault(key, value)


load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MAX_SUBSCRIPTIONS_PER_OWNER = int(os.getenv("MAX_SUBSCRIPTIONS_PER_OWNER", "25"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bot.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or None
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
# Public HTTPS origin for Twitch OAuth redirect (Render sets RENDER_EXTERNAL_URL).
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
)


def twitch_oauth_redirect_uri() -> str:
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/oauth/twitch/callback"


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
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "llama-3.1-8b-instant").strip() or (
    "llama-3.1-8b-instant"
)
BOT_VERSION = (
    os.getenv("RENDER_GIT_COMMIT")
    or os.getenv("BOT_VERSION")
    or "dev"
).strip() or "dev"


def validate() -> None:
    _require("TELEGRAM_BOT_TOKEN")
    _require("TWITCH_CLIENT_ID")
    _require("TWITCH_CLIENT_SECRET")
