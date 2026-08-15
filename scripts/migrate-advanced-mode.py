#!/usr/bin/env python3
"""One-off / repeatable: set users.advanced_mode from product defaults."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DATABASE_PATH, DATABASE_URL
from db import open_database
from premium import migrate_advanced_mode_defaults

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Set advanced_mode ON only for Premium-entitled users who already "
            "use ignore/delay/repeat/delete on an alert; everyone else OFF."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count changes without writing to the database",
    )
    args = parser.parse_args()
    db = open_database(DATABASE_PATH, DATABASE_URL)
    examined, set_on, set_off = migrate_advanced_mode_defaults(
        db, dry_run=args.dry_run
    )
    mode = "would set" if args.dry_run else "set"
    log.info(
        "examined %s; %s on: %s; %s off: %s",
        examined,
        mode,
        set_on,
        mode,
        set_off,
    )


if __name__ == "__main__":
    main()
