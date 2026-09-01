"""Tests for services/notifications.py.

The duplicate-room notice is what a member sees after the safeguard blocks a
second room, so it runs on exactly the path the incident travels. It must be
rate limited, must not raise into the creation path, and must not accumulate
one cooldown entry per member for the life of the process.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, Mock

import pytest

from services.notifications import notify_duplicate_room
from tests.conftest import forbidden, http_error, make_guild, make_member, make_voice_channel


@pytest.fixture
def env():
    guild = make_guild(1, name="Test Guild")
    member = make_member(7, guild)
    lobby = make_voice_channel(100, guild, name="Join to Yap")
    existing = make_voice_channel(200, guild, name="user7's Yap")
    bot = types.SimpleNamespace(
        storage=types.SimpleNamespace(get_guild_config=AsyncMock(return_value=None)),
        notification_cooldowns={},
    )
    return types.SimpleNamespace(
        guild=guild, member=member, lobby=lobby, existing=existing, bot=bot
    )


async def test_dm_is_preferred(env):
    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    env.member.send.assert_awaited_once()
    env.lobby.send.assert_not_called()


async def test_falls_back_to_the_lobby_when_the_dm_is_refused(env):
    env.member.send = AsyncMock(side_effect=forbidden("Cannot send messages to this user"))

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    env.lobby.send.assert_awaited_once()
    _, kwargs = env.lobby.send.call_args
    assert kwargs["delete_after"] > 0


async def test_falls_back_to_the_system_channel_when_the_lobby_is_refused(env):
    system = make_voice_channel(300, env.guild, name="general")
    env.guild.system_channel = system
    env.member.send = AsyncMock(side_effect=forbidden())
    env.lobby.send = AsyncMock(side_effect=forbidden())

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    system.send.assert_awaited_once()


async def test_every_delivery_failing_does_not_raise(env):
    """A notice YapHub cannot deliver must not break room creation."""
    env.member.send = AsyncMock(side_effect=forbidden())
    env.lobby.send = AsyncMock(side_effect=http_error(500))
    env.guild.system_channel = None

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)


async def test_repeated_notices_are_rate_limited(env):
    for _ in range(5):
        await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    assert env.member.send.await_count == 1


async def test_the_cooldown_is_per_member(env):
    other = make_member(8, env.guild)

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)
    await notify_duplicate_room(env.bot, other, env.lobby, env.existing)

    env.member.send.assert_awaited_once()
    other.send.assert_awaited_once()


async def test_expired_cooldown_entries_are_pruned(env):
    """Otherwise the dict grows one entry per member, forever."""
    now = asyncio.get_running_loop().time()
    env.bot.notification_cooldowns = {
        (1, 100 + index): now - 1 for index in range(50)
    }

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    assert env.bot.notification_cooldowns == {
        (1, env.member.id): env.bot.notification_cooldowns[(1, env.member.id)]
    }


async def test_a_live_cooldown_for_another_member_is_not_pruned(env):
    now = asyncio.get_running_loop().time()
    env.bot.notification_cooldowns = {(1, 999): now + 600}

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    assert (1, 999) in env.bot.notification_cooldowns


async def test_the_guild_cooldown_setting_is_honoured(env):
    env.bot.storage.get_guild_config = AsyncMock(
        return_value={"notification_cooldown_seconds": 900}
    )

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)

    now = asyncio.get_running_loop().time()
    assert env.bot.notification_cooldowns[(1, env.member.id)] >= now + 800


async def test_a_target_without_send_is_skipped(env):
    env.member.send = AsyncMock(side_effect=forbidden())
    sendless = Mock(spec=[])  # a category, say -- nothing to post into
    env.guild.system_channel = sendless
    env.lobby.send = AsyncMock(side_effect=forbidden())

    await notify_duplicate_room(env.bot, env.member, env.lobby, env.existing)
