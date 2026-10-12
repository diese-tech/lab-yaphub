import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from discord.http import HTTPClient, Route

from services.api_recovery import (
    AmbiguousMutation, ApiDeferred, ApiRecovery, ExplicitRejection, RecoveryHTTPClient, RequestInfo, url_info,
)
from services.stats_server import build_stats_app
from storage import Storage


@pytest.fixture
async def api(tmp_path):
    recovery = ApiRecovery(Storage(str(tmp_path / "state.sqlite3")))
    await recovery.initialize()
    return recovery


@pytest.fixture
async def server():
    attempts = []
    replies = []

    async def handler(request):
        attempts.append((request.method, request.path))
        if replies:
            status, headers, payload = replies.pop(0)
            return web.Response(body=json.dumps(payload).encode(), status=status,
                                headers={"Content-Type": "application/json", **headers})
        return web.Response(body=b'{"id":"123"}', headers={"Content-Type": "application/json"})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", attempts, replies
    await runner.cleanup()


async def client_for(api):
    original = HTTPClient(asyncio.get_running_loop())
    client = RecoveryHTTPClient(original, api)
    # Use the real client and trace configuration, with only transport
    # destination changed to our local HTTP server.
    client._HTTPClient__session = aiohttp.ClientSession(trace_configs=[api.trace()])
    client._global_over = asyncio.Event()
    client._global_over.set()
    return client


def route(server, method, path, **ids):
    value = Route(method, path, **ids)
    value.url = server + value.url.split("/api/v10", 1)[1]
    return value


async def test_real_429_blocks_delete_fetch_and_panels_without_attempts(api, server):
    url, attempts, replies = server
    replies.append((429, {"Retry-After": "60"}, {"message": "blocked"}))
    client = await client_for(api)
    try:
        with pytest.raises(ExplicitRejection):
            await client.request(route(url, "POST", "/guilds/{guild_id}/channels", guild_id=1), json={"name": "A"})
        for method, path in [("DELETE", "/channels/{channel_id}"), ("GET", "/channels/{channel_id}"),
                             ("POST", "/channels/{channel_id}/messages")]:
            with pytest.raises(ApiDeferred):
                await client.request(route(url, method, path, channel_id=2))
        assert len(attempts) == 1
        restored = ApiRecovery(api.storage)
        await restored.initialize()
        assert restored.retry_at() == api.retry_at()
        with pytest.raises(ApiDeferred):
            await restored.admit(RequestInfo("GET", "/users/@me"))
    finally:
        await client.close()


async def test_resource_scope_preserves_other_resources_and_internal_retry_is_guarded(api, server):
    url, attempts, replies = server
    replies.append((429, {"Retry-After": "0.03", "Via": "edge", "X-RateLimit-Scope": "shared",
                          "X-RateLimit-Bucket": "bucket"}, {"retry_after": 0.03, "global": False}))
    client = await client_for(api)
    try:
        await client.request(route(url, "DELETE", "/channels/{channel_id}", channel_id=2))
        await client.request(route(url, "DELETE", "/channels/{channel_id}", channel_id=3))
        assert len(attempts) == 3
        assert api.metrics()["http_429_10m"] == 1
        assert not api.states
    finally:
        await client.close()


async def test_create_5xx_has_one_attempt_and_no_hidden_retry(api, server):
    url, attempts, replies = server
    replies.append((503, {}, {"message": "unknown"}))
    client = await client_for(api)
    try:
        with pytest.raises(AmbiguousMutation):
            await client.request(route(url, "POST", "/guilds/{guild_id}/channels", guild_id=1), json={"name": "A"})
        assert len(attempts) == 1
    finally:
        await client.close()


