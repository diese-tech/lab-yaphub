"""Tests for services/ownership.py's authorization gate.

_authorize_channel is the single choke point used by both
resolve_owned_temp_channel (slash commands, voice-state based) and
resolve_owned_temp_channel_by_id (panel buttons, channel-id based):
- the recorded owner is always allowed
- a non-owner needs Manage Channels AND to be physically connected to the
  room (channel.members) -- the presence check closes a bug where an admin
  elsewhere in the server could silently manage a room they never joined.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from services.ownership import (
    _authorize_channel,
    resolve_owned_temp_channel,
    resolve_owned_temp_channel_by_id,
)
from tests.conftest import make_interaction, make_member, make_voice_channel

NOT_TRACKED_MESSAGE = "That voice channel is not a tracked YapHub temp room."
NOT_OWNER_OR_ADMIN_MESSAGE = "Only the room owner or a Manage Channels admin can do that."
NOT_PRESENT_MESSAGE = "You must be connected to this voice channel to manage it as an admin."


def _record(guild_id: int, owner_id: int) -> dict:
    return {"guild_id": str(guild_id), "owner_user_id": str(owner_id)}


def _storage(record) -> AsyncMock:
    storage = AsyncMock()
    storage.get_active_temp_channel = AsyncMock(return_value=record)
    return storage


@pytest.fixture
def guild(guild_factory):
    return guild_factory(guild_id=1)


async def test_owner_is_allowed_even_when_absent_from_channel(guild):
    owner = make_member(1, guild, manage_channels=False)
    channel = make_voice_channel(500, guild, members=[])  # owner not present
    interaction = make_interaction(owner, guild)
    storage = _storage(_record(guild.id, owner.id))

    allowed = await _authorize_channel(interaction, storage, channel)

    assert allowed is True
    interaction.response.send_message.assert_not_called()


async def test_non_owner_admin_present_is_allowed(guild):
    owner_id = 1
    admin = make_member(2, guild, manage_channels=True)
    channel = make_voice_channel(500, guild, members=[admin])
    interaction = make_interaction(admin, guild)
    storage = _storage(_record(guild.id, owner_id))

    allowed = await _authorize_channel(interaction, storage, channel)

    assert allowed is True
    interaction.response.send_message.assert_not_called()


async def test_non_owner_admin_absent_is_denied_with_presence_message(guild):
    owner_id = 1
    admin = make_member(2, guild, manage_channels=True)
    channel = make_voice_channel(500, guild, members=[])  # admin not connected
    interaction = make_interaction(admin, guild)
    storage = _storage(_record(guild.id, owner_id))

    allowed = await _authorize_channel(interaction, storage, channel)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        NOT_PRESENT_MESSAGE, ephemeral=True
    )


async def test_non_owner_non_admin_is_denied_regardless_of_presence(guild):
    owner_id = 1
    other = make_member(2, guild, manage_channels=False)
    channel = make_voice_channel(500, guild, members=[other])  # even though present
    interaction = make_interaction(other, guild)
    storage = _storage(_record(guild.id, owner_id))

    allowed = await _authorize_channel(interaction, storage, channel)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        NOT_OWNER_OR_ADMIN_MESSAGE, ephemeral=True
    )


async def test_untracked_channel_is_denied(guild):
    member = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[member])
    interaction = make_interaction(member, guild)
    storage = _storage(None)

    allowed = await _authorize_channel(interaction, storage, channel)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        NOT_TRACKED_MESSAGE, ephemeral=True
    )


async def test_record_from_different_guild_is_denied(guild):
    other_guild_id = 999
    member = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[member])
    interaction = make_interaction(member, guild)
    storage = _storage(_record(other_guild_id, member.id))

    allowed = await _authorize_channel(interaction, storage, channel)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        NOT_TRACKED_MESSAGE, ephemeral=True
    )


# --- resolve_owned_temp_channel (voice-state based) ------------------------


async def test_resolve_owned_temp_channel_requires_guild_member():
    interaction = make_interaction(user=object(), guild=None)
    storage = AsyncMock()

    result = await resolve_owned_temp_channel(interaction, storage)

    assert result is None
    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used in a server.", ephemeral=True
    )


async def test_resolve_owned_temp_channel_requires_being_in_voice(guild):
    member = make_member(1, guild)
    member.voice = None
    interaction = make_interaction(member, guild)
    storage = AsyncMock()

    result = await resolve_owned_temp_channel(interaction, storage)

    assert result is None
    interaction.response.send_message.assert_awaited_once_with(
        "Join your Yap room before using this command.", ephemeral=True
    )


async def test_resolve_owned_temp_channel_returns_channel_for_owner(guild):
    member = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[member])
    member.voice = types.SimpleNamespace(channel=channel)
    interaction = make_interaction(member, guild)
    storage = _storage(_record(guild.id, member.id))

    result = await resolve_owned_temp_channel(interaction, storage)

    assert result is channel


# --- resolve_owned_temp_channel_by_id (panel buttons) -----------------------


async def test_resolve_owned_temp_channel_by_id_requires_guild_member():
    interaction = make_interaction(user=object(), guild=None)
    storage = AsyncMock()

    result = await resolve_owned_temp_channel_by_id(interaction, storage, channel_id=500)

    assert result is None
    interaction.response.send_message.assert_awaited_once_with(
        "This command can only be used in a server.", ephemeral=True
    )


async def test_resolve_owned_temp_channel_by_id_channel_gone(guild):
    member = make_member(1, guild)
    guild.get_channel = lambda channel_id: None
    interaction = make_interaction(member, guild)
    storage = AsyncMock()

    result = await resolve_owned_temp_channel_by_id(interaction, storage, channel_id=500)

    assert result is None
    interaction.response.send_message.assert_awaited_once_with(
        "This Yap room no longer exists.", ephemeral=True
    )


async def test_resolve_owned_temp_channel_by_id_returns_channel_for_owner(guild):
    member = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[member])
    guild.get_channel = lambda channel_id: channel if channel_id == 500 else None
    interaction = make_interaction(member, guild)
    storage = _storage(_record(guild.id, member.id))

    result = await resolve_owned_temp_channel_by_id(interaction, storage, channel_id=500)

    assert result is channel
