"""Tests for services/temp_channels.py."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from services.temp_channels import create_temp_room, reconcile_active_temp_channels
from tests.conftest import make_guild, make_member, make_message, make_voice_channel


def _row(**overrides) -> dict:
    row = {
        "channel_id": "500",
        "guild_id": "1",
        "profile_id": "profile-1",
        "owner_user_id": "42",
        "panel_message_id": None,
    }
    row.update(overrides)
    return row


def _make_bot(*, guild=None, get_guild=None, fetch_channel=None):
    storage = types.SimpleNamespace(
        list_active_temp_channels=AsyncMock(return_value=[]),
        delete_active_temp_channel=AsyncMock(),
        touch_active_temp_channel=AsyncMock(),
        set_panel_message_id=AsyncMock(),
    )
    bot = types.SimpleNamespace(
        storage=storage,
        get_guild=get_guild or Mock(return_value=guild),
        fetch_channel=fetch_channel or AsyncMock(return_value=None),
        active_temp_channel_ids=set(),
    )
    return bot


# --- reconcile_active_temp_channels: stale records ----------------------


async def test_reconcile_deletes_record_for_missing_guild():
    row = _row()
    bot = _make_bot(get_guild=Mock(return_value=None))
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    await reconcile_active_temp_channels(bot)

    bot.storage.delete_active_temp_channel.assert_awaited_once_with(500)
    assert bot.active_temp_channel_ids == set()


async def test_reconcile_deletes_record_for_missing_channel():
    row = _row()
    guild = make_guild(1)
    guild.get_channel = Mock(return_value=None)
    bot = _make_bot(guild=guild, fetch_channel=AsyncMock(side_effect=discord.NotFound(
        types.SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel"
    )))
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    await reconcile_active_temp_channels(bot)

    bot.storage.delete_active_temp_channel.assert_awaited_once_with(500)
    assert bot.active_temp_channel_ids == set()


async def test_reconcile_deletes_empty_room():
    row = _row()
    guild = make_guild(1)
    channel = make_voice_channel(500, guild, members=[])
    guild.get_channel = Mock(return_value=channel)
    bot = _make_bot(guild=guild)
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    await reconcile_active_temp_channels(bot)

    channel.delete.assert_awaited_once()
    bot.storage.delete_active_temp_channel.assert_awaited_once_with(500)
    assert bot.active_temp_channel_ids == set()


async def test_reconcile_keeps_and_touches_non_empty_room():
    row = _row(panel_message_id="777")  # already has a panel -- no backfill
    guild = make_guild(1)
    member = make_member(9, guild)
    channel = make_voice_channel(500, guild, members=[member])
    guild.get_channel = Mock(return_value=channel)
    bot = _make_bot(guild=guild)
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    with patch("services.temp_channels.send_room_panel", new=AsyncMock()) as send_panel:
        await reconcile_active_temp_channels(bot)

    bot.storage.touch_active_temp_channel.assert_awaited_once_with(500)
    bot.storage.delete_active_temp_channel.assert_not_called()
    send_panel.assert_not_called()
    assert bot.active_temp_channel_ids == {500}


# --- panel_message_id backfill -------------------------------------------


async def test_reconcile_backfills_panel_message_when_owner_present():
    row = _row(panel_message_id=None)
    guild = make_guild(1)
    owner = make_member(42, guild)
    guild.get_member = Mock(return_value=owner)
    channel = make_voice_channel(500, guild, members=[owner])
    guild.get_channel = Mock(return_value=channel)
    bot = _make_bot(guild=guild)
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    panel_message = make_message(message_id=888)
    with patch(
        "services.temp_channels.send_room_panel", new=AsyncMock(return_value=panel_message)
    ) as send_panel:
        await reconcile_active_temp_channels(bot)

    send_panel.assert_awaited_once_with(channel, owner)
    bot.storage.set_panel_message_id.assert_awaited_once_with(500, 888)


async def test_reconcile_skips_backfill_when_owner_left_guild():
    row = _row(panel_message_id=None)
    guild = make_guild(1)
    guild.get_member = Mock(return_value=None)  # owner no longer in guild
    member = make_member(9, guild)
    channel = make_voice_channel(500, guild, members=[member])
    guild.get_channel = Mock(return_value=channel)
    bot = _make_bot(guild=guild)
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    with patch("services.temp_channels.send_room_panel", new=AsyncMock()) as send_panel:
        await reconcile_active_temp_channels(bot)

    send_panel.assert_not_called()
    bot.storage.set_panel_message_id.assert_not_called()
    # The room itself is still tracked; only the backfill is deferred.
    assert bot.active_temp_channel_ids == {500}


async def test_reconcile_does_not_backfill_when_panel_already_set():
    row = _row(panel_message_id="777")
    guild = make_guild(1)
    owner = make_member(42, guild)
    guild.get_member = Mock(return_value=owner)
    channel = make_voice_channel(500, guild, members=[owner])
    guild.get_channel = Mock(return_value=channel)
    bot = _make_bot(guild=guild)
    bot.storage.list_active_temp_channels = AsyncMock(return_value=[row])

    with patch("services.temp_channels.send_room_panel", new=AsyncMock()) as send_panel:
        await reconcile_active_temp_channels(bot)

    send_panel.assert_not_called()
    bot.storage.set_panel_message_id.assert_not_called()


# --- refcounted user_creation_locks under concurrency ----------------------


async def test_create_temp_room_lock_evicted_after_concurrent_calls():
    guild = make_guild(1)
    member = make_member(7, guild)
    lobby = make_voice_channel(100, guild, category=None)

    created_channels = []

    async def _create_voice_channel(**kwargs):
        channel = make_voice_channel(200 + len(created_channels), guild)
        created_channels.append(channel)
        return channel

    guild.create_voice_channel = AsyncMock(side_effect=_create_voice_channel)

    storage = types.SimpleNamespace(
        get_active_temp_channel_by_owner=AsyncMock(return_value=None),
        get_guild_config=AsyncMock(return_value=None),
        create_active_temp_channel=AsyncMock(),
        delete_active_temp_channel=AsyncMock(),
        set_panel_message_id=AsyncMock(),
    )
    bot = types.SimpleNamespace(
        storage=storage,
        active_temp_channel_ids=set(),
        user_creation_locks={},
        get_guild=Mock(return_value=guild),
        fetch_channel=AsyncMock(return_value=None),
    )

    profile = {
        "id": "profile-1",
        "target_category_id": None,
        "default_user_limit": None,
        "temp_name_template": None,
    }

    with patch(
        "services.temp_channels.send_room_panel",
        new=AsyncMock(return_value=make_message(message_id=1)),
    ), patch("services.temp_channels.notify_duplicate_room", new=AsyncMock()):
        await asyncio.gather(
            *(create_temp_room(bot, member, lobby, profile) for _ in range(5))
        )

    assert bot.user_creation_locks == {}
