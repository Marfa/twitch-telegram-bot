"""Normalize free-text game/category search queries (punctuation-tolerant)."""

from __future__ import annotations

import re

# Letters/digits across Latin + Cyrillic; punctuation/colons become separators.
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


def search_tokens(query: str) -> list[str]:
    """Split query into alphanumeric tokens (lowercased)."""
    return [m.group(0).casefold() for m in _TOKEN_RE.finditer(query or "")]


def normalize_search_query(query: str) -> str:
    """Collapse punctuation: 'Worms: Galactic Tactics' → 'worms galactic tactics'."""
    return " ".join(search_tokens(query))
