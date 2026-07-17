from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

_ALLOWED_RSS_HOSTS = frozenset({"status.render.com", "status.aiven.io"})

_PLANNED_RE = re.compile(
    r"scheduled|maintenance\s+period|this\s+is\s+a\s+scheduled\s+event",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(r"Status:\s*(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class StatusItem:
    guid: str
    title: str
    description: str
    link: str


def assert_safe_rss_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_RSS_HOSTS
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"Refusing non-allowlisted RSS URL: {url!r}")
    return url.strip()


def _plain_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_render_status(url: str) -> list[StatusItem]:
    assert_safe_rss_url(url)
    resp = requests.get(url, timeout=20, allow_redirects=False)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    items: list[StatusItem] = []
    for node in root.findall("./channel/item"):
        guid = (node.findtext("guid") or node.findtext("link") or "").strip()
        title = (node.findtext("title") or "").strip()
        if not guid or not title:
            continue
        items.append(
            StatusItem(
                guid=guid,
                title=title,
                description=_plain_text(node.findtext("description") or ""),
                link=(node.findtext("link") or guid).strip(),
            )
        )
    return items


def is_planned_maintenance(item: StatusItem) -> bool:
    haystack = f"{item.title} {item.description}"
    return bool(_PLANNED_RE.search(haystack))


def is_aiven_outage(item: StatusItem) -> bool:
    match = _STATUS_RE.search(f"{item.title} {item.description}")
    if not match:
        return False
    return match.group(1).lower() != "resolved"
