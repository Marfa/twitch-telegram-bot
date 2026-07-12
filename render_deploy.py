"""Trigger Render deploy and notify admin via Telegram."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

RENDER_API = "https://api.render.com/v1"
DEFAULT_BLUEPRINT_ID = "REDACTED_BLUEPRINT_ID"
DEFAULT_SERVICE_ID = "REDACTED_SERVICE_ID"
POLL_INTERVAL_SEC = 15
DEPLOY_TIMEOUT_SEC = 900

_TERMINAL_OK = frozenset({"live"})
_TERMINAL_FAIL = frozenset(
    {"build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"}
)


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "render":
            os.environ.setdefault("RENDER_API_KEY", value)
        else:
            os.environ.setdefault(key, value)


def _render_api_key() -> str:
    key = os.getenv("RENDER_API_KEY", "").strip() or os.getenv("render", "").strip()
    if not key:
        raise RuntimeError("Missing Render API key (RENDER_API_KEY or render in .env)")
    return key


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def git_head_commit() -> str:
    out = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT
    )
    return out.strip()


def trigger_deploy(api_key: str, service_id: str, commit_id: str) -> str:
    resp = requests.post(
        f"{RENDER_API}/services/{service_id}/deploys",
        headers={**_headers(api_key), "Content-Type": "application/json"},
        json={"commitId": commit_id},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    deploy_id = data.get("id")
    if not deploy_id:
        raise RuntimeError(f"Render deploy response missing id: {data}")
    return deploy_id


def get_deploy(api_key: str, service_id: str, deploy_id: str) -> dict:
    resp = requests.get(
        f"{RENDER_API}/services/{service_id}/deploys/{deploy_id}",
        headers=_headers(api_key),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_deploy(api_key: str, service_id: str, deploy_id: str) -> dict:
    deadline = time.monotonic() + DEPLOY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        deploy = get_deploy(api_key, service_id, deploy_id)
        status = deploy.get("status", "")
        logger.info("Deploy %s status: %s", deploy_id, status)
        if status in _TERMINAL_OK | _TERMINAL_FAIL:
            return deploy
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"Deploy {deploy_id} did not finish within {DEPLOY_TIMEOUT_SEC}s")


def notify_admins(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN for admin notification")

    admin_raw = os.getenv("ADMIN_USER_IDS", "REDACTED_TELEGRAM_ID")
    admin_ids = [int(x.strip()) for x in admin_raw.split(",") if x.strip()]
    if not admin_ids:
        raise RuntimeError("ADMIN_USER_IDS is empty")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for admin_id in admin_ids:
        resp = requests.post(
            url,
            json={"chat_id": admin_id, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        resp.raise_for_status()


def _short_commit(commit_id: str) -> str:
    return commit_id[:7] if len(commit_id) >= 7 else commit_id


def run_deploy(commit_id: str, *, service_id: str, notify: bool = True) -> int:
    api_key = _render_api_key()
    service_id = service_id or os.getenv("RENDER_SERVICE_ID", DEFAULT_SERVICE_ID)

    logger.info("Triggering Render deploy for %s on %s", _short_commit(commit_id), service_id)
    deploy_id = trigger_deploy(api_key, service_id, commit_id)
    deploy = wait_for_deploy(api_key, service_id, deploy_id)
    status = deploy.get("status", "unknown")
    short = _short_commit(commit_id)

    if status in _TERMINAL_OK:
        message = (
            f"✅ Render: деплой успешен\n"
            f"Коммит: {short}\n"
            f"Deploy: {deploy_id}\n"
            f"Blueprint: https://dashboard.render.com/blueprint/{DEFAULT_BLUEPRINT_ID}"
        )
        logger.info("Deploy succeeded: %s", deploy_id)
        if notify:
            notify_admins(message)
        return 0

    message = (
        f"❌ Render: ошибка деплоя\n"
        f"Коммит: {short}\n"
        f"Статус: {status}\n"
        f"Deploy: {deploy_id}\n"
        f"Blueprint: https://dashboard.render.com/blueprint/{DEFAULT_BLUEPRINT_ID}"
    )
    logger.error("Deploy failed: %s status=%s", deploy_id, status)
    if notify:
        notify_admins(message)
    return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Deploy latest commit to Render")
    parser.add_argument(
        "--commit",
        default="",
        help="Git commit SHA (default: HEAD)",
    )
    parser.add_argument(
        "--service-id",
        default=os.getenv("RENDER_SERVICE_ID", DEFAULT_SERVICE_ID),
        help="Render service ID",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip Telegram notification to admin",
    )
    args = parser.parse_args(argv)

    commit_id = args.commit.strip() or git_head_commit()
    try:
        return run_deploy(commit_id, service_id=args.service_id, notify=not args.no_notify)
    except Exception as exc:
        short = _short_commit(commit_id)
        logger.exception("Deploy flow failed")
        if not args.no_notify:
            try:
                notify_admins(
                    f"❌ Render: не удалось задеплоить\n"
                    f"Коммит: {short}\n"
                    f"Ошибка: {exc}"
                )
            except Exception:
                logger.exception("Failed to notify admin about deploy error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
