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
STATUS_CHECK_INTERVAL = int(os.getenv("STATUS_CHECK_INTERVAL", "1800"))
RENDER_STATUS_RSS = os.getenv(
    "RENDER_STATUS_RSS", "https://status.render.com/history.rss"
)
AIVEN_STATUS_RSS = os.getenv(
    "AIVEN_STATUS_RSS", "https://status.aiven.io/feed.rss"
)
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bot.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or None
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()


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
BOT_VERSION = (
    os.getenv("RENDER_GIT_COMMIT")
    or os.getenv("BOT_VERSION")
    or "dev"
).strip() or "dev"


def validate() -> None:
    _require("TELEGRAM_BOT_TOKEN")
    _require("TWITCH_CLIENT_ID")
    _require("TWITCH_CLIENT_SECRET")
