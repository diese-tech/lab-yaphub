import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import discord
import pytest

from services.login_backoff import login_with_backoff


def rejected(status=429, headers=None):
    return discord.HTTPException(
        SimpleNamespace(status=status, reason="Rejected", headers=headers or {}),
        "Temporarily blocked",
    )


async def test_login_recovers_with_header_delay_and_closes_failed_session():
    login = AsyncMock(side_effect=[rejected(headers={"Retry-After": "123.5"}), None])
    close = AsyncMock()
    with patch("services.login_backoff.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await login_with_backoff(login, close, "test-token")
    assert login.await_count == 2
    close.assert_awaited_once()
    sleep.assert_awaited_once_with(123.5)


async def test_missing_timing_uses_capped_exponential_backoff():
    login = AsyncMock(side_effect=[rejected() for _ in range(7)] + [None])
    close = AsyncMock()
    with patch("services.login_backoff.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await login_with_backoff(login, close, "test-token")
    assert sleep.await_args_list == [call(x) for x in [60, 120, 240, 480, 900, 900, 900]]
    assert close.await_count == 7


@pytest.mark.parametrize("timing", ["nan", "inf", "-1", "bad", "0"])
async def test_malformed_retry_timing_does_not_spin(timing):
    login = AsyncMock(side_effect=[rejected(headers={"Retry-After": timing}), None])
    with patch("services.login_backoff.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await login_with_backoff(login, AsyncMock(), "test-token")
    sleep.assert_awaited_once_with(60)


@pytest.mark.parametrize("error", [rejected(status=403), discord.LoginFailure("Invalid token"), RuntimeError("setup")])
async def test_non_429_errors_propagate_without_retry(error):
    login = AsyncMock(side_effect=error)
    close = AsyncMock()
    with pytest.raises(type(error)):
        await login_with_backoff(login, close, "test-token")
    login.assert_awaited_once()
    close.assert_not_awaited()


async def test_shutdown_cancels_wait_without_retry():
    login = AsyncMock(side_effect=rejected())
    close = AsyncMock()
    with patch("services.login_backoff.asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await login_with_backoff(login, close, "test-token")
    login.assert_awaited_once()
    close.assert_awaited_once()


async def test_bot_start_uses_login_recovery_before_connecting():
    import bot as bot_module

    client = bot_module.YapHubBot()
    with (
        patch("discord.ext.commands.Bot.login", new_callable=AsyncMock) as base_login,
        patch.object(client.http, "close", new_callable=AsyncMock) as close,
        patch.object(client, "connect", new_callable=AsyncMock) as connect,
        patch("services.login_backoff.asyncio.sleep", new_callable=AsyncMock),
    ):
        base_login.side_effect = [rejected(), None]
        await client.start("test-token")
    assert base_login.await_count == 2
    close.assert_awaited_once()
    connect.assert_awaited_once_with(reconnect=True)


async def test_real_login_recreates_connector_and_runs_setup_only_after_success():
    import bot as bot_module

    client = bot_module.YapHubBot()
    attempts = 0
    connectors = []

    async def request(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        connector = client.http.connector
        assert not connector.closed
        connectors.append(connector)
        if attempts == 1:
            raise rejected()
        return {"id": "1", "username": "YapHub", "discriminator": "0", "avatar": None}

    with (
        patch.object(client.http, "request", side_effect=request),
        patch.object(client, "application_info", new_callable=AsyncMock) as app,
        patch.object(client, "setup_hook", new_callable=AsyncMock) as setup,
        patch("services.login_backoff.asyncio.sleep", new_callable=AsyncMock),
    ):
        app.return_value = SimpleNamespace(id=1, interactions_endpoint_url=None, flags=None)
        async with client:
            await client.login("test-token")
            setup.assert_awaited_once()
            assert connectors[0].closed
            assert connectors[0] is not connectors[1]
            assert not connectors[1].closed
    assert connectors[1].closed
