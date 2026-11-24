"""One Telegram delivery boundary: every DM is a per-recipient outcome.

``bot_helpers._send_dm_html`` is the single delivery boundary for system DMs.
A transport error (``NetworkError`` / ``TimedOut``) must come back as that
recipient's "failed" outcome, so one failed send never ends a fan-out loop and
never ends a ``check_streams`` tick.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Forbidden, NetworkError, RetryAfter, TimedOut


def _dm_db() -> MagicMock:
    db = MagicMock()
    db.get_notifications_paused_until = MagicMock(return_value=0)
    db.is_bot_blocked = MagicMock(return_value=False)
    db.set_bot_blocked = MagicMock()
    db.pause_delivery_for_chat = MagicMock(return_value=0)
    db.get_user_locale = MagicMock(return_value="en")
    return db


def _bot(*side_effect) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=list(side_effect))
    return bot


def _sent_ids(bot: MagicMock) -> list[int]:
    return [call.args[0] for call in bot.send_message.await_args_list]


def _check_boundary() -> None:
    from bot_helpers import _send_dm_html

    async def _run() -> None:
        db = _dm_db()

        # Transport error: this recipient failed, nothing repeats the send.
        bot = _bot(NetworkError("httpx.ConnectError"))
        assert (
            await _send_dm_html(bot, db, 101, "hi", outbound_kind="trial_expiry")
            == "failed"
        )
        assert bot.send_message.await_count == 1, "a transport error must not repeat"

        bot = _bot(TimedOut("request timed out"))
        assert await _send_dm_html(bot, db, 102, "hi") == "failed"
        assert bot.send_message.await_count == 1

        # Blocked user: still marked blocked, with the outbound kind as source.
        bot = _bot(Forbidden("Forbidden: bot was blocked by the user"))
        assert (
            await _send_dm_html(bot, db, 103, "hi", outbound_kind="twitch_status")
            == "blocked"
        )
        db.set_bot_blocked.assert_called_with(103, True)

        # RetryAfter: wait once, repeat once, deliver.
        bot = _bot(RetryAfter(3), None)
        sleeps: list[float] = []

        async def _sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with patch("bot_helpers.asyncio.sleep", _sleep):
            assert await _send_dm_html(bot, db, 104, "hi") == "sent"
        assert bot.send_message.await_count == 2
        assert sleeps == [3.5]

        # A transport error on the RetryAfter repeat is also "failed".
        bot = _bot(RetryAfter(1), NetworkError("httpx.ConnectError"))
        with patch("bot_helpers.asyncio.sleep", _sleep):
            assert await _send_dm_html(bot, db, 105, "hi") == "failed"
        assert bot.send_message.await_count == 2

    asyncio.run(_run())


def _check_growth_report_fanout() -> None:
    from handlers import monitoring

    async def _run() -> None:
        db = _dm_db()
        db.get_bot_stats = MagicMock(return_value=MagicMock(users=10))
        context = MagicMock()
        context.application.bot_data = {"db": db}
        context.bot = _bot(None, NetworkError("httpx.ConnectError"), None)
        saved: list[str] = []
        with (
            patch.object(monitoring, "_load_admin_growth_snapshots", return_value={}),
            patch.object(
                monitoring,
                "_save_admin_growth_snapshot",
                side_effect=lambda kind, **kwargs: saved.append(kind),
            ),
            patch("config.ADMIN_USER_IDS", [11, 22, 33]),
        ):
            await monitoring._send_growth_report_to_admins(
                context,
                template_key="weekly_new_users",
                count=3,
                paid=1,
                trials=[],
                snapshot_kind="weekly",
            )
        assert _sent_ids(context.bot) == [11, 22, 33], _sent_ids(context.bot)
        assert saved == ["weekly"], "the growth snapshot must still be written"

    asyncio.run(_run())


def _check_premium_expiry_loops() -> None:
    import premium as prem
    from handlers import notifications

    async def _run() -> None:
        db = _dm_db()
        context = MagicMock()
        context.application.bot_data = {"db": db}
        context.bot = _bot(None, NetworkError("httpx.ConnectError"), None, None)
        with (
            patch.object(
                prem,
                "expire_due_trials",
                return_value=[(201, 0), (202, 0), (203, 0)],
            ),
            patch.object(prem, "trial_expiry_should_notify", return_value=True),
            patch.object(prem, "claim_pay_reminders", return_value=[]),
            patch.object(prem, "process_stars_expiry_grace", return_value=[]),
        ):
            await notifications._notify_premium_expiries(context, db)
        assert _sent_ids(context.bot) == [201, 202, 203], _sent_ids(context.bot)

    asyncio.run(_run())


def _check_pay_and_grace_senders_return_outcome() -> None:
    import premium as prem
    from premium_handlers import send_pay_reminder_dm, send_stars_grace_dm

    async def _run() -> None:
        db = _dm_db()
        db.get_bot_stats = MagicMock(return_value=MagicMock(users=1))

        bot = _bot(NetworkError("httpx.ConnectError"))
        reminder = prem.PayReminder(user_id=301, kind="stars", until_unix=0)
        assert await send_pay_reminder_dm(bot, db, reminder, "en") == "failed"
        assert bot.send_message.await_count == 1

        bot = _bot(TimedOut("request timed out"))
        with patch.object(prem, "stars_plan_kind", return_value="month"):
            assert await send_stars_grace_dm(bot, db, 302, "en") == "failed"
        assert bot.send_message.await_count == 1

    asyncio.run(_run())


def run() -> None:
    _check_boundary()
    _check_growth_report_fanout()
    _check_premium_expiry_loops()
    _check_pay_and_grace_senders_return_outcome()
