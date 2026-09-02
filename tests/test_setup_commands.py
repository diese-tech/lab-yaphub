"""Tests for the admin setup surface (/yap setup, /yap profile create).

These commands create a Discord channel and then record it. That is the same
create-then-persist ordering that produced the orphaned-room incident in the
temp-room path: if the write fails, a real Discord resource exists that
nothing remembers. These tests pin the rollback, and pin that a lobby YapHub
did NOT create is never deleted.
"""

from __future__ import annotations

import sqlite3
import types
from unittest.mock import AsyncMock, Mock, patch

import pytest

from commands.profiles import ProfileGroup
from commands.setup import YapGroup
from storage import Storage
from tests.conftest import (
    forbidden,
    make_category,
    make_guild,
    make_interaction,
    make_member,
    make_voice_channel,
)


@pytest.fixture
async def env(tmp_path):
    guild = make_guild(1)
    admin = make_member(1, guild, manage_channels=True)
    storage = Storage(str(tmp_path / "yaphub.sqlite3"))
    await storage.initialize()
    bot = types.SimpleNamespace(storage=storage, profile_cache={})
    interaction = make_interaction(admin, guild, client=bot)
    return {
        "guild": guild,
        "admin": admin,
        "storage": storage,
        "bot": bot,
        "interaction": interaction,
    }


async def _run_setup(env, category=None):
    group = YapGroup(env["bot"])
    await group.setup.callback(group, env["interaction"], category)


async def _run_profile_create(env, **kwargs):
    group = ProfileGroup(env["bot"])
    params = dict(
        name="Test",
        category=None,
        lobby_channel=None,
        lobby_name=None,
        default_limit=None,
        name_template=None,
    )
    params.update(kwargs)
    await group.create.callback(group, env["interaction"], **params)


# --- /yap setup --------------------------------------------------------------


async def test_setup_creates_a_lobby_and_records_a_profile(env):
    lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    env["guild"].create_voice_channel = AsyncMock(return_value=lobby)

    await _run_setup(env)

    profiles = await env["storage"].list_profiles(1)
    assert len(profiles) == 1
    assert int(profiles[0]["join_channel_id"]) == lobby.id
    assert env["bot"].profile_cache[lobby.id] is not None
    lobby.delete.assert_not_called()


async def test_setup_requires_manage_channels(env):
    env["interaction"].user = make_member(2, env["guild"], manage_channels=False)

    await _run_setup(env)

    env["guild"].create_voice_channel.assert_not_called()
    env["interaction"].response.send_message.assert_awaited_once_with(
        "You need Manage Channels permission.", ephemeral=True
    )


async def test_setup_rolls_the_lobby_back_when_the_profile_cannot_be_saved(env):
    """Otherwise the guild is left with a dead lobby nothing points at."""
    lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    env["guild"].create_voice_channel = AsyncMock(return_value=lobby)
    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.list_profiles = AsyncMock(return_value=[])
    env["bot"].storage.get_profile_by_name = AsyncMock(return_value=None)
    env["bot"].storage.create_profile = AsyncMock(
        side_effect=sqlite3.IntegrityError("unique constraint failed")
    )

    await _run_setup(env)

    lobby.delete.assert_awaited_once()
    assert env["bot"].profile_cache == {}
    args, _ = env["interaction"].response.send_message.call_args
    assert "removed the lobby" in args[0]


async def test_setup_reports_when_the_rollback_itself_fails(env, caplog):
    lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    lobby.delete = AsyncMock(side_effect=forbidden())
    env["guild"].create_voice_channel = AsyncMock(return_value=lobby)
    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.list_profiles = AsyncMock(return_value=[])
    env["bot"].storage.get_profile_by_name = AsyncMock(return_value=None)
    env["bot"].storage.create_profile = AsyncMock(side_effect=sqlite3.IntegrityError("boom"))

    await _run_setup(env)

    assert "must be removed manually" in caplog.text
    assert env["bot"].profile_cache == {}


async def test_setup_handles_a_refused_lobby_creation(env):
    env["guild"].create_voice_channel = AsyncMock(side_effect=forbidden())

    await _run_setup(env)

    assert await env["storage"].list_profiles(1) == []
    args, _ = env["interaction"].response.send_message.call_args
    assert "Manage Channels permission" in args[0]


async def test_setup_is_idempotent_for_the_same_category(env):
    category = make_category(300, env["guild"])
    lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    env["guild"].create_voice_channel = AsyncMock(return_value=lobby)

    await _run_setup(env, category=category)
    env["interaction"] = make_interaction(env["admin"], env["guild"], client=env["bot"])
    await _run_setup(env, category=category)

    assert env["guild"].create_voice_channel.await_count == 1
    assert len(await env["storage"].list_profiles(1)) == 1
    args, _ = env["interaction"].response.send_message.call_args
    assert "already set up" in args[0]


