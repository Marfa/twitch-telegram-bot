"""Orchestrate characterization self-checks."""
import os

from .checks_core import check_core
from .checks_db_premium import check_db_premium
from .checks_handlers import check_handlers


def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_SELF_CHECK")
    check_core()
    check_db_premium()
    check_handlers()
    print("ok")


if __name__ == "__main__":
    main()
