#!/usr/bin/env python3
"""Send a test live-DM alert with 🎉 message_effect_id to ADMIN_USER_IDS."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram import Bot

from config import ADMIN_USER_IDS, TELEGRAM_BOT_TOKEN
from handlers.delivery import LIVE_DM_MESSAGE_EFFECT_ID, _deliver_alert_content


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not ADMIN_USER_IDS:
        raise SystemExit("ADMIN_USER_IDS is not set")

    bot = Bot(TELEGRAM_BOT_TOKEN)
    text = (
        "🎉 Тест live-оповещения в ЛС\n"
        f"(message_effect_id={LIVE_DM_MESSAGE_EFFECT_ID}, только live + dm)"
    )
    for admin_id in ADMIN_USER_IDS:
        msg = await _deliver_alert_content(
            bot,
            chat_id=admin_id,
            text=text,
            message_effect_id=LIVE_DM_MESSAGE_EFFECT_ID,
        )
        print(f"sent live-dm effect test to {admin_id} message_id={msg.message_id}")


if __name__ == "__main__":
    asyncio.run(main())
