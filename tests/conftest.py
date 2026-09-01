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
import types
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


BOT_MEMBER_ID = 777000


def make_guild(
    guild_id: int = 1000,
    *,
    name: str = "Test Guild",
    bot_member_id: int = BOT_MEMBER_ID,
) -> Mock:
    guild = Mock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    guild.default_role = Mock(spec=discord.Role)
    guild.default_role.id = 0
    guild.system_channel = None
    guild.get_member = Mock(return_value=None)
    guild.get_channel = Mock(return_value=None)
    guild.create_voice_channel = AsyncMock()
    # guild.me is YapHub's own member object. It is not a voice member of any
    # room, so lock/hide have to give it an explicit overwrite or the
    # @everyone deny locks the bot out of the room it is managing.
    guild.me = make_member(bot_member_id, guild, is_bot=True, display_name="YapHub")
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


def make_permissions(**overrides) -> discord.Permissions:
    """Effective permissions for a scope, defaulting to everything YapHub
    needs. Tests that care about a missing permission pass it as False."""
    permissions = discord.Permissions.all()
    permissions.update(**overrides)
    return permissions


def make_category(
    category_id: int,
    guild: Mock,
    *,
    name: str = "Yap Category",
    permissions: discord.Permissions | None = None,
) -> Mock:
    category = Mock(spec=discord.CategoryChannel)
    category.id = category_id
    category.guild = guild
    category.name = name
    category.overwrites = {}
    category.overwrites_for = Mock(
        side_effect=lambda target: category.overwrites.get(target, discord.PermissionOverwrite())
    )
    category.permissions_for = Mock(
        return_value=permissions if permissions is not None else make_permissions()
    )
    return category


def make_voice_channel(
    channel_id: int,
    guild: Mock,
    *,
    members: list | None = None,
    name: str = "Yap Room",
    user_limit: int = 0,
    category=None,
    permissions: discord.Permissions | None = None,
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
    # Real channels resolve permissions for a member; the temp-room preflight
    # reads this to decide whether creating a room it cannot move anyone into
    # is even worth attempting.
    channel.permissions_for = Mock(
        return_value=permissions if permissions is not None else make_permissions()
    )

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

    # Mirrors the real object: once the interaction has been acknowledged --
    # by defer() or by send_message() -- is_done() flips and further replies
    # have to go through followup. Tests for the deferred room actions depend
    # on that transition being modelled rather than pinned to a constant.
    state = {"done": is_done}
    response.is_done = Mock(side_effect=lambda: state["done"])

    def _acknowledge(*args, **kwargs) -> None:
        state["done"] = True

    async def _defer(*args, **kwargs) -> None:
        _acknowledge()

    response.defer = AsyncMock(side_effect=_defer)
    response.send_message.side_effect = _acknowledge
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
    response = types.SimpleNamespace(status=status, reason="Not Found")
    return discord.NotFound(response, message)


def make_profile(
    profile_id: str = "profile-1",
    *,
    guild_id: int | None = 1,
    join_channel_id: int = 100,
    target_category_id: int | None = None,
    default_user_limit: int | None = None,
    temp_name_template: str | None = None,
) -> dict:
    return {
        "id": profile_id,
        "guild_id": str(guild_id) if guild_id is not None else None,
        "join_channel_id": str(join_channel_id),
        "target_category_id": str(target_category_id) if target_category_id else None,
        "default_user_limit": default_user_limit,
        "temp_name_template": temp_name_template,
    }


class FakeDiscord:
    """An in-memory stand-in for one guild's channel and voice state.

    Exists so lifecycle tests can assert what Discord actually ends up
    holding -- "exactly one managed room exists" -- rather than which mock
    was called. Channel creation, deletion and member moves all mutate this
    state, and each can be made to fail the way Discord fails.
    """

    def __init__(self, guild_id: int = 1, *, first_channel_id: int = 200) -> None:
        self.guild = make_guild(guild_id)
        self.channels: dict[int, Mock] = {}
        self.created_ids: list[int] = []
        self.deleted_ids: list[int] = []
        # Set to a discord exception to make the matching operation fail.
        self.delete_error: BaseException | None = None
        self.move_error: BaseException | None = None
        self._next_channel_id = first_channel_id

        self.guild.create_voice_channel = AsyncMock(side_effect=self._create_voice_channel)
        self.guild.get_channel = Mock(side_effect=self.get_channel)

    # --- Discord side effects --------------------------------------------

    async def _create_voice_channel(self, *, name="Yap Room", category=None, **kwargs) -> Mock:
        channel_id = self._next_channel_id
        self._next_channel_id += 1
        channel = self.add_channel(channel_id, name=name, category=category)
        self.created_ids.append(channel_id)
        return channel

    def add_channel(
        self,
        channel_id: int,
        *,
        name: str = "Yap Room",
        members: list | None = None,
        category=None,
    ) -> Mock:
        channel = make_voice_channel(
            channel_id, self.guild, members=members or [], name=name, category=category
        )

        async def _delete(**kwargs) -> None:
            if self.delete_error is not None:
                raise self.delete_error
            self.channels.pop(channel_id, None)
            self.deleted_ids.append(channel_id)

        channel.delete = AsyncMock(side_effect=_delete)
        self.channels[channel_id] = channel
        return channel

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    def make_member(self, member_id: int, **kwargs) -> Mock:
        member = make_member(member_id, self.guild, **kwargs)

        async def _move_to(destination, reason=None) -> None:
            if self.move_error is not None:
                raise self.move_error
            self.move(member, destination)

        member.move_to = AsyncMock(side_effect=_move_to)
        return member

    def move(self, member: Mock, destination) -> None:
        """Apply a voice move to the fake state, as Discord's gateway would."""
        for channel in self.channels.values():
            if member in channel.members:
                channel.members.remove(member)
        if destination is not None:
            destination.members.append(member)
        member.voice = types.SimpleNamespace(channel=destination)

    # --- assertions -------------------------------------------------------

    @property
    def live_channel_ids(self) -> set[int]:
        return set(self.channels)


def make_bot(storage, **overrides):
    """A bot stand-in carrying the runtime state the services mutate."""
    bot = types.SimpleNamespace(
        storage=storage,
        active_temp_channel_ids=set(),
        profile_cache={},
        notification_cooldowns={},
        user_creation_locks={},
        get_guild=Mock(return_value=None),
        fetch_channel=AsyncMock(side_effect=make_notfound(message="Unknown Channel")),
    )
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


def forbidden(message: str = "Missing Permissions") -> discord.Forbidden:
    return discord.Forbidden(
        types.SimpleNamespace(status=403, reason="Forbidden"), message
    )


def http_error(status: int = 503, message: str = "Service Unavailable") -> discord.HTTPException:
    return discord.HTTPException(
        types.SimpleNamespace(status=status, reason="Service Unavailable"), message
    )


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


@pytest.fixture(autouse=True)
def _reset_module_level_state():
    """Clear process-global caches between tests.

    services.room_actions keeps a module-level rename-rate-limit history
    keyed by channel id. Tests reuse channel ids, so without this a rename
    test could pass or fail depending on which tests ran before it.
    """
    from services import room_actions

    room_actions._rename_history.clear()
    yield
    room_actions._rename_history.clear()
