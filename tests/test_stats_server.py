"""Tests for services/stats_server.py: the one public HTTP route and its
contract (cached payload, CORS, no-snapshot-yet behavior, GET-only).
"""

from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.stats_server import build_stats_app

CACHED_PAYLOAD = json.dumps(
    {
        "as_of": "2026-09-02T10:00:00-04:00",
        "servers_served": 42,
        "unique_users_served": 3912,
        "rooms_created_total": 18273,
        "rooms_created_7d": 614,
        "rooms_created_30d": 2164,
        "active_profiles": 58,
    }
)


def _row(payload: str, as_of: str = "2026-09-02T10:00:00-04:00"):
    return {"as_of": as_of, "payload": payload}


def _bot(row=None):
    storage = types.SimpleNamespace(
        get_public_stats_snapshot=AsyncMock(return_value=row),
    )
    return types.SimpleNamespace(storage=storage)


@pytest.fixture
async def client(request):
    bot = getattr(request, "param", None) or _bot(_row(CACHED_PAYLOAD))
    app = build_stats_app(bot)
    server = TestServer(app)
    async with TestClient(server) as client:
        client.app_bot = bot  # stash for tests that want to assert on the mock
        yield client


async def test_returns_the_cached_snapshot_verbatim(client):
    response = await client.get("/stats.json")

    assert response.status == 200
    body = await response.json()
    assert body["servers_served"] == 42
    assert body["rooms_created_total"] == 18273


async def test_content_type_is_json(client):
    response = await client.get("/stats.json")

    assert "application/json" in response.headers["Content-Type"]


async def test_allows_cross_origin_reads(client):
    """The landing page is served from a different origin (GitHub Pages)
    than the bot; without this header the browser would block the JS
    fetch() from reading the response at all."""
    response = await client.get("/stats.json")

    assert response.headers["Access-Control-Allow-Origin"] == "*"


async def test_returns_503_with_no_snapshot_yet_rather_than_a_fake_payload():
    bot = _bot(row=None)
    app = build_stats_app(bot)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/stats.json")

        assert response.status == 503
        body = await response.json()
        assert "rooms_created_total" not in body
        # Even the "not ready" response must be readable cross-origin, or a
        # fresh deploy looks like a silent CORS failure instead of "not
        # ready yet."
        assert response.headers["Access-Control-Allow-Origin"] == "*"


async def test_serves_the_payload_byte_for_byte_not_reencoded(client):
    """payload is stored pre-serialized; the server must not decode and
    re-encode it, or a field order/formatting difference could creep in
    between what services/public_stats.py built and what goes out."""
    response = await client.get("/stats.json")

    text = await response.text()
    assert text == CACHED_PAYLOAD


async def test_post_is_not_allowed(client):
    response = await client.post("/stats.json")

    assert response.status in (404, 405)


async def test_only_the_stats_route_exists(client):
    response = await client.get("/")

    assert response.status == 404
