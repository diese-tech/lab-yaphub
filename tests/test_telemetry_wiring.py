"""End-to-end tests proving the real create/reconcile code paths record the
telemetry event the design says they should -- not just that the telemetry
primitives work in isolation (tests/test_storage.py, tests/test_telemetry.py
cover that), but that services/temp_channels.py actually calls them at the
right moments.

Uses the same FakeDiscord/Storage harness as the incident-regression and
load-stress suites: real Storage, real create_temp_room/reconcile code,
only Discord I/O faked. Reading a scenario's outcome from
storage.get_telemetry_summary() -- not from asserting a recorder was
called -- means these tests fail if the wiring is ever silently removed or
miscounts, not just if its call signature changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import discord
import pytest

from services.temp_channels import (
    cleanup_temp_channel,
    create_temp_room,
    reconcile_active_temp_channels,
)
from storage import Storage
from tests.conftest import FakeDiscord, forbidden, make_bot, make_profile

LOBBY_ID = 100


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "test-secret")
    world = FakeDiscord(guild_id=1)
    lobby = world.add_channel(LOBBY_ID, name="Join to Yap")
    storage = Storage(str(tmp_path / "yaphub.sqlite3"))
    await storage.initialize()
    bot = make_bot(storage)
    bot.get_guild = lambda guild_id: world.guild if guild_id == 1 else None
    return {"world": world, "lobby": lobby, "storage": storage, "bot": bot}


async def _create(env, member, profile=None):
    with patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)), patch(
        "services.temp_channels.notify_duplicate_room", new=AsyncMock()
    ):
        await create_temp_room(
            env["bot"], member, env["lobby"], profile or make_profile(join_channel_id=LOBBY_ID)
        )


async def _summary(env):
    return await env["storage"].get_telemetry_summary()


# --- success ------------------------------------------------------------


async def test_a_successful_creation_records_room_created_and_both_entities(env):
    member = env["world"].make_member(7)

    await _create(env, member)

    summary = await _summary(env)
    assert summary["rooms_created_total"] == 1
    assert summary["unique_users_served_total"] == 1
    assert summary["unique_guilds_served_total"] == 1
    assert summary["room_create_failed_total"] == 0


async def test_a_second_member_in_the_same_guild_does_not_inflate_unique_guilds(env):
    member_a = env["world"].make_member(7)
    member_b = env["world"].make_member(8)

    await _create(env, member_a)
    await _create(env, member_b)

    summary = await _summary(env)
    assert summary["rooms_created_total"] == 2
    assert summary["unique_users_served_total"] == 2
    assert summary["unique_guilds_served_total"] == 1  # same guild both times


async def test_the_same_member_creating_twice_only_counts_as_one_unique_user(env):
    member = env["world"].make_member(7)

    await _create(env, member)
    # Leave and re-create -- a legitimate second room for the same person.
    room_id = next(iter(env["world"].live_channel_ids - {LOBBY_ID}))
    room = env["world"].channels[room_id]
    env["world"].move(member, None)
    await cleanup_temp_channel(env["bot"], room, leaver=member)
    await _create(env, member)

    summary = await _summary(env)
    assert summary["rooms_created_total"] == 2
    assert summary["unique_users_served_total"] == 1


# --- failures -------------------------------------------------------------


async def test_a_failed_move_with_successful_rollback_records_room_create_failed(env):
    member = env["world"].make_member(7)
    env["world"].move_error = forbidden()

    await _create(env, member)

    summary = await _summary(env)
    assert summary["rooms_created_total"] == 0
    assert summary["room_create_failed_total"] == 1
    assert summary["unique_users_served_total"] == 0


async def test_the_incident_case_records_rollback_failed_tracking_preserved(env):
    """Move fails AND rollback fails -- the production incident's exact
    shape. This is deliberately its own counter, not folded into the
    generic room_create_failed bucket, because it is the more severe case:
    a room is left behind, not just a failed attempt."""
    member = env["world"].make_member(7)
    env["world"].move_error = forbidden()
    env["world"].delete_error = forbidden()

    await _create(env, member)

    summary = await _summary(env)
    assert summary["rollback_failed_tracking_preserved_total"] == 1
    assert summary["room_create_failed_total"] == 0
    assert summary["rooms_created_total"] == 0


async def test_a_blocked_duplicate_records_duplicate_blocked_not_room_created(env):
    member = env["world"].make_member(7)
    await _create(env, member)

    await _create(env, member)  # still owns a room -- blocked

    summary = await _summary(env)
    assert summary["rooms_created_total"] == 1
    assert summary["duplicate_blocked_total"] == 1


async def test_a_refused_channel_creation_records_room_create_failed_and_still_raises(env):
    """guild.create_voice_channel itself failing is the one branch with no
    local return to attach telemetry to -- it deliberately propagates to
    the caller (bot.py's broad except around create_temp_room) rather than
    being swallowed here. The exception must still propagate unchanged;
    only the telemetry gap is being closed."""
    member = env["world"].make_member(7)
    env["world"].guild.create_voice_channel = AsyncMock(side_effect=forbidden())

    with pytest.raises(discord.Forbidden):
        await _create(env, member)

    summary = await _summary(env)
    assert summary["room_create_failed_total"] == 1
    assert summary["rooms_created_total"] == 0


async def test_a_missing_permissions_refusal_records_room_create_failed(env):
    from tests.conftest import make_permissions

    member = env["world"].make_member(7)
    env["lobby"].permissions_for = lambda _m: make_permissions(move_members=False)

    await _create(env, member)

    summary = await _summary(env)
    assert summary["room_create_failed_total"] == 1
    assert summary["rooms_created_total"] == 0
    env["world"].guild.create_voice_channel.assert_not_called()


# --- reconciliation ---------------------------------------------------------


async def test_reconcile_deleting_an_empty_room_records_reconcile_cleanup_ok(env):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    env["world"].add_channel(500, members=[])
    await env["storage"].create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="profile-1", owner_user_id=7
    )
    old = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    connection = sqlite3.connect(env["storage"].database_path)
    with connection:
        connection.execute(
            "update active_temp_channels set created_at = ? where channel_id = ?", (old, "500")
        )
    connection.close()

    await reconcile_active_temp_channels(env["bot"])

    summary = await _summary(env)
    assert summary["reconcile_cleanup_ok_total"] == 1
    assert summary["reconcile_cleanup_failed_total"] == 0


async def test_reconcile_failing_to_delete_records_reconcile_cleanup_failed(env):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    env["world"].add_channel(501, members=[])
    env["world"].delete_error = forbidden()
    await env["storage"].create_active_temp_channel(
        channel_id=501, guild_id=1, profile_id="profile-1", owner_user_id=7
    )
    old = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    connection = sqlite3.connect(env["storage"].database_path)
    with connection:
        connection.execute(
            "update active_temp_channels set created_at = ? where channel_id = ?", (old, "501")
        )
    connection.close()

    await reconcile_active_temp_channels(env["bot"])

    summary = await _summary(env)
    assert summary["reconcile_cleanup_failed_total"] == 1
    assert summary["reconcile_cleanup_ok_total"] == 0


# --- privacy boundary --------------------------------------------------------


async def test_telemetry_never_persists_a_raw_discord_id_anywhere(env):
    """Read every telemetry table directly and confirm the real Discord
    ids used in this test never appear in them -- only their HMAC keys.

    Uses long, distinctive ids (real Discord snowflakes are ~18 digits) so
    the substring check cannot accidentally match an unrelated short number
    that happens to appear in a timestamp or a count.
    """
    import sqlite3

    distinctive_guild_id = 918273645918273645
    distinctive_user_id = 837465928374659283
    other_world = FakeDiscord(guild_id=distinctive_guild_id, first_channel_id=900)
    other_lobby = other_world.add_channel(800, name="Join to Yap")
    env["bot"].get_guild = lambda gid: other_world.guild if gid == distinctive_guild_id else None
    member = other_world.make_member(distinctive_user_id)

    with patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)):
        await create_temp_room(
            env["bot"],
            member,
            other_lobby,
            make_profile(guild_id=distinctive_guild_id, join_channel_id=other_lobby.id),
        )

    connection = sqlite3.connect(env["storage"].database_path)
    connection.row_factory = sqlite3.Row
    all_telemetry_text = []
    for table in ("telemetry_daily_counts", "telemetry_known_users", "telemetry_known_guilds"):
        for row in connection.execute(f"select * from {table}"):  # noqa: S608 -- fixed table names
            all_telemetry_text.append(" ".join(str(value) for value in tuple(row)))
    connection.close()

    blob = " ".join(all_telemetry_text)
    assert str(distinctive_guild_id) not in blob
    assert str(distinctive_user_id) not in blob


async def test_without_the_secret_configured_no_entity_table_gains_a_row(env, monkeypatch):
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)
    member = env["world"].make_member(7)

    await _create(env, member)

    summary = await _summary(env)
    assert summary["rooms_created_total"] == 1  # counting still works
    assert summary["unique_users_served_total"] == 0  # but identity recognition waits
    assert summary["unique_guilds_served_total"] == 0
