"""Last successful outbound DM per user — context for bot_blocked.

In-memory; resets on restart. Blocks usually follow the last DM within
minutes, so a restart loses only a small diagnostic tail.

Kinds from ``_send_dm_html(outbound_kind=…)``: ``twitch_status``,
``posthog_status``, ``cursor_status``, ``broadcast:{id}``,
``beta_announce:{id}`` (fallback ``system_dm``). Other paths use
``reply``, ``welcome``, ``alert_*``, etc. Surfaced on ``bot_blocked`` as
``last_bot_message_kind`` (and Forbidden send ``source``).
"""
from __future__ import annotations

import time

_last: dict[int, tuple[str, float]] = {}


def note_outbound(user_id: int, kind: str, *, at: float | None = None) -> None:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    _last[uid] = (str(kind), float(at if at is not None else time.time()))


def last_outbound(user_id: int) -> tuple[str, float] | None:
    try:
        return _last.get(int(user_id))
    except (TypeError, ValueError):
        return None
