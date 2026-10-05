"""Shared PostHog HogQL helper (Personal API key).

Import from other scripts, or use via ``scripts/posthog-query.py``.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Query API host (app), not the ingest host (POSTHOG_HOST / us.i.posthog.com).
DEFAULT_QUERY_HOST = "https://us.posthog.com"
DEFAULT_PROJECT_ID = "554824"


def load_env_file(path: Path | None = None) -> None:
    """Set missing keys from KEY=value .env (no shell source)."""
    env_path = path or (ROOT / ".env")
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = val.strip().strip("'").strip('"')


def personal_api_key() -> str:
    return (os.environ.get("POSTHOG_API_KEY_PERSONAL") or "").strip()


def project_id() -> str:
    return (
        os.environ.get("POSTHOG_PROJECT_ID") or DEFAULT_PROJECT_ID
    ).strip() or DEFAULT_PROJECT_ID


def query_host() -> str:
    # Prefer explicit query host; fall back to app US. Do not use ingest host.
    raw = (os.environ.get("POSTHOG_QUERY_HOST") or "").strip()
    if raw:
        return raw.rstrip("/")
    return DEFAULT_QUERY_HOST


def hogql(
    query: str,
    *,
    timeout: float = 120,
    api_key: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Run a HogQL query. Returns PostHog JSON (columns + results).

    Raises SystemExit-friendly RuntimeError on missing key / HTTP errors.
    """
    key = (api_key if api_key is not None else personal_api_key()).strip()
    if not key:
        raise RuntimeError("POSTHOG_API_KEY_PERSONAL unset")
    pid = (project if project is not None else project_id()).strip()
    url = f"{query_host()}/api/projects/{pid}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"PostHog HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PostHog unreachable: {exc}") from exc
    return payload


def hogql_rows(query: str, **kwargs: Any) -> tuple[list[str] | None, list[list[Any]]]:
    payload = hogql(query, **kwargs)
    cols = payload.get("columns")
    rows = payload.get("results") or payload.get("result") or []
    if not isinstance(rows, list):
        rows = []
    return cols if isinstance(cols, list) else None, rows
