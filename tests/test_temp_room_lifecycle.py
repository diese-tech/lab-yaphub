"""Failure-boundary, ownership and guild-isolation tests for the temp-room
lifecycle.

Each test walks the room lifecycle to one boundary, fails it the way Discord
or SQLite fails, and then asserts the resulting state on all four axes that
matter: what Discord holds, what YapHub tracks, whether the user can retry,
and whether a duplicate could form.

The boundaries covered here, in lifecycle order:

    profile validation -> existing-room lookup -> permission preflight ->
    channel creation -> tracking write -> member move -> panel post ->
    departure -> cleanup -> reconciliation -> restart
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from config import RECONCILE_MIN_ROOM_AGE_SECONDS
from services.temp_channels import (
    UnresolvableTempChannel,
    cleanup_temp_channel,
    create_temp_room,
    reconcile_active_temp_channels,
    resolve_existing_owned_channel,
    runtime_active_channel_ids,
)
from storage import Storage
from tests.conftest import (
    FakeDiscord,
    forbidden,
    http_error,
    make_bot,
    make_category,
    make_notfound,
    make_permissions,
    make_profile,
    make_voice_channel,
)

LOBBY_ID = 100


@pytest.fixture
async def env(tmp_path):
    world = FakeDiscord(guild_id=1)
    lobby = world.add_channel(LOBBY_ID, name="Join to Yap")
    storage = Storage(str(tmp_path / "yaphub.sqlite3"))
    await storage.initialize()
    bot = make_bot(storage)
    bot.get_guild = lambda guild_id: world.guild if guild_id == 1 else None
    return {"world": world, "lobby": lobby, "storage": storage, "bot": bot}


async def _create(env, member, profile=None, lobby=None):
    with patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)), patch(
        "services.temp_channels.notify_duplicate_room", new=AsyncMock()
    ) as notify:
        await create_temp_room(
            env["bot"],
            member,
            lobby or env["lobby"],
            profile or make_profile(join_channel_id=LOBBY_ID),
        )
    return notify


def _rooms(env) -> set[int]:
    return env["world"].live_channel_ids - {LOBBY_ID}


def _backdate(storage: Storage, channel_id: int, seconds: int = 3600) -> None:
    created = (datetime.now(UTC) - timedelta(seconds=seconds)).replace(microsecond=0)
    connection = sqlite3.connect(storage.database_path)
    with connection:
        connection.execute(
            "update active_temp_channels set created_at = ? where channel_id = ?",
            (created.isoformat(), str(channel_id)),
        )
    connection.close()


# --- boundary: profile validation ------------------------------------------


async def test_a_profile_from_another_guild_never_creates_a_room(env):
    """The profile cache is keyed by lobby id across every guild.

    A stale or crossed entry must not let one guild's configuration create a
    room in another guild.
    """
    member = env["world"].make_member(7)
    foreign_profile = make_profile(guild_id=999, join_channel_id=LOBBY_ID)

    await _create(env, member, profile=foreign_profile)

    assert _rooms(env) == set()
    assert await env["storage"].list_active_temp_channels() == []


async def test_a_profile_without_a_guild_id_still_works(env):
    """Older cached rows may predate the check; they must not be rejected."""
    member = env["world"].make_member(7)
    profile = make_profile(join_channel_id=LOBBY_ID)
    profile.pop("guild_id")

    await _create(env, member, profile=profile)

    assert len(_rooms(env)) == 1


# --- boundary: permission preflight ----------------------------------------


async def test_missing_move_members_refuses_before_creating_anything(env, caplog):
    """No channel at all beats a channel nobody can be moved into.

    This is the incident's precondition: creating the room first and
    discovering the 403 afterwards is what produced a resource to orphan.
    """
    env["lobby"].permissions_for = Mock(
        return_value=make_permissions(move_members=False)
    )
    member = env["world"].make_member(7)

    await _create(env, member)

    assert _rooms(env) == set()
    assert await env["storage"].list_active_temp_channels() == []
    assert "reason=missing_permissions" in caplog.text
    assert "Move Members" in caplog.text


async def test_missing_manage_channels_in_the_target_category_refuses(env):
    category = make_category(
        300, env["world"].guild, permissions=make_permissions(manage_channels=False)
    )
    env["world"].guild.get_channel = Mock(
        side_effect=lambda cid: category if cid == 300 else env["world"].get_channel(cid)
    )
    member = env["world"].make_member(7)

    await _create(env, member, profile=make_profile(target_category_id=300))

    assert _rooms(env) == set()


async def test_preflight_fails_open_when_the_bot_member_is_not_cached(env):
    """An unknown answer must never be treated as "no permission"."""
    env["world"].guild.me = None
    member = env["world"].make_member(7)

    await _create(env, member)

    assert len(_rooms(env)) == 1


async def test_preflight_does_not_replace_exception_handling(env):
    """Discord can still answer 403 for a request the preflight approved."""
    member = env["world"].make_member(7)
    env["world"].move_error = forbidden()

    await _create(env, member)

    # Rolled back cleanly rather than left as an orphan.
    assert _rooms(env) == set()
    assert await env["storage"].list_active_temp_channels() == []


# --- boundary: channel creation ---------------------------------------------


async def test_a_refused_channel_creation_writes_no_tracking(env):
    member = env["world"].make_member(7)
    env["world"].guild.create_voice_channel = AsyncMock(side_effect=forbidden())

    with pytest.raises(discord.Forbidden):
        await _create(env, member)

    assert await env["storage"].list_active_temp_channels() == []
    assert env["bot"].active_temp_channel_ids == set()
    assert env["bot"].user_creation_locks == {}, "the lock must be released on error"


# --- boundary: the tracking write -------------------------------------------


async def test_a_failed_tracking_write_rolls_the_channel_back(env, caplog):
    """A created room that cannot be recorded is an orphan by construction."""
    caplog.set_level(logging.INFO, logger="yaphub")
    member = env["world"].make_member(7)
    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.get_active_temp_channel_by_owner = AsyncMock(return_value=None)
    env["bot"].storage.get_guild_config = AsyncMock(return_value=None)
    env["bot"].storage.create_active_temp_channel = AsyncMock(
        side_effect=sqlite3.OperationalError("database is locked")
    )

    await _create(env, member)

    assert _rooms(env) == set(), "the unrecordable room must not survive"
    assert "temp_room persist_failed" in caplog.text
    assert "temp_room persist_rollback_ok" in caplog.text


async def test_an_unrollbackable_tracking_failure_is_logged_as_critical(env, caplog):
    member = env["world"].make_member(7)
    env["world"].delete_error = forbidden()
    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.get_active_temp_channel_by_owner = AsyncMock(return_value=None)
    env["bot"].storage.get_guild_config = AsyncMock(return_value=None)
    env["bot"].storage.create_active_temp_channel = AsyncMock(
        side_effect=sqlite3.OperationalError("database is locked")
    )

    await _create(env, member)

    # Nothing can be done about it, but it must be findable in the logs
    # rather than silently invisible.
    assert "temp_room orphan_untracked" in caplog.text
    assert len(_rooms(env)) == 1


# --- boundary: existing-room lookup -----------------------------------------


async def test_an_unfetchable_existing_room_blocks_creation(env):
    """A 403 on the recorded room is not proof it is gone.

    Creating a second room here would fire `insert or replace` over the
    first room's record and orphan a live channel.
    """
    member = env["world"].make_member(7)
    await env["storage"].create_active_temp_channel(
        channel_id=555, guild_id=1, profile_id="profile-1", owner_user_id=7
    )
    env["bot"].fetch_channel = AsyncMock(side_effect=forbidden())

    await _create(env, member)

    assert _rooms(env) == set()
    assert await env["storage"].get_active_temp_channel(555) is not None


async def test_a_recorded_room_confirmed_absent_is_replaced(env):
    member = env["world"].make_member(7)
    await env["storage"].create_active_temp_channel(
        channel_id=555, guild_id=1, profile_id="profile-1", owner_user_id=7
    )

    await _create(env, member)  # fetch_channel answers 404 by default

    assert await env["storage"].get_active_temp_channel(555) is None
    assert len(_rooms(env)) == 1


async def test_resolve_raises_rather_than_reporting_no_room_on_a_5xx(env):
    await env["storage"].create_active_temp_channel(
        channel_id=555, guild_id=1, profile_id="profile-1", owner_user_id=7
    )
    env["bot"].fetch_channel = AsyncMock(side_effect=http_error(503))

    with pytest.raises(UnresolvableTempChannel):
        await resolve_existing_owned_channel(env["bot"], env["world"].guild, 7)

    assert await env["storage"].get_active_temp_channel(555) is not None


async def test_an_empty_existing_room_that_cannot_be_deleted_blocks_creation(env):
    member = env["world"].make_member(7)
    stale = env["world"].add_channel(555, members=[])
    await env["storage"].create_active_temp_channel(
        channel_id=555, guild_id=1, profile_id="profile-1", owner_user_id=7
    )
    env["world"].delete_error = forbidden()

    notify = await _create(env, member)

    assert _rooms(env) == {stale.id}
    assert env["world"].guild.create_voice_channel.await_count == 0
    notify.assert_awaited_once()


# --- boundary: ownership proof ----------------------------------------------


async def test_an_untracked_channel_with_a_yaphub_shaped_name_is_never_deleted(env):
    """Names are not identity.

    A member who hand-makes a channel called "user7's Yap" must not have it
    reaped by reconciliation or by cleanup.
    """
    lookalike = env["world"].add_channel(777, name="user7's Yap", members=[])

    await reconcile_active_temp_channels(env["bot"])
    await cleanup_temp_channel(env["bot"], lookalike)

    assert lookalike.id in env["world"].live_channel_ids
    lookalike.delete.assert_not_called()


async def test_cleanup_ignores_a_channel_that_is_not_tracked(env):
    unrelated = make_voice_channel(888, env["world"].guild, members=[])

    await cleanup_temp_channel(env["bot"], unrelated)

    unrelated.delete.assert_not_called()


async def test_reconcile_never_resolves_a_record_to_another_guilds_channel(env, caplog):
    """bot.fetch_channel is process-wide, not guild-scoped."""
    other_guild = FakeDiscord(guild_id=2, first_channel_id=900)
    foreign = other_guild.add_channel(901, members=[])
    await env["storage"].create_active_temp_channel(
        channel_id=901, guild_id=1, profile_id="profile-1", owner_user_id=7
    )
    _backdate(env["storage"], 901)
    env["bot"].fetch_channel = AsyncMock(return_value=foreign)

    await reconcile_active_temp_channels(env["bot"])

    assert foreign.id in other_guild.live_channel_ids
    foreign.delete.assert_not_called()
    assert "guild_mismatch" in caplog.text
    # Nor is the record silently dropped: it describes a channel that exists,
    # so forgetting it would be the incident's mistake in a different place.
    assert await env["storage"].get_active_temp_channel(901) is not None
    assert env["bot"].active_temp_channel_ids == {901}


async def test_creation_is_blocked_when_the_owners_record_points_at_another_guild(env):
    member = env["world"].make_member(7)
    other_guild = FakeDiscord(guild_id=2, first_channel_id=900)
    foreign = other_guild.add_channel(901, members=[])
    await env["storage"].create_active_temp_channel(
        channel_id=901, guild_id=1, profile_id="p", owner_user_id=7
    )
    env["bot"].fetch_channel = AsyncMock(return_value=foreign)

    await _create(env, member)

    assert _rooms(env) == set()
    assert await env["storage"].get_active_temp_channel(901) is not None


# --- boundary: guild isolation ----------------------------------------------


async def test_two_guilds_track_rooms_independently(env, tmp_path):
    other = FakeDiscord(guild_id=2, first_channel_id=500)
    other_lobby = other.add_channel(400, name="Join to Yap")
    env["bot"].get_guild = lambda gid: {1: env["world"].guild, 2: other.guild}.get(gid)

    member_a = env["world"].make_member(7)
    member_b = other.make_member(7)  # deliberately the same member id

    await _create(env, member_a)
    await _create(
        env,
        member_b,
        profile=make_profile(guild_id=2, join_channel_id=400),
        lobby=other_lobby,
    )

    rooms = await env["storage"].list_active_temp_channels()
    assert len(rooms) == 2
    assert {int(row["guild_id"]) for row in rooms} == {1, 2}
    assert len(_rooms(env)) == 1
    assert len(other.live_channel_ids - {400}) == 1


async def test_one_guilds_failure_does_not_touch_another_guilds_rooms(env):
    other = FakeDiscord(guild_id=2, first_channel_id=500)
    other_lobby = other.add_channel(400, name="Join to Yap")
    env["bot"].get_guild = lambda gid: {1: env["world"].guild, 2: other.guild}.get(gid)

    member_b = other.make_member(8)
    await _create(
        env,
        member_b,
        profile=make_profile(guild_id=2, join_channel_id=400),
        lobby=other_lobby,
    )
    healthy_room_id = next(iter(other.live_channel_ids - {400}))

    # Guild 1 goes fully unreachable.
    env["world"].move_error = forbidden()
    env["world"].delete_error = forbidden()
    member_a = env["world"].make_member(7)
    await _create(env, member_a)

    assert healthy_room_id in other.live_channel_ids
    assert await env["storage"].get_active_temp_channel(healthy_room_id) is not None


async def test_one_users_stuck_room_does_not_block_another_user(env):
    stuck = env["world"].make_member(7)
    env["world"].move_error = forbidden()
    env["world"].delete_error = forbidden()
    await _create(env, stuck)

    env["world"].move_error = None
    env["world"].delete_error = None
    healthy = env["world"].make_member(8)
    await _create(env, healthy)

    rooms = await env["storage"].list_active_temp_channels()
    assert {int(row["owner_user_id"]) for row in rooms} == {7, 8}
    assert len(_rooms(env)) == 2


# --- boundary: concurrency ---------------------------------------------------


async def test_many_distinct_users_creating_at_once_get_one_room_each(env):
    members = [env["world"].make_member(member_id) for member_id in range(10, 20)]

    with patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)), patch(
        "services.temp_channels.notify_duplicate_room", new=AsyncMock()
    ):
        await asyncio.gather(
            *(
                create_temp_room(
                    env["bot"], member, env["lobby"], make_profile(join_channel_id=LOBBY_ID)
                )
                for member in members
            )
        )

    rooms = await env["storage"].list_active_temp_channels()
    assert len(rooms) == 10
    assert len(_rooms(env)) == 10
    assert env["bot"].user_creation_locks == {}


async def test_the_creation_lock_is_released_when_the_body_raises(env):
    member = env["world"].make_member(7)
    env["world"].guild.create_voice_channel = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await _create(env, member)

    assert env["bot"].user_creation_locks == {}


async def test_a_reconcile_pass_does_not_forget_a_room_created_during_it(env):
    """Reconcile rewrites active_temp_channel_ids wholesale.

    A room created while the pass was running is not in the snapshot it read
    from SQLite, so a naive reassignment drops it from the in-memory set --
    and cleanup_temp_channel then ignores that live room entirely.
    """
    member = env["world"].make_member(7)
    original_list = env["storage"].list_active_temp_channels

    async def _list_then_create(*args, **kwargs):
        rows = await original_list(*args, **kwargs)
        await _create(env, member)  # a lobby join lands mid-pass
        return rows

    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.list_active_temp_channels = AsyncMock(side_effect=_list_then_create)

    await reconcile_active_temp_channels(env["bot"])

    new_room_id = next(iter(_rooms(env)))
    assert new_room_id in env["bot"].active_temp_channel_ids
    assert await env["storage"].get_active_temp_channel(new_room_id) is not None


async def test_reconcile_leaves_a_brand_new_empty_room_alone(env):
    """The member has not arrived in it yet; it is not an orphan."""
    member = env["world"].make_member(7)
    env["world"].move_error = None
    await _create(env, member)
    room_id = next(iter(_rooms(env)))
    # Simulate the window before the gateway reports the member in the room.
    env["world"].channels[room_id].members.clear()

    await reconcile_active_temp_channels(env["bot"])

    assert room_id in _rooms(env)
    assert await env["storage"].get_active_temp_channel(room_id) is not None
    assert env["bot"].active_temp_channel_ids == {room_id}


async def test_reconcile_reaps_the_same_room_once_it_is_past_the_grace_window(env):
    member = env["world"].make_member(7)
    await _create(env, member)
    room_id = next(iter(_rooms(env)))
    env["world"].channels[room_id].members.clear()
    _backdate(env["storage"], room_id, seconds=RECONCILE_MIN_ROOM_AGE_SECONDS + 60)

    await reconcile_active_temp_channels(env["bot"])

    assert _rooms(env) == set()
    assert await env["storage"].get_active_temp_channel(room_id) is None


# --- boundary: reconciliation robustness ------------------------------------


async def test_one_bad_row_does_not_abort_the_whole_reconcile_pass(env, caplog):
    """Reconciliation is the recovery path; it must survive a bad row.

    A raise here used to propagate into discord.ext.tasks, which stops a
    loop that raises -- killing reconciliation for the life of the process.
    """
    good = env["world"].add_channel(601, members=[])
    env["world"].add_channel(602, members=[])
    for channel_id, owner in ((601, 11), (602, 12)):
        await env["storage"].create_active_temp_channel(
            channel_id=channel_id, guild_id=1, profile_id="p", owner_user_id=owner
        )
        _backdate(env["storage"], channel_id)

    # Make the first row's cleanup blow up in a way reconcile does not model.
    original_delete = env["storage"].delete_active_temp_channel
    calls = {"n": 0}

    async def _flaky_delete(channel_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient storage failure")
        await original_delete(channel_id)

    env["bot"].storage = Mock(wraps=env["storage"])
    env["bot"].storage.delete_active_temp_channel = AsyncMock(side_effect=_flaky_delete)

    await reconcile_active_temp_channels(env["bot"])

    assert "reconcile_row_failed" in caplog.text
    # The second row was still processed despite the first one blowing up.
    assert calls["n"] == 2
    # The failed row keeps its tracking: nothing proved it was gone.
    assert good.id in env["bot"].active_temp_channel_ids


async def test_reconcile_keeps_tracking_a_room_it_cannot_see(env):
    env["world"].add_channel(700, members=[])
    await env["storage"].create_active_temp_channel(
        channel_id=700, guild_id=1, profile_id="p", owner_user_id=7
    )
    env["world"].guild.get_channel = Mock(return_value=None)
    env["bot"].fetch_channel = AsyncMock(side_effect=forbidden())

    await reconcile_active_temp_channels(env["bot"])

    assert await env["storage"].get_active_temp_channel(700) is not None
    assert env["bot"].active_temp_channel_ids == {700}


async def test_reconcile_drops_a_record_only_on_a_confirmed_404(env):
    await env["storage"].create_active_temp_channel(
        channel_id=701, guild_id=1, profile_id="p", owner_user_id=7
    )
    env["bot"].fetch_channel = AsyncMock(side_effect=make_notfound(message="Unknown Channel"))

    await reconcile_active_temp_channels(env["bot"])

    assert await env["storage"].get_active_temp_channel(701) is None
    assert env["bot"].active_temp_channel_ids == set()


# --- boundary: restart -------------------------------------------------------


async def test_restart_rebuilds_tracking_from_sqlite(env):
    member = env["world"].make_member(7)
    await _create(env, member)
    room_id = next(iter(_rooms(env)))

    restarted = make_bot(Storage(env["storage"].database_path))
    restarted.active_temp_channel_ids = await runtime_active_channel_ids(restarted)

    assert restarted.active_temp_channel_ids == {room_id}


async def test_a_room_deleted_while_the_bot_was_down_is_reconciled_away(env):
    member = env["world"].make_member(7)
    await _create(env, member)
    room_id = next(iter(_rooms(env)))

    # An admin deleted the room in Discord while the process was stopped.
    del env["world"].channels[room_id]
    env["bot"].fetch_channel = AsyncMock(side_effect=make_notfound(message="Unknown Channel"))

    await reconcile_active_temp_channels(env["bot"])

    assert await env["storage"].get_active_temp_channel(room_id) is None
    assert env["bot"].active_temp_channel_ids == set()

    # And the owner can create a new one.
    await _create(env, member)
    assert len(_rooms(env)) == 1
