#!/usr/bin/env python3
"""Bump pinned streamlink in requirements.txt to the latest PyPI release.

Exit codes:
  0 — requirements updated (or --check with an update available)
  1 — error
  2 — already up to date (--check: no changes)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("sync-streamlink")

PACKAGE = "streamlink"
PYPI_JSON = f"https://pypi.org/pypi/{PACKAGE}/json"
GITHUB_REPO = "streamlink/streamlink"

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
_PIN_RE = re.compile(rf"^{re.escape(PACKAGE)}==([^\s#]+)\s*$", re.M)


def _http_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "twitch-telegram-bot-sync-streamlink",
    }
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_http_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} → {exc.code}: {body}") from exc


def _latest_pypi_version() -> str:
    raw = _http_get(PYPI_JSON)
    data = json.loads(raw.decode("utf-8"))
    version = (data.get("info") or {}).get("version")
    if not version or not isinstance(version, str):
        raise RuntimeError(f"PyPI JSON missing info.version for {PACKAGE}")
    return version.strip()


def _read_pinned_version(text: str) -> str | None:
    match = _PIN_RE.search(text)
    return match.group(1) if match else None


def _write_pin(text: str, version: str) -> str:
    replacement = f"{PACKAGE}=={version}"
    if _PIN_RE.search(text):
        return _PIN_RE.sub(replacement, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + replacement + "\n"


def sync(*, check_only: bool) -> int:
    remote = _latest_pypi_version()
    text = REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.is_file() else ""
    current = _read_pinned_version(text)

    if current == remote:
        log.info("up to date at %s", remote)
        return 2

    if check_only:
        log.info(
            "update available: local=%s remote=%s (https://github.com/%s)",
            current or "(none)",
            remote,
            GITHUB_REPO,
        )
        return 0

    updated = _write_pin(text, remote)
    REQUIREMENTS.write_text(updated, encoding="utf-8", newline="\n")
    log.info("updated %s: %s → %s", REQUIREMENTS.name, current or "(none)", remote)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether an update is available; do not write files",
    )
    args = parser.parse_args()
    try:
        return sync(check_only=args.check)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
