"""One Telegram delivery boundary: every DM / alert is a per-recipient outcome.

``bot_helpers._send_dm_html`` is the boundary for system DMs;
``handlers.delivery._send_notification`` is the boundary for alerts.
A transport error (``NetworkError`` / ``TimedOut``) must come back as that
recipient's "failed" outcome, so one failed send never ends a fan-out loop and
never ends a ``check_streams`` tick.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
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


def _check_alert_send_transport_error() -> None:
    """A transport error stays with its destination and leaves the tick running."""
    import telegram_circuit
    from db import open_database
    from handlers import delivery
    from handlers.notifications import transport_job_name

    async def _run() -> None:
        telegram_circuit.reset_for_tests()
        with tempfile.TemporaryDirectory() as td:
            db = open_database(Path(td) / "alert_boundary.db")
            uid = 601
            db.upsert_user(uid)
            db.set_user_locale(uid, "en")
            first_id = db.add_subscription(
                uid, "streamer", "tw1", "{username} live", "dm", uid, None
            )
            second_id = db.add_subscription(
                uid, "other", "tw2", "{username} live", "dm", uid, None
            )
            first = next(
                s for s in db.get_subscriptions_by_owner(uid) if s.id == first_id
            )
            second = next(
                s for s in db.get_subscriptions_by_owner(uid) if s.id == second_id
            )

            bot = AsyncMock()
            bot.send_message = AsyncMock(
                side_effect=NetworkError("httpx.ConnectError")
            )
            bot.send_photo = AsyncMock(
                side_effect=NetworkError("httpx.ConnectError")
            )
            bot.get_me = AsyncMock(
                return_value=MagicMock(username="testbot", id=1)
            )
            outcomes: list[str] = []

            def _record(**kwargs) -> None:
                outcomes.append(str(kwargs.get("outcome") or ""))

            with patch.object(delivery.metrics, "record_alert_delivery", _record):
                assert (
                    await delivery._send_notification(
                        bot, db, first, "hi", stream_id="s1"
                    )
                ) is False
            assert outcomes == ["failed"], outcomes
            job = transport_job_name(first.id, "live", "s1")
            assert db.has_pending_alert_job(job), "transport fail must enqueue retry"
            # Inner path would raise; the boundary must not.
            try:
                await delivery._send_notification_inner(bot, db, first, "hi")
            except NetworkError:
                pass
            else:
                raise AssertionError(
                    "_send_notification_inner must still raise NetworkError"
                )

            bot.send_message = AsyncMock(
                return_value=MagicMock(message_id=42)
            )
            bot.send_photo = AsyncMock(return_value=MagicMock(message_id=42))
            assert (
                await delivery._send_notification(bot, db, second, "hi again")
            ) is True

    asyncio.run(_run())
    telegram_circuit.reset_for_tests()


def _check_circuit_open_defers_without_send() -> None:
    """Open circuit skips Telegram send and enqueues one pending transport job."""
    import telegram_circuit
    from db import open_database
    from handlers import delivery
    from handlers.notifications import transport_job_name

    async def _run() -> None:
        telegram_circuit.reset_for_tests()
        telegram_circuit.note_failure(TimedOut())
        telegram_circuit.note_failure(TimedOut())
        assert telegram_circuit.is_open()
        with tempfile.TemporaryDirectory() as td:
            db = open_database(Path(td) / "circuit_defer.db")
            uid = 701
            db.upsert_user(uid)
            db.set_user_locale(uid, "en")
            sub_id = db.add_subscription(
                uid, "streamer", "tw1", "{username} live", "dm", uid, None
            )
            sub = next(s for s in db.get_subscriptions_by_owner(uid) if s.id == sub_id)
            bot = AsyncMock()
            bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
            outcomes: list[str] = []

            def _record(**kwargs) -> None:
                outcomes.append(str(kwargs.get("outcome") or ""))

            with patch.object(delivery.metrics, "record_alert_delivery", _record):
                assert (
                    await delivery._send_notification(
                        bot, db, sub, "hi", alert_type="live", stream_id="abc"
                    )
                ) is False
            assert bot.send_message.await_count == 0, "open circuit must not send"
            assert outcomes == ["deferred"], outcomes
            name = transport_job_name(sub.id, "live", "abc")
            assert db.has_pending_alert_job(name)
            # Same key replaces — still one row.
            with patch.object(delivery.metrics, "record_alert_delivery", _record):
                await delivery._send_notification(
                    bot, db, sub, "hi again", alert_type="live", stream_id="abc"
                )
            pending = [
                r
                for r in db.list_pending_alert_jobs()
                if r.job_name == name
            ]
            assert len(pending) == 1, "transport job key must stay unique"

    asyncio.run(_run())
    telegram_circuit.reset_for_tests()


def _check_external_transport_retry() -> None:
    """A deferred external go-live alert is delivered from the platform probe, not Helix."""
    import stream_platforms as sp
    from db import open_database
    from handlers import notifications

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            db = open_database(Path(td) / "external_retry.db")
            uid = 801
            db.upsert_user(uid)
            db.set_user_locale(uid, "en")
            sub_id = db.add_subscription(
                uid,
                "sl4mtv",
                "ext:kick:sl4mtv",
                "{username} live",
                "dm",
                uid,
                None,
                stream_platform="kick",
                stream_url="https://kick.com/sl4mtv",
            )
            sub = next(s for s in db.get_subscriptions_by_owner(uid) if s.id == sub_id)
            assert sp.is_external_subscription(
                stream_platform=sub.stream_platform,
                twitch_user_id=sub.twitch_user_id,
            ), "test sub must resolve as an external channel"

            twitch = MagicMock()
            twitch.get_live_streams = MagicMock(
                side_effect=AssertionError("an ext: id must not reach Helix")
            )
            bot = AsyncMock()
            bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))

            def _context(alert_type: str) -> MagicMock:
                context = MagicMock()
                context.application.bot_data = {"db": db, "twitch": twitch}
                context.application.job_queue = None
                context.bot = bot
                context.job.data = {
                    "sub_id": sub_id,
                    "alert_type": alert_type,
                    "stream_id": "ext:kick:sl4mtv",
                    "transport_attempt": 1,
                }
                return context

            with patch.object(
                sp,
                "external_live_state",
                return_value=(True, "https://kick.com/sl4mtv"),
            ):
                await notifications._send_transport_retry_notification(_context("live"))
            assert twitch.get_live_streams.call_count == 0, "ext: ids must not reach Helix"
            assert bot.send_message.await_count == 1, "the deferred alert must reach the chat"

            # The channel ended while Telegram was down: nothing to send, still no Helix.
            bot.send_message.reset_mock()
            with patch.object(sp, "external_live_state", return_value=(False, "")):
                await notifications._send_transport_retry_notification(_context("live"))
            assert bot.send_message.await_count == 0
            assert twitch.get_live_streams.call_count == 0

            # End / category retries have nothing to deliver for an external channel.
            with patch.object(sp, "external_live_state", return_value=(True, "")):
                await notifications._send_transport_retry_notification(_context("end"))
            assert bot.send_message.await_count == 0
            assert twitch.get_live_streams.call_count == 0

    asyncio.run(_run())


def run() -> None:
    _check_boundary()
    _check_alert_send_transport_error()
    _check_circuit_open_defers_without_send()
    _check_external_transport_retry()
    _check_growth_report_fanout()
    _check_premium_expiry_loops()
    _check_pay_and_grace_senders_return_outcome()
