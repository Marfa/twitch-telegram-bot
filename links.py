from __future__ import annotations

import re
from dataclasses import dataclass

# t.me/c/1234567890/30 или t.me/c/themarfa_gaming/30
TG_C_TOPIC_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/c/([A-Za-z0-9_]+)/(\d+)(?:/\d+)?",
    re.IGNORECASE,
)
# t.me/groupname/30 (публичная группа с темами)
TG_PUBLIC_TOPIC_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?!c/)([A-Za-z][A-Za-z0-9_]{3,})/(\d+)(?:/\d+)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TelegramTopicLink:
    chat_ref: str
    thread_id: int


def parse_telegram_topic_link(text: str) -> TelegramTopicLink | None:
    text = text.strip()
    match = TG_C_TOPIC_RE.search(text) or TG_PUBLIC_TOPIC_RE.search(text)
    if not match:
        return None
    return TelegramTopicLink(chat_ref=match.group(1), thread_id=int(match.group(2)))


def chat_ref_to_id(ref: str) -> int | None:
    ref = ref.strip().lstrip("@")
    if ref.isdigit():
        num = int(ref)
        return int(f"-100{num}") if num > 0 else num
    return None