# --- /yap profile create -----------------------------------------------------


async def test_profile_create_rolls_back_only_a_lobby_it_created(env):
    lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    env["guild"].create_voice_channel = AsyncMock(return_value=lobby)
    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.get_profile_by_name = AsyncMock(return_value=None)
    env["bot"].storage.create_profile = AsyncMock(side_effect=sqlite3.IntegrityError("boom"))

    await _run_profile_create(env)

    lobby.delete.assert_awaited_once()
    assert env["bot"].profile_cache == {}


async def test_profile_create_never_deletes_an_admin_supplied_lobby(env):
    """An existing channel the admin pointed at is not YapHub's to remove."""
    existing = make_voice_channel(500, env["guild"], name="Voice Chat")
    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.get_profile_by_name = AsyncMock(return_value=None)
    env["bot"].storage.get_profile_by_join_channel = AsyncMock(return_value=None)
    env["bot"].storage.create_profile = AsyncMock(side_effect=sqlite3.IntegrityError("boom"))

    await _run_profile_create(env, lobby_channel=existing)

    existing.delete.assert_not_called()
    env["guild"].create_voice_channel.assert_not_called()


async def test_profile_create_handles_a_refused_lobby_creation(env):
    env["guild"].create_voice_channel = AsyncMock(side_effect=forbidden())

    await _run_profile_create(env)

    assert await env["storage"].list_profiles(1) == []
    args, _ = env["interaction"].response.send_message.call_args
    assert "Manage Channels permission" in args[0]


async def test_profile_create_rejects_a_duplicate_name(env):
    await env["storage"].create_profile(
        guild_id=1,
        name="Test",
        join_channel_id=400,
        target_category_id=None,
        created_by_user_id=1,
    )

    await _run_profile_create(env)

    env["guild"].create_voice_channel.assert_not_called()
    assert len(await env["storage"].list_profiles(1)) == 1


async def test_profile_create_rejects_a_lobby_already_registered(env):
    await env["storage"].create_profile(
        guild_id=1,
        name="Existing",
        join_channel_id=500,
        target_category_id=None,
        created_by_user_id=1,
    )
    existing = make_voice_channel(500, env["guild"])

    await _run_profile_create(env, lobby_channel=existing)

    assert len(await env["storage"].list_profiles(1)) == 1
    args, _ = env["interaction"].response.send_message.call_args
    assert "already registered" in args[0]


# --- /yap reset --------------------------------------------------------------


async def test_reset_only_deletes_lobbies_it_has_records_for(env):
    """Reset resolves channels by persisted id, never by name."""
    tracked_lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    lookalike = make_voice_channel(501, env["guild"], name="Join to Yap")
    await env["storage"].create_profile(
        guild_id=1,
        name="Default",
        join_channel_id=500,
        target_category_id=None,
        created_by_user_id=1,
    )
    env["guild"].get_channel = Mock(
        side_effect=lambda cid: {500: tracked_lobby, 501: lookalike}.get(cid)
    )
    env["bot"].profile_cache = {500: {"id": "x"}}

    group = YapGroup(env["bot"])
    with patch("commands.setup.ConfirmView") as confirm_view:
        await group.reset.callback(group, env["interaction"])
        on_confirm = confirm_view.call_args.kwargs["on_confirm"]

    confirm_interaction = make_interaction(env["admin"], env["guild"], client=env["bot"])
    await on_confirm(confirm_interaction)

    tracked_lobby.delete.assert_awaited_once()
    lookalike.delete.assert_not_called()
    assert await env["storage"].list_profiles(1) == []
    assert env["bot"].profile_cache == {}


async def test_reset_survives_a_lobby_it_cannot_delete(env):
    lobby = make_voice_channel(500, env["guild"], name="Join to Yap")
    lobby.delete = AsyncMock(side_effect=forbidden())
    await env["storage"].create_profile(
        guild_id=1,
        name="Default",
        join_channel_id=500,
        target_category_id=None,
        created_by_user_id=1,
    )
    env["guild"].get_channel = Mock(return_value=lobby)

    group = YapGroup(env["bot"])
    with patch("commands.setup.ConfirmView") as confirm_view:
        await group.reset.callback(group, env["interaction"])
        on_confirm = confirm_view.call_args.kwargs["on_confirm"]

    confirm_interaction = make_interaction(env["admin"], env["guild"], client=env["bot"])
    await on_confirm(confirm_interaction)

    # The configuration is still cleared; the undeletable lobby is reported.
    assert await env["storage"].list_profiles(1) == []
    confirm_interaction.followup.send.assert_awaited_once()
