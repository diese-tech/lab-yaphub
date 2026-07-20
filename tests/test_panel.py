"""Tests for services/panel.py."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, Mock, patch

import pytest

from services.panel import RoomControlPanel, refresh_panel_message
from tests.conftest import make_interaction, make_member, make_message, make_voice_channel


@pytest.fixture
def guild(guild_factory):
    return guild_factory(guild_id=1)


# --- Rename/Limit buttons must not open a modal on denial -----------------


async def test_rename_button_does_not_open_modal_when_resolve_denies():
    view = RoomControlPanel()
    interaction = make_interaction(user=make_member(1, None), guild=None)

    with patch.object(RoomControlPanel, "_resolve", new=AsyncMock(return_value=None)):
        await view.rename_button.callback(interaction)

    interaction.response.send_modal.assert_not_called()


async def test_limit_button_does_not_open_modal_when_resolve_denies():
    view = RoomControlPanel()
    interaction = make_interaction(user=make_member(1, None), guild=None)

    with patch.object(RoomControlPanel, "_resolve", new=AsyncMock(return_value=None)):
        await view.limit_button.callback(interaction)

    interaction.response.send_modal.assert_not_called()


async def test_rename_button_opens_modal_when_resolve_allows(guild):
    view = RoomControlPanel()
    channel = make_voice_channel(500, guild)
    interaction = make_interaction(user=make_member(1, guild), guild=guild)

    with patch.object(RoomControlPanel, "_resolve", new=AsyncMock(return_value=channel)):
        await view.rename_button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()


async def test_limit_button_opens_modal_when_resolve_allows(guild):
    view = RoomControlPanel()
    channel = make_voice_channel(500, guild)
    interaction = make_interaction(user=make_member(1, guild), guild=guild)

    with patch.object(RoomControlPanel, "_resolve", new=AsyncMock(return_value=channel)):
        await view.limit_button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()


# --- refresh_panel_message --------------------------------------------


def _bot_with_record(record, *, permits=None):
    storage = types.SimpleNamespace(
        get_active_temp_channel=AsyncMock(return_value=record),
        list_permits=AsyncMock(return_value=permits or []),
    )
    return types.SimpleNamespace(storage=storage)


async def test_refresh_panel_message_edits_when_found(guild):
    owner = make_member(1, guild)
    guild.get_member = Mock(return_value=owner)
    message = make_message(message_id=777)
    channel = make_voice_channel(500, guild)
    channel.fetch_message = AsyncMock(return_value=message)
    bot = _bot_with_record({"panel_message_id": "777", "owner_user_id": "1"})

    await refresh_panel_message(bot, channel)

    channel.fetch_message.assert_awaited_once_with(777)
    message.edit.assert_awaited_once()
    _, kwargs = message.edit.call_args
    assert kwargs["embed"].title == "Yap Room Controls"


async def test_refresh_panel_message_noop_when_panel_message_id_none(guild):
    channel = make_voice_channel(500, guild)
    bot = _bot_with_record({"panel_message_id": None, "owner_user_id": "1"})

    await refresh_panel_message(bot, channel)

    channel.fetch_message.assert_not_called()


async def test_refresh_panel_message_noop_when_record_missing(guild):
    channel = make_voice_channel(500, guild)
    bot = _bot_with_record(None)

    await refresh_panel_message(bot, channel)

    channel.fetch_message.assert_not_called()


async def test_refresh_panel_message_noop_when_owner_left_guild(guild):
    guild.get_member = Mock(return_value=None)
    channel = make_voice_channel(500, guild)
    bot = _bot_with_record({"panel_message_id": "777", "owner_user_id": "1"})

    await refresh_panel_message(bot, channel)

    channel.fetch_message.assert_not_called()


async def test_refresh_panel_message_swallows_not_found(guild, notfound_factory):
    owner = make_member(1, guild)
    guild.get_member = Mock(return_value=owner)
    channel = make_voice_channel(500, guild)
    channel.fetch_message = AsyncMock(side_effect=notfound_factory())
    bot = _bot_with_record({"panel_message_id": "777", "owner_user_id": "1"})

    # Must not raise.
    await refresh_panel_message(bot, channel)

    channel.fetch_message.assert_awaited_once()
