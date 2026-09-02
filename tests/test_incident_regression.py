"""Regression tests for the orphaned-duplicate-room production incident.

The incident sequence was:

    CREATE room X
    MOVE member -> 403 Forbidden
    ROLLBACK delete X -> also failed
    DROP tracking for X          <-- the bug
    next lobby event -> CREATE room Y
    ... repeated, producing a screen full of orphan rooms

PR #20 fixed step 4: when rollback deletion fails, the room still exists in
Discord, so its record is kept. These tests pin the whole matrix around that
fix -- move succeeds, rollback succeeds, rollback fails, reconciliation
recovers, reconciliation still fails -- against a real SQLite Storage and a
fake Discord that tracks which channels actually exist, so each one asserts
final system state rather than "an exception was raised".

The rule these encode: tracking is removed only when the Discord resource is
confirmed absent (404) or was just deleted successfully. Never otherwise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.temp_channels import (
    cleanup_temp_channel,
    create_temp_room,
    reconcile_active_temp_channels,
)
from storage import Storage
from tests.conftest import (
    FakeDiscord,
    forbidden,
    http_error,
    make_bot,
    make_notfound,
    make_profile,
)

LOBBY_ID = 100
OWNER_ID = 7


@pytest.fixture
async def world(tmp_path):
    """A guild with a lobby, a member, real storage and a wired-up bot."""
    discord_world = FakeDiscord(guild_id=1)
    lobby = discord_world.add_channel(LOBBY_ID, name="Join to Yap")
    member = discord_world.make_member(OWNER_ID)
    discord_world.move(member, lobby)

    storage = Storage(str(tmp_path / "yaphub.sqlite3"))
    await storage.initialize()

    bot = make_bot(storage)
    bot.get_guild = lambda guild_id: discord_world.guild if guild_id == 1 else None

    return {
        "discord": discord_world,
        "lobby": lobby,
        "member": member,
        "storage": storage,
        "bot": bot,
        "profile": make_profile(join_channel_id=LOBBY_ID),
    }


async def _create(world, member=None):
    with patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)), patch(
        "services.temp_channels.notify_duplicate_room", new=AsyncMock()
    ) as notify:
        await create_temp_room(
            world["bot"], member or world["member"], world["lobby"], world["profile"]
        )
    return notify


async def _managed_room_ids(world) -> set[int]:
    """Rooms Discord still holds that YapHub created (the lobby excluded)."""
    return world["discord"].live_channel_ids - {LOBBY_ID}


# --- Test A: the happy path ------------------------------------------------


async def test_a_move_succeeds_leaves_exactly_one_managed_room(world):
    await _create(world)

    assert len(await _managed_room_ids(world)) == 1
    room_id = next(iter(await _managed_room_ids(world)))

    record = await world["storage"].get_active_temp_channel(room_id)
    assert record is not None
    assert int(record["owner_user_id"]) == OWNER_ID
    assert int(record["guild_id"]) == 1
    assert world["bot"].active_temp_channel_ids == {room_id}
    # The member really ended up in the room, not just "move_to was called".
    assert world["member"] in world["discord"].channels[room_id].members


# --- Test B: move fails, rollback succeeds ---------------------------------


async def test_b_failed_move_with_successful_rollback_leaves_no_trace(world):
    world["discord"].move_error = forbidden("Missing Permissions")

    await _create(world)

    assert await _managed_room_ids(world) == set()
    assert world["bot"].active_temp_channel_ids == set()
    assert await world["storage"].list_active_temp_channels() == []


async def test_b_retry_after_a_clean_rollback_can_create_a_room(world):
    world["discord"].move_error = forbidden()
    await _create(world)

    # The permission problem is resolved; the next lobby join must work.
    world["discord"].move_error = None
    await _create(world)

    managed = await _managed_room_ids(world)
    assert len(managed) == 1
    assert world["bot"].active_temp_channel_ids == managed


# --- Test C: THE incident. move fails AND rollback deletion fails ----------


async def test_c_failed_move_and_failed_rollback_keeps_the_room_tracked(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()

    await _create(world)

    managed = await _managed_room_ids(world)
    assert len(managed) == 1, "the undeletable room must still exist in Discord"
    room_id = next(iter(managed))

    # It must NOT have been forgotten: that is what turned it into an orphan.
    assert await world["storage"].get_active_temp_channel(room_id) is not None
    assert world["bot"].active_temp_channel_ids == {room_id}
    record = await world["storage"].get_active_temp_channel_by_owner(1, OWNER_ID)
    assert record is not None and int(record["channel_id"]) == room_id


async def test_c_second_attempt_is_blocked_and_creates_no_duplicate(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()

    await _create(world)
    notify = await _create(world)

    assert world["discord"].guild.create_voice_channel.await_count == 1
    assert len(await _managed_room_ids(world)) == 1
    notify.assert_awaited_once()


async def test_c_repeated_lobby_events_cannot_recreate_the_incident(world):
    """The screenshot state: one 403 amplified into many orphan rooms."""
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()

    for _ in range(12):
        await _create(world)

    assert len(await _managed_room_ids(world)) == 1
    assert len(await world["storage"].list_active_temp_channels()) == 1


async def test_c_concurrent_lobby_events_cannot_create_duplicates(world):
    import asyncio

    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()

    with patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)), patch(
        "services.temp_channels.notify_duplicate_room", new=AsyncMock()
    ):
        await asyncio.gather(
            *(
                create_temp_room(
                    world["bot"], world["member"], world["lobby"], world["profile"]
                )
                for _ in range(8)
            )
        )

    assert len(await _managed_room_ids(world)) == 1
    assert len(await world["storage"].list_active_temp_channels()) == 1
    assert world["bot"].user_creation_locks == {}


@pytest.mark.parametrize("delete_error", [forbidden(), http_error(500), http_error(503)])
async def test_c_holds_for_every_non_404_delete_failure(world, delete_error):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = delete_error

    await _create(world)

    assert len(await _managed_room_ids(world)) == 1
    assert len(await world["storage"].list_active_temp_channels()) == 1


async def test_a_404_on_rollback_is_proof_the_room_is_gone(world):
    """The one case where dropping tracking is correct.

    A 404 means Discord no longer has the channel, so keeping the record
    would block the member from ever getting a room again for a room that
    does not exist. This is the boundary PR #20 must not over-reach past.
    """
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = make_notfound(message="Unknown Channel")

    await _create(world)

    assert await world["storage"].list_active_temp_channels() == []
    assert world["bot"].active_temp_channel_ids == set()


# --- Test D: reconciliation later succeeds ---------------------------------


async def test_d_reconciliation_deletes_the_room_once_permissions_recover(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))

    # The admin fixes the bot's permissions; the room is old enough to reap.
    world["discord"].delete_error = None
    await _age_record(world["storage"], room_id)

    await reconcile_active_temp_channels(world["bot"])

    assert await _managed_room_ids(world) == set()
    assert await world["storage"].get_active_temp_channel(room_id) is None
    assert world["bot"].active_temp_channel_ids == set()


async def test_d_owner_can_create_a_new_room_after_reconciliation_cleans_up(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))

    world["discord"].delete_error = None
    world["discord"].move_error = None
    await _age_record(world["storage"], room_id)
    await reconcile_active_temp_channels(world["bot"])

    await _create(world)

    managed = await _managed_room_ids(world)
    assert len(managed) == 1
    assert room_id not in managed


# --- Test E: reconciliation still fails ------------------------------------


async def test_e_reconciliation_that_still_fails_keeps_tracking(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))
    await _age_record(world["storage"], room_id)

    await reconcile_active_temp_channels(world["bot"])

    assert room_id in await _managed_room_ids(world)
    assert await world["storage"].get_active_temp_channel(room_id) is not None
    assert world["bot"].active_temp_channel_ids == {room_id}


async def test_e_duplicate_creation_stays_blocked_after_a_failed_reconcile(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))
    await _age_record(world["storage"], room_id)

    await reconcile_active_temp_channels(world["bot"])
    await _create(world)

    assert await _managed_room_ids(world) == {room_id}
    assert world["discord"].guild.create_voice_channel.await_count == 1


async def test_e_repeated_reconciliation_is_idempotent(world):
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))
    await _age_record(world["storage"], room_id)

    for _ in range(5):
        await reconcile_active_temp_channels(world["bot"])

    assert await _managed_room_ids(world) == {room_id}
    assert len(await world["storage"].list_active_temp_channels()) == 1

    # And it still converges the moment the failure clears.
    world["discord"].delete_error = None
    await reconcile_active_temp_channels(world["bot"])
    await reconcile_active_temp_channels(world["bot"])

    assert await _managed_room_ids(world) == set()
    assert await world["storage"].list_active_temp_channels() == []


# --- restart: tracking survives the process ---------------------------------


async def test_tracking_survives_a_restart_and_still_blocks_duplicates(world, tmp_path):
    """Nothing about the safeguard may depend on in-memory state."""
    world["discord"].move_error = forbidden()
    world["discord"].delete_error = forbidden()
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))

    # A fresh process: new Storage over the same file, empty runtime caches.
    from services.temp_channels import runtime_active_channel_ids

    restarted_storage = Storage(world["storage"].database_path)
    await restarted_storage.initialize()
    restarted = make_bot(restarted_storage)
    restarted.get_guild = world["bot"].get_guild
    restarted.active_temp_channel_ids = await runtime_active_channel_ids(restarted)

    assert restarted.active_temp_channel_ids == {room_id}

    world["bot"] = restarted
    notify = await _create(world)

    assert world["discord"].guild.create_voice_channel.await_count == 1
    notify.assert_awaited_once()


# --- cleanup path parity ----------------------------------------------------


async def test_cleanup_keeps_tracking_when_the_delete_is_refused(world):
    """cleanup_temp_channel obeys the same rule as the creation rollback."""
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))
    room = world["discord"].channels[room_id]

    world["discord"].move(world["member"], None)
    world["discord"].delete_error = forbidden()

    await cleanup_temp_channel(world["bot"], room, leaver=world["member"])

    assert room_id in await _managed_room_ids(world)
    assert await world["storage"].get_active_temp_channel(room_id) is not None
    assert world["bot"].active_temp_channel_ids == {room_id}


async def test_cleanup_twice_is_safe_and_clears_tracking_once(world):
    """Two members leaving at once both reach the delete."""
    await _create(world)
    room_id = next(iter(await _managed_room_ids(world)))
    room = world["discord"].channels[room_id]
    world["discord"].move(world["member"], None)

    await cleanup_temp_channel(world["bot"], room, leaver=world["member"])
    # The channel is gone now, so the second call's delete answers 404.
    world["discord"].delete_error = make_notfound(message="Unknown Channel")
    world["bot"].active_temp_channel_ids.add(room_id)
    await cleanup_temp_channel(world["bot"], room, leaver=world["member"])

    assert await _managed_room_ids(world) == set()
    assert await world["storage"].list_active_temp_channels() == []
    assert world["bot"].active_temp_channel_ids == set()


async def _age_record(storage: Storage, channel_id: int, seconds: int = 3600) -> None:
    """Backdate a record past the reconcile grace window.

    Reconcile deliberately leaves brand-new empty rooms alone (a room whose
    member has not been moved in yet is not an orphan), so incident tests
    have to age the record to reach the cleanup branch.
    """
    import sqlite3
    from datetime import UTC, datetime, timedelta

    created = (datetime.now(UTC) - timedelta(seconds=seconds)).replace(microsecond=0)
    connection = sqlite3.connect(storage.database_path)
    with connection:
        connection.execute(
            "update active_temp_channels set created_at = ? where channel_id = ?",
            (created.isoformat(), str(channel_id)),
        )
    connection.close()
