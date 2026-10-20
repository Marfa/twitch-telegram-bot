"""Startup: bootstrap survives one Telegram timeout; fatal errors reach Error Tracking."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TimedOut
from telegram.ext import Application, ExtBot

import main as bot_main


def _run_main_with(run_polling: MagicMock) -> MagicMock:
    app = MagicMock()
    app.run_polling = run_polling
    with (
        patch.object(bot_main, "start_health_server"),
        patch.object(bot_main, "validate"),
        patch.object(bot_main, "init_analytics"),
        patch.object(bot_main, "shutdown_analytics"),
        patch.object(bot_main, "open_database"),
        patch.object(bot_main, "TwitchClient"),
        patch.object(bot_main, "build_application", return_value=app),
        patch.object(bot_main, "capture_exception") as capture,
    ):
        try:
            bot_main.main()
        except TimedOut:
            pass
        else:
            assert not run_polling.side_effect, "startup error was swallowed"
    return capture


def _check_bootstrap_retries_one_timeout() -> None:
    run_polling = MagicMock()
    _run_main_with(run_polling)
    retries = run_polling.call_args.kwargs.get("bootstrap_retries", 0)
    assert retries > 0, "one getMe timeout would abort startup"

    app = Application.builder().token("123:abc").build()
    with patch.object(
        ExtBot, "initialize", AsyncMock(side_effect=[TimedOut(), None, None])
    ):
        asyncio.run(app._bootstrap_initialize(max_retries=retries))
    assert app._initialized


def _check_startup_failure_captured() -> None:
    exc = TimedOut()
    capture = _run_main_with(MagicMock(side_effect=exc))
    capture.assert_called_once()
    assert capture.call_args.args[0] is exc


def run() -> None:
    _check_bootstrap_retries_one_timeout()
    _check_startup_failure_captured()
