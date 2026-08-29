"""Tests for services/permissions.py.

These cover the hide/unhide (and the identical lock/unlock) overwrite maths
directly, because that is where a hidden room can quietly become a room
YapHub itself can no longer see or clean up.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from services.permissions import (
    hide_temp_channel,
    is_hidden,
    is_locked,
    lock_temp_channel,
    unhide_temp_channel,
    unlock_temp_channel,
)
from tests.conftest import make_member, make_voice_channel


@pytest.fixture
def guild(guild_factory):
    return guild_factory(guild_id=1)


def _recording_channel(guild, *, members=(), category=None):
    """A voice channel whose set_permissions actually applies to
    channel.overwrites, so a test can assert on the resulting state rather
    than on a bare call list. Returns (channel, applied) where `applied` is
    the ordered list of (target, overwrite) writes."""
    channel = make_voice_channel(500, guild, members=list(members), category=category)
    applied: list[tuple[object, discord.PermissionOverwrite | None]] = []

    async def _set_permissions(target, *, overwrite=None, reason=None):
        applied.append((target, overwrite))
        if overwrite is None:
            channel.overwrites.pop(target, None)
        else:
            channel.overwrites[target] = overwrite

    channel.set_permissions = AsyncMock(side_effect=_set_permissions)
    return channel, applied


def _written_targets(applied):
    return [target for target, _ in applied]


# --- hide/lock must not lock the bot out ---------------------------------


async def test_hide_gives_the_bot_an_explicit_view_allow(guild):
    # Without this, the @everyone deny applies to YapHub too (it is invited
    # without Administrator and is never a voice member of the room), so the
    # bot loses sight of the room it is supposed to unhide and clean up.
    member = make_member(1, guild)
    channel, applied = _recording_channel(guild, members=[member])

    await hide_temp_channel(channel, reason="test")

    assert channel.overwrites[guild.me].view_channel is True


async def test_hide_allows_the_bot_before_denying_everyone(guild):
    # Ordering matters: a failure or timeout partway through must never leave
    # a window where @everyone is denied but the bot has no allow yet.
    member = make_member(1, guild)
    channel, applied = _recording_channel(guild, members=[member])

    await hide_temp_channel(channel, reason="test")

    targets = _written_targets(applied)
    assert targets.index(guild.me) < targets.index(guild.default_role)


def _forbidden_channel(guild, *, members=()):
    """A channel where every set_permissions call 403s -- what YapHub sees
    when it is missing Manage Roles, or when the room's category denies it
    Manage Permissions."""
    channel, applied = _recording_channel(guild, members=members)

    async def _forbidden(target, *, overwrite=None, reason=None):
        applied.append((target, overwrite))
        raise discord.Forbidden(
            types.SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
        )

    channel.set_permissions = AsyncMock(side_effect=_forbidden)
    return channel, applied


async def test_hide_aborts_before_denying_everyone_when_writes_are_forbidden(guild):
    # The failure mode that matters is not "hide didn't work" -- it is "hide
    # half-worked": @everyone denied, the bot's allow never written, and a
    # room nothing can see or clean up. Writing the bot's allow first means a
    # permission failure aborts before anything is denied, so the room is
    # simply left alone and the panel's on_error explains why.
    owner = make_member(1, guild)
    channel, applied = _forbidden_channel(guild, members=[owner])

    with pytest.raises(discord.Forbidden):
        await hide_temp_channel(channel, reason="test", owner=owner)

    assert _written_targets(applied) == [guild.me]  # one attempt, nothing after
    assert channel.overwrites == {}
    assert not is_hidden(channel)


async def test_lock_aborts_before_denying_everyone_when_writes_are_forbidden(guild):
    owner = make_member(1, guild)
    channel, applied = _forbidden_channel(guild, members=[owner])

    with pytest.raises(discord.Forbidden):
        await lock_temp_channel(channel, reason="test", owner=owner)

    assert _written_targets(applied) == [guild.me]
    assert not is_locked(channel)


async def test_lock_gives_the_bot_an_explicit_connect_allow(guild):
    member = make_member(1, guild)
    channel, applied = _recording_channel(guild, members=[member])

    await lock_temp_channel(channel, reason="test")

    assert channel.overwrites[guild.me].connect is True


# --- hide/lock grants ------------------------------------------------------


async def test_hide_denies_everyone_and_allows_members_owner_and_permits(guild):
    owner = make_member(1, guild)
    present = make_member(2, guild)
    permitted = make_member(3, guild)  # permitted but not currently in the room
    channel, _ = _recording_channel(guild, members=[owner, present])

    await hide_temp_channel(
        channel, reason="test", owner=owner, extra_allowed=(permitted,)
    )

    assert channel.overwrites[guild.default_role].view_channel is False
    assert is_hidden(channel)
    for member in (owner, present, permitted):
        assert channel.overwrites[member].view_channel is True


async def test_hide_does_not_grant_access_to_a_blocked_member(guild):
    # A blocked member is normally disconnected, but if that disconnect failed
    # they are still in channel.members -- hiding must not hand them an allow
    # and quietly undo the block.
    owner = make_member(1, guild)
    blocked = make_member(2, guild)
    channel, _ = _recording_channel(guild, members=[owner, blocked])
    channel.overwrites[blocked] = discord.PermissionOverwrite(
        view_channel=False, connect=False
    )

    await hide_temp_channel(channel, reason="test", owner=owner, denied=(blocked,))

    assert channel.overwrites[blocked].view_channel is False
    assert channel.overwrites[owner].view_channel is True


async def test_lock_does_not_grant_connect_to_a_blocked_member(guild):
    owner = make_member(1, guild)
    blocked = make_member(2, guild)
    channel, _ = _recording_channel(guild, members=[owner, blocked])
    channel.overwrites[blocked] = discord.PermissionOverwrite(
        view_channel=False, connect=False
    )

    await lock_temp_channel(channel, reason="test", owner=owner, denied=(blocked,))

    assert channel.overwrites[blocked].connect is False


async def test_hide_flips_role_level_allows_to_deny(guild):
    role = Mock(spec=discord.Role)
    role.id = 42
    channel, _ = _recording_channel(guild)
    channel.overwrites[role] = discord.PermissionOverwrite(view_channel=True)

    await hide_temp_channel(channel, reason="test")

    assert channel.overwrites[role].view_channel is False


# --- unhide/unlock must not eat permits ------------------------------------


async def test_unhide_preserves_permitted_members_view_allow(guild):
    # A permit is standing access until the member is unpermitted or the room
    # closes; one hide/unhide cycle must not silently empty the permit list.
    permitted = make_member(3, guild)
    ordinary = make_member(4, guild)
    channel, _ = _recording_channel(guild)
    channel.overwrites[permitted] = discord.PermissionOverwrite(
        view_channel=True, connect=True
    )
    channel.overwrites[ordinary] = discord.PermissionOverwrite(view_channel=True)
    channel.overwrites[guild.default_role] = discord.PermissionOverwrite(
        view_channel=False
    )

    await unhide_temp_channel(channel, reason="test", preserve=(permitted,))

    assert channel.overwrites[permitted].view_channel is True
    assert ordinary not in channel.overwrites
    assert not is_hidden(channel)


async def test_unlock_preserves_permitted_members_connect_allow(guild):
    permitted = make_member(3, guild)
    ordinary = make_member(4, guild)
    channel, _ = _recording_channel(guild)
    channel.overwrites[permitted] = discord.PermissionOverwrite(
        view_channel=True, connect=True
    )
    channel.overwrites[ordinary] = discord.PermissionOverwrite(connect=True)
    channel.overwrites[guild.default_role] = discord.PermissionOverwrite(connect=False)

    await unlock_temp_channel(channel, reason="test", preserve=(permitted,))

    assert channel.overwrites[permitted].connect is True
    assert ordinary not in channel.overwrites
    assert not is_locked(channel)


async def test_unhide_preserves_the_bots_own_allow(guild):
    # The room may still be locked, and the bot's overwrite is what keeps it
    # able to manage the room at all.
    channel, _ = _recording_channel(guild)
    channel.overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True)
    channel.overwrites[guild.default_role] = discord.PermissionOverwrite(
        view_channel=False
    )

    await unhide_temp_channel(channel, reason="test")

    assert channel.overwrites[guild.me].view_channel is True


async def test_unhide_restores_role_denies_from_the_category(guild):
    role = Mock(spec=discord.Role)
    role.id = 42
    category = Mock(spec=discord.CategoryChannel)
    category.overwrites_for = Mock(
        return_value=discord.PermissionOverwrite(view_channel=True)
    )
    channel, _ = _recording_channel(guild, category=category)
    channel.overwrites[role] = discord.PermissionOverwrite(view_channel=False)
    channel.overwrites[guild.default_role] = discord.PermissionOverwrite(
        view_channel=False
    )

    await unhide_temp_channel(channel, reason="test")

    assert channel.overwrites[role].view_channel is True


async def test_hide_then_unhide_round_trips_to_an_unrestricted_channel(guild):
    owner = make_member(1, guild)
    channel, _ = _recording_channel(guild, members=[owner])

    await hide_temp_channel(channel, reason="test", owner=owner)
    assert is_hidden(channel)

    await unhide_temp_channel(channel, reason="test")

    assert not is_hidden(channel)
    assert owner not in channel.overwrites
    # Only the bot's own allow is left behind, and it is a no-op once
    # @everyone can see the room again.
    assert channel.overwrites[guild.me].view_channel is True
