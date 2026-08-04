"""In-process admin Demo mode: free-tier UX with disposable demo subscriptions."""
from __future__ import annotations

_active: set[int] = set()


def is_active(user_id: int) -> bool:
    return user_id in _active


def activate(user_id: int) -> None:
    _active.add(user_id)


def deactivate(user_id: int) -> None:
    _active.discard(user_id)
