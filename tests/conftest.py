"""Shared fixtures and mock-object factories for the YapHub test suite.

Mocking conventions (established by this repo's prior one-off manual
verification scripts, followed here rather than inventing a new style):

- discord.py model objects (Member, VoiceChannel, Guild, Interaction,
  Message, ...) are built with unittest.mock.Mock(spec=discord.X). The
  spec= matters: it makes isinstance() checks against discord.X succeed,
  which several functions under test rely on (e.g. `isinstance(channel,
  discord.VoiceChannel)`).
- Async methods on those mocks are AsyncMock().
- Lightweight stand-ins for `bot` and other non-discord collaborators use
  types.SimpleNamespace.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import discord
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def make_guild(guild_id: int = 1000, *, name: str = "Test Guild") -> Mock:
    guild = Mock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.default_role = Mock(spec=discord.Role)
    guild.default_role.id = 0
    guild.system_channel = None
    guild.get_member = Mock(return_value=None)
    guild.get_channel = Mock(return_value=None)
    guild.create_voice_channel = AsyncMock()
    return guild


def make_member(
    member_id: int,
    guild: Mock,
    *,
    manage_channels: bool = False,
    is_bot: bool = False,
    display_name: str | None = None,
) -> Mock:
    member = Mock(spec=discord.Member)
    member.id = member_id
    member.guild = guild
    member.bot = is_bot
    member.mention = f"<@{member_id}>"
    member.display_name = display_name or f"user{member_id}"
    member.voice = None
    member.guild_permissions = Mock(spec=discord.Permissions)
    member.guild_permissions.manage_channels = manage_channels
    member.move_to = AsyncMock()
    member.send = AsyncMock()
    return member


def make_voice_channel(
    channel_id: int,
    guild: Mock,
    *,
    members: list | None = None,
    name: str = "Yap Room",
    user_limit: int = 0,
    category=None,
) -> Mock:
    channel = Mock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.guild = guild
    channel.members = members if members is not None else []
    channel.mention = f"<#{channel_id}>"
    channel.name = name
    channel.user_limit = user_limit
    channel.category = category
    channel.overwrites = {}

    def _overwrites_for(target):
        return channel.overwrites.get(target, discord.PermissionOverwrite())

    channel.overwrites_for = Mock(side_effect=_overwrites_for)
    channel.set_permissions = AsyncMock()
    channel.edit = AsyncMock()
    channel.send = AsyncMock()
    channel.delete = AsyncMock()
    channel.fetch_message = AsyncMock()
    return channel


def make_response(*, is_done: bool = False) -> Mock:
    response = Mock(spec=discord.InteractionResponse)
    response.send_message = AsyncMock()
    response.send_modal = AsyncMock()
    response.edit_message = AsyncMock()
    response.is_done = Mock(return_value=is_done)
    return response


def make_interaction(
    user: Mock,
    guild: Mock | None,
    *,
    channel=None,
    client=None,
) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.channel = channel
    interaction.client = client
    interaction.response = make_response()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


def make_message(message_id: int = 999) -> Mock:
    message = Mock(spec=discord.Message)
    message.id = message_id
    message.edit = AsyncMock()
    return message


def make_notfound(status: int = 404, message: str = "Unknown Message") -> discord.NotFound:
    import types

    response = types.SimpleNamespace(status=status, reason="Not Found")
    return discord.NotFound(response, message)


@pytest.fixture
def guild_factory():
    return make_guild


@pytest.fixture
def member_factory():
    return make_member


@pytest.fixture
def channel_factory():
    return make_voice_channel


@pytest.fixture
def interaction_factory():
    return make_interaction


@pytest.fixture
def message_factory():
    return make_message


@pytest.fixture
def notfound_factory():
    return make_notfound
