#!/usr/bin/env python3
"""One-shot: sync stream-chat Chat Menu Button for a Telegram user_id."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram import Bot

from config import DATABASE_PATH, DATABASE_URL, TELEGRAM_BOT_TOKEN
from db import open_database
from handlers.settings import sync_stream_chat_menu_button


async def _run(user_id: int) -> None:
    db = open_database(DATABASE_PATH, DATABASE_URL)
    bot = Bot(TELEGRAM_BOT_TOKEN)
    async with bot:
        await sync_stream_chat_menu_button(bot, db, user_id)
    print(f"synced stream-chat menu button for {user_id}")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: sync-stream-chat-menu-button.py <telegram_user_id>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_run(int(sys.argv[1])))


if __name__ == "__main__":
    main()
