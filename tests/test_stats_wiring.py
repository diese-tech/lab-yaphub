"""Tests for the public-stats wiring in bot.py: the daily refresh loop's
schedule and resilience, and the stats server's best-effort startup/
shutdown as part of the bot's own lifecycle.

Mirrors the style of tests/test_discord_configuration.py, which covers
the equivalent properties for reconcile_loop.
"""

from __future__ import annotations

import datetime
import inspect
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from zoneinfo import ZoneInfo

from discord.ext import commands

import bot as bot_module
from config import STATS_REFRESH_HOUR_ET, STATS_REFRESH_MINUTE_ET


def test_stats_refresh_loop_is_scheduled_for_the_configured_eastern_time():
    scheduled = bot_module.stats_refresh_loop.time[0]

    assert scheduled == datetime.time(
        hour=STATS_REFRESH_HOUR_ET,
        minute=STATS_REFRESH_MINUTE_ET,
        tzinfo=ZoneInfo("America/New_York"),
    )


def test_stats_refresh_loop_body_cannot_kill_itself():
    """refresh_public_stats_snapshot is already best-effort internally, so
    the loop body needs no try/except of its own -- but if that internal
    contract were ever weakened, an unhandled exception here would stop
    the loop, per discord.ext.tasks' behavior. A registered error handler
    is the backstop, matching reconcile_loop's."""
    assert bot_module.stats_refresh_loop._error is bot_module.on_stats_refresh_loop_error


async def test_a_failed_refresh_still_lets_the_loop_body_complete():
    source = inspect.getsource(bot_module.stats_refresh_loop.coro)
    assert "refresh_public_stats_snapshot" in source


async def test_the_error_handler_restarts_a_stopped_loop():
    with patch.object(bot_module.stats_refresh_loop, "is_running", return_value=False), patch.object(
        bot_module.stats_refresh_loop, "restart"
    ) as restart:
        await bot_module.on_stats_refresh_loop_error(RuntimeError("boom"))

    restart.assert_called_once()


async def test_the_error_handler_does_not_double_restart_a_running_loop():
    with patch.object(bot_module.stats_refresh_loop, "is_running", return_value=True), patch.object(
        bot_module.stats_refresh_loop, "restart"
    ) as restart:
        await bot_module.on_stats_refresh_loop_error(RuntimeError("boom"))

    restart.assert_not_called()


# --- setup_hook / close resilience ------------------------------------


async def test_setup_hook_continues_if_the_stats_server_fails_to_start(caplog):
    """A bound-port conflict or a sandbox without public networking must
    not stop the bot from logging into Discord and doing its actual job."""
    fresh_bot = bot_module.YapHubBot.__new__(bot_module.YapHubBot)
    fresh_bot.storage = AsyncMock()
    fresh_bot.storage.get_public_stats_snapshot = AsyncMock(return_value=None)
    fresh_bot.stats_server_runner = None
    fresh_bot.public_stats_cache = None

    with patch(
        "bot.start_stats_server", new=AsyncMock(side_effect=OSError("address in use"))
    ), patch.object(bot_module.YapHubBot, "add_view"), patch.object(
        commands.Bot, "tree", new_callable=PropertyMock, return_value=Mock(add_command=Mock())
    ):
        await fresh_bot.setup_hook()  # must not raise

    assert fresh_bot.stats_server_runner is None
    assert "Failed to start the public stats server" in caplog.text


async def test_setup_hook_warms_the_cache_from_a_prior_snapshot():
    """A restart must not 503 the public endpoint while waiting for the
    next daily refresh -- setup_hook reads the last durable snapshot into
    the in-memory cache the HTTP route serves from."""
    fresh_bot = bot_module.YapHubBot.__new__(bot_module.YapHubBot)
    fresh_bot.storage = AsyncMock()
    fresh_bot.storage.get_public_stats_snapshot = AsyncMock(
        return_value={"as_of": "2026-09-02T10:00:00-04:00", "payload": '{"rooms_created_total": 1}'}
    )
    fresh_bot.stats_server_runner = None
    fresh_bot.public_stats_cache = None

    with patch("bot.start_stats_server", new=AsyncMock(return_value=object())), patch.object(
        bot_module.YapHubBot, "add_view"
    ), patch.object(
        commands.Bot, "tree", new_callable=PropertyMock, return_value=Mock(add_command=Mock())
    ):
        await fresh_bot.setup_hook()

    assert fresh_bot.public_stats_cache == '{"rooms_created_total": 1}'


async def test_setup_hook_continues_if_warming_the_cache_fails(caplog):
    """A storage error reading the prior snapshot must not stop the bot
    from starting the stats server or logging into Discord."""
    fresh_bot = bot_module.YapHubBot.__new__(bot_module.YapHubBot)
    fresh_bot.storage = AsyncMock()
    fresh_bot.storage.get_public_stats_snapshot = AsyncMock(side_effect=RuntimeError("db down"))
    fresh_bot.stats_server_runner = None
    fresh_bot.public_stats_cache = None

    with patch("bot.start_stats_server", new=AsyncMock(return_value=object())), patch.object(
        bot_module.YapHubBot, "add_view"
    ), patch.object(
        commands.Bot, "tree", new_callable=PropertyMock, return_value=Mock(add_command=Mock())
    ):
        await fresh_bot.setup_hook()  # must not raise

    assert fresh_bot.public_stats_cache is None
    assert fresh_bot.stats_server_runner is not None
    assert "Failed to warm the public stats cache" in caplog.text


async def test_close_cleans_up_the_stats_server_runner_when_present():
    fresh_bot = bot_module.YapHubBot.__new__(bot_module.YapHubBot)
    runner = AsyncMock()
    fresh_bot.stats_server_runner = runner

    with patch("discord.ext.commands.Bot.close", new=AsyncMock()) as super_close:
        await fresh_bot.close()

    runner.cleanup.assert_awaited_once()
    super_close.assert_awaited_once()


async def test_close_does_not_crash_when_the_stats_server_never_started():
    fresh_bot = bot_module.YapHubBot.__new__(bot_module.YapHubBot)
    fresh_bot.stats_server_runner = None

    with patch("discord.ext.commands.Bot.close", new=AsyncMock()) as super_close:
        await fresh_bot.close()  # must not raise

    super_close.assert_awaited_once()
