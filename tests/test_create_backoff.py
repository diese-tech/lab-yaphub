import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services.create_backoff import CreateBackoff, RoomCreationPaused


def rejection(status=429, headers=None):
    response = SimpleNamespace(status=status, reason="Rejected", headers=headers or {})
    return discord.HTTPException(response, "Temporarily blocked")


async def test_429_pauses_other_guilds_without_sending_or_retrying(monkeypatch):
    monkeypatch.setattr("services.create_backoff.time.monotonic", lambda: 100)
    gate = CreateBackoff()
    first = SimpleNamespace(id=1, create_voice_channel=AsyncMock(side_effect=rejection()))
    other = SimpleNamespace(id=2, create_voice_channel=AsyncMock())
    with pytest.raises(discord.HTTPException):
        await gate.create(first, name="A")
    with pytest.raises(RoomCreationPaused):
        await gate.create(other, name="B")
    first.create_voice_channel.assert_awaited_once()
    other.create_voice_channel.assert_not_awaited()
    monkeypatch.setattr("services.create_backoff.time.monotonic", lambda: 161)
    await gate.create(other, name="B")
    other.create_voice_channel.assert_awaited_once_with(name="B")


@pytest.mark.parametrize("value,expected", [("120.5", 120.5), ("bad", 60), ("nan", 60), ("inf", 60), ("-1", 60)])
async def test_retry_after_is_respected_and_invalid_values_are_safe(value, expected):
    gate = CreateBackoff()
    guild = SimpleNamespace(id=1, create_voice_channel=AsyncMock(side_effect=rejection(headers={"Retry-After": value})))
    with pytest.raises(discord.HTTPException):
        await gate.create(guild, name="A")
    import time
    assert gate.until - time.monotonic() == pytest.approx(expected, abs=1)


async def test_permission_failure_does_not_pause_other_guilds():
    gate = CreateBackoff()
    guild = SimpleNamespace(id=1, create_voice_channel=AsyncMock(side_effect=rejection(status=403)))
    with pytest.raises(discord.HTTPException):
        await gate.create(guild, name="A")
    assert gate.until == 0


async def test_concurrent_joins_send_only_one_rejected_request():
    gate = CreateBackoff()
    guild = SimpleNamespace(id=1, create_voice_channel=AsyncMock(side_effect=rejection()))
    results = await asyncio.gather(
        *(gate.create(guild, name=str(i)) for i in range(20)),
        return_exceptions=True,
    )
    guild.create_voice_channel.assert_awaited_once()
    assert sum(isinstance(result, RoomCreationPaused) for result in results) == 19
