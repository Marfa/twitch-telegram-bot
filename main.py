import logging

from bot import build_application
from config import DATABASE_PATH, DATABASE_URL, TELEGRAM_BOT_TOKEN, validate
from db import open_database
from health import mark_ready, start_health_server
from twitch import TwitchClient

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    start_health_server()
    validate()
    log = logging.getLogger(__name__)
    if DATABASE_URL:
        log.info("Using PostgreSQL (DATABASE_URL)")
    else:
        log.info("DATABASE_PATH=%s", DATABASE_PATH.resolve())
    db = open_database(DATABASE_PATH, DATABASE_URL)
    twitch = TwitchClient()
    app = build_application(TELEGRAM_BOT_TOKEN, db, twitch)
    mark_ready()
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
