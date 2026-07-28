#!/usr/bin/env python3
"""One-off: update import/sync subscriptions to the current default template."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import migrate_import_sync_subscriptions
from config import DATABASE_PATH, DATABASE_URL
from db import open_database

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate import/sync subscriptions to the current default template."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count changes without writing to the database",
    )
    args = parser.parse_args()
    db = open_database(DATABASE_PATH, DATABASE_URL)
    templates, previews = migrate_import_sync_subscriptions(db, dry_run=args.dry_run)
    mode = "would update" if args.dry_run else "updated"
    log.info(
        "%s templates: %s; %s link preview off: %s",
        mode.capitalize(),
        templates,
        mode,
        previews,
    )


if __name__ == "__main__":
    main()
