import logging

from bot import build_application
from config import DATABASE_PATH, TELEGRAM_BOT_TOKEN, validate
from db import Database
from health import start_health_server
from twitch import TwitchClient

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    start_health_server()
    validate()
    db = Database(DATABASE_PATH)
    twitch = TwitchClient()
    app = build_application(TELEGRAM_BOT_TOKEN, db, twitch)
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