async def test_only_one_expired_probe_and_cancel_releases_with_backoff(api):
    state = {"scope_key": "global", "deadline_utc": time.time() - 1, "attempt": 1,
             "route_key": "GET /users/@me:", "reason": "discord"}
    await api.storage.save_api_cooldown(state)
    await api.initialize()
    info = RequestInfo("GET", "/users/@me")
    await api.admit(info)
    async def second():
        with pytest.raises(ApiDeferred):
            await api.admit(info)
    await asyncio.create_task(second())
    await api.release_probe(info)
    assert not api.probes
    with pytest.raises(ApiDeferred):
        await api.admit(info)


async def test_persistence_failure_does_not_open_gate(api):
    api.storage.save_api_cooldown = AsyncMock(side_effect=RuntimeError("disk unavailable"))
    with pytest.raises(RuntimeError):
        await api.observe(RequestInfo("POST", "/guilds/{guild_id}/channels", "1"),
                          SimpleNamespace(status=429, headers={}), None, 0.1)
    with pytest.raises(ApiDeferred):
        await api.admit(RequestInfo("GET", "/users/@me"))


async def test_backup_includes_wal_and_validates(api, tmp_path):
    await api.storage.save_api_cooldown({"scope_key": "global", "deadline_utc": time.time() + 999,
                                       "attempt": 1, "route_key": "GET /users/@me:", "reason": "discord"})
    path = await api.storage.backup(tmp_path / "backup.sqlite3")
    copied = Storage(path)
    assert len(await copied.list_api_cooldowns()) == 1


async def test_health_reports_liveness_without_false_readiness(server):
    bot = SimpleNamespace(public_stats_cache=None, status_cache='{"state":"blocked"}')
    app = build_stats_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            for path, expected in [("healthz", 200), ("readyz", 503), ("status.json", 200)]:
                async with session.get(f"http://127.0.0.1:{port}/{path}") as response:
                    assert response.status == expected
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("retry", ["nan", "-1", "0", "invalid", "inf"])
async def test_invalid_retry_timing_increases_backoff_without_shortening(api, retry):
    info = RequestInfo("GET", "/users/@me")
    response = SimpleNamespace(status=429, headers={"Retry-After": retry})
    await api.observe(info, response, None, 0.1)
    first = api.retry_at()
    assert 59 <= first - time.time() <= 61
    await api.observe(info, response, None, 0.1)
    assert api.retry_at() > first + 59


async def test_ambiguous_global_metadata_is_not_treated_as_resource_limit(api):
    await api.observe(RequestInfo("DELETE", "/channels/{channel_id}", "1"),
                      SimpleNamespace(status=429, headers={"Via": "edge", "X-RateLimit-Scope": "user",
                                                          "X-RateLimit-Bucket": "bucket", "Retry-After": "60"}),
                      {"global": "false"}, 0.1)
    assert api.blocked()


async def test_probe_rejection_renews_pause_without_another_mutation(api, server, monkeypatch):
    url, attempts, replies = server
    monkeypatch.setattr(Route, "BASE", url + "/api/v10")
    replies.append((429, {"Retry-After": "0.02"}, {"message": "blocked"}))
    client = await client_for(api)
    try:
        with pytest.raises(ExplicitRejection):
            await client.request(route(url, "POST", "/guilds/{guild_id}/channels", guild_id=1))
        await asyncio.sleep(0.03)
        replies.append((429, {"Retry-After": "60"}, {"message": "blocked"}))
        with pytest.raises(ExplicitRejection):
            await client.request(route(url, "DELETE", "/channels/{channel_id}", channel_id=2))
        with pytest.raises(ApiDeferred):
            await client.request(route(url, "DELETE", "/channels/{channel_id}", channel_id=2))
        assert [method for method, _ in attempts] == ["POST", "GET"]
        assert api.retry_at() > time.time() + 59
    finally:
        await client.close()


def test_webhook_and_interaction_urls_are_sanitized():
    from yarl import URL
    for path in ("/api/v10/webhooks/123/secret-token?secret=value",
                 "/api/v10/interactions/123/secret-token/callback?secret=value"):
        info = url_info("POST", URL("https://discord.com" + path))
        assert "secret" not in info.path and "secret" not in info.key
