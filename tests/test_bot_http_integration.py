import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from discord.http import Route

import bot as bot_module
from services.api_recovery import ApiDeferred


@pytest.fixture
async def discord_server():
    attempts = []
    replies = []

    async def handler(request):
        attempts.append((request.method, request.path))
        status, headers, body = 200, {}, []
        if replies:
            status, headers, body = replies.pop(0)
        elif request.path == "/users/@me":
            body = {"id": "1", "username": "YapHub", "discriminator": "0", "avatar": None}
        return web.Response(body=json.dumps(body).encode(), status=status,
                            headers={"Content-Type": "application/json", **headers})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", attempts, replies
    await runner.cleanup()


async def test_real_bot_login_recovery_and_command_tree_share_guarded_session(tmp_path, monkeypatch, discord_server):
    url, attempts, replies = discord_server
    monkeypatch.setattr(Route, "BASE", url)
    monkeypatch.setattr(bot_module, "DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    client = bot_module.YapHubBot()
    replies.append((429, {"Retry-After": "0.02"}, {"message": "Temporarily blocked", "code": 0}))

    with (
        patch("bot.start_stats_server", new_callable=AsyncMock, return_value=None),
        patch.object(client, "application_info", new_callable=AsyncMock,
                     return_value=SimpleNamespace(id=1, interactions_endpoint_url=None, flags=None)),
    ):
        async with client:
            await client.login("fake-local-test-token")
            # Exercise real CommandTree.sync and HTTPClient.request after a
            # rejected login has closed and recreated the session.
            await client.tree.sync()
            assert attempts == [("GET", "/users/@me"), ("GET", "/users/@me"),
                                ("PUT", "/applications/1/commands")]
            assert client.api_recovery.metrics()["requests_10m"] == 3
            replies.append((429, {"Retry-After": "60"}, {"message": "Temporarily blocked", "code": 0}))
            with pytest.raises(ApiDeferred):
                await client.tree.sync()
            with pytest.raises(ApiDeferred):
                await client.tree.fetch_commands()
            assert len(attempts) == 4
            assert client.api_recovery.metrics()["http_429_10m"] == 2
            assert client._connection.http is client.http


async def test_command_sync_failure_does_not_prevent_room_worker_start(tmp_path, monkeypatch, discord_server):
    url, attempts, replies = discord_server
    monkeypatch.setattr(Route, "BASE", url)
    monkeypatch.setattr(bot_module, "DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    client = bot_module.YapHubBot()
    monkeypatch.setattr(bot_module, "bot", client)
    with (
        patch("bot.start_stats_server", new_callable=AsyncMock, return_value=None),
        patch.object(client, "application_info", new_callable=AsyncMock,
                     return_value=SimpleNamespace(id=1, interactions_endpoint_url=None, flags=None)),
        patch.object(bot_module.reconcile_loop, "start"),
        patch.object(bot_module.stats_refresh_loop, "start"),
        patch.object(client.lifecycle_recovery, "scan", new_callable=AsyncMock),
        patch.object(client.lifecycle_recovery, "start") as workers,
    ):
        async with client:
            await client.login("fake-local-test-token")
            replies.append((403, {}, {"message": "Missing Access", "code": 50001}))
            await bot_module.on_ready()
            assert client.runtime_initialized
            assert client.command_sync_pending
            workers.assert_called_once()
            assert attempts == [("GET", "/users/@me"), ("PUT", "/applications/1/commands")]
