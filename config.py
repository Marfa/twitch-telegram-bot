from __future__ import annotations

import os
from pathlib import Path


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
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bot.db"))


def validate() -> None:
    _require("TELEGRAM_BOT_TOKEN")
    _require("TWITCH_CLIENT_ID")
    _require("TWITCH_CLIENT_SECRET")
