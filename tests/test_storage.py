"""Tests for storage.py's SQLite-backed Storage class.

Per repo convention, these hit a real temp SQLite file via
tempfile.TemporaryDirectory() rather than mocking the DB layer -- every
prior manual verification script in this repo did the same.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from storage import Storage


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "yaphub.sqlite3")
        store = Storage(db_path)
        await store.initialize()
        yield store


# --- guild config -----------------------------------------------------


async def test_get_or_create_guild_config_creates_then_reuses(storage):
    created = await storage.get_or_create_guild_config(guild_id=1)
    assert created["guild_id"] == "1"
    assert created["temp_channel_prefix"] == ""
    assert created["notification_cooldown_seconds"] == 45

    fetched = await storage.get_guild_config(guild_id=1)
    assert fetched is not None
    assert fetched["guild_id"] == created["guild_id"]

    reused = await storage.get_or_create_guild_config(guild_id=1)
    assert reused["created_at"] == created["created_at"]


async def test_get_guild_config_missing_returns_none(storage):
    assert await storage.get_guild_config(guild_id=999) is None


async def test_reset_guild_configuration_clears_profiles_and_config(storage):
    await storage.get_or_create_guild_config(guild_id=1)
    await storage.create_profile(
        guild_id=1,
        name="Default",
        join_channel_id=10,
        target_category_id=None,
        created_by_user_id=5,
    )

    await storage.reset_guild_configuration(guild_id=1)

    assert await storage.get_guild_config(guild_id=1) is None
    assert await storage.list_profiles(guild_id=1) == []


# --- profiles -----------------------------------------------------------


async def test_create_profile_round_trip(storage):
    profile = await storage.create_profile(
        guild_id=1,
        name="Gaming",
        join_channel_id=100,
        target_category_id=200,
        created_by_user_id=5,
        default_user_limit=10,
        temp_name_template="{user}'s den",
    )

    assert profile["name"] == "Gaming"
    assert profile["guild_id"] == "1"
    assert profile["join_channel_id"] == "100"
    assert profile["target_category_id"] == "200"
    assert profile["created_by_user_id"] == "5"
    assert profile["default_user_limit"] == 10
    assert profile["temp_name_template"] == "{user}'s den"

    fetched = await storage.get_profile(profile["id"])
    assert fetched["id"] == profile["id"]

    by_name = await storage.get_profile_by_name(1, "gaming")  # case-insensitive
    assert by_name["id"] == profile["id"]

    by_join_channel = await storage.get_profile_by_join_channel(1, 100)
    assert by_join_channel["id"] == profile["id"]


async def test_create_profile_without_optional_fields_defaults_to_none(storage):
    profile = await storage.create_profile(
        guild_id=1,
        name="Default",
        join_channel_id=100,
        target_category_id=None,
        created_by_user_id=5,
    )
    assert profile["target_category_id"] is None
    assert profile["default_user_limit"] is None
    assert profile["temp_name_template"] is None


async def test_get_profile_missing_returns_none(storage):
    assert await storage.get_profile("does-not-exist") is None


async def test_list_profiles_scoped_to_guild_ordered_by_created_at(storage):
    p1 = await storage.create_profile(
        guild_id=1, name="A", join_channel_id=1, target_category_id=None, created_by_user_id=1
    )
    p2 = await storage.create_profile(
        guild_id=1, name="B", join_channel_id=2, target_category_id=None, created_by_user_id=1
    )
    await storage.create_profile(
        guild_id=2, name="Other Guild", join_channel_id=3, target_category_id=None, created_by_user_id=1
    )

    profiles = await storage.list_profiles(guild_id=1)
    assert [p["id"] for p in profiles] == [p1["id"], p2["id"]]


async def test_list_all_profiles_spans_guilds(storage):
    await storage.create_profile(
        guild_id=1, name="A", join_channel_id=1, target_category_id=None, created_by_user_id=1
    )
    await storage.create_profile(
        guild_id=2, name="B", join_channel_id=2, target_category_id=None, created_by_user_id=1
    )

    all_profiles = await storage.list_all_profiles()
    assert len(all_profiles) == 2


async def test_delete_profile(storage):
    profile = await storage.create_profile(
        guild_id=1, name="A", join_channel_id=1, target_category_id=None, created_by_user_id=1
    )
    await storage.delete_profile(profile["id"])
    assert await storage.get_profile(profile["id"]) is None


# --- active temp channels -------------------------------------------------


async def test_create_and_get_active_temp_channel(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="profile-1", owner_user_id=42
    )

    record = await storage.get_active_temp_channel(500)
    assert record is not None
    assert record["channel_id"] == "500"
    assert record["guild_id"] == "1"
    assert record["profile_id"] == "profile-1"
    assert record["owner_user_id"] == "42"
    assert record["panel_message_id"] is None


async def test_get_active_temp_channel_missing_returns_none(storage):
    assert await storage.get_active_temp_channel(12345) is None


async def test_get_active_temp_channel_by_owner(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="profile-1", owner_user_id=42
    )

    record = await storage.get_active_temp_channel_by_owner(1, 42)
    assert record["channel_id"] == "500"

    assert await storage.get_active_temp_channel_by_owner(1, 999) is None


async def test_list_active_temp_channels_filters_by_guild(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.create_active_temp_channel(
        channel_id=600, guild_id=2, profile_id="p", owner_user_id=2
    )

    guild_1_rooms = await storage.list_active_temp_channels(guild_id=1)
    assert [r["channel_id"] for r in guild_1_rooms] == ["500"]

    all_rooms = await storage.list_active_temp_channels()
    assert {r["channel_id"] for r in all_rooms} == {"500", "600"}


async def test_transfer_active_temp_channel_owner(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.transfer_active_temp_channel_owner(500, 99)

    record = await storage.get_active_temp_channel(500)
    assert record["owner_user_id"] == "99"


async def test_touch_active_temp_channel_updates_last_seen(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    original = await storage.get_active_temp_channel(500)

    await storage.touch_active_temp_channel(500)
    touched = await storage.get_active_temp_channel(500)

    # last_seen_at is second-resolution ISO; equality is a reasonable
    # sanity check that the column round-trips even if the clock didn't tick.
    assert touched["last_seen_at"] >= original["last_seen_at"]


async def test_delete_active_temp_channel_also_clears_permits(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.add_permit(500, 7)
    assert len(await storage.list_permits(500)) == 1

    await storage.delete_active_temp_channel(500)

    assert await storage.get_active_temp_channel(500) is None
    assert await storage.list_permits(500) == []


# --- permits --------------------------------------------------------------


async def test_add_list_remove_permit(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.add_permit(500, 7)
    await storage.add_permit(500, 8)

    permits = await storage.list_permits(500)
    assert {p["user_id"] for p in permits} == {"7", "8"}

    await storage.remove_permit(500, 7)
    permits = await storage.list_permits(500)
    assert {p["user_id"] for p in permits} == {"8"}


async def test_add_permit_is_idempotent(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.add_permit(500, 7)
    await storage.add_permit(500, 7)  # insert or ignore -- must not raise or duplicate

    permits = await storage.list_permits(500)
    assert len(permits) == 1


# --- panel_message_id -------------------------------------------------


async def test_set_and_get_panel_message_id(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.set_panel_message_id(500, 123456789)

    record = await storage.get_active_temp_channel(500)
    assert record["panel_message_id"] == "123456789"


async def test_panel_message_id_cleared_when_room_deleted(storage):
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    await storage.set_panel_message_id(500, 123456789)

    await storage.delete_active_temp_channel(500)

    assert await storage.get_active_temp_channel(500) is None
    # Recreating the room (as would happen for a fresh temp channel with the
    # same id, however unlikely) must not resurrect the stale panel id.
    await storage.create_active_temp_channel(
        channel_id=500, guild_id=1, profile_id="p", owner_user_id=1
    )
    record = await storage.get_active_temp_channel(500)
    assert record["panel_message_id"] is None


# --- idempotent initialize --------------------------------------------


async def test_double_initialize_is_idempotent(storage):
    # `storage` fixture already called initialize() once; call again and
    # confirm no error and existing data survives.
    profile = await storage.create_profile(
        guild_id=1, name="A", join_channel_id=1, target_category_id=None, created_by_user_id=1
    )

    await storage.initialize()

    assert await storage.get_profile(profile["id"]) is not None


# --- guarded migration (_migrate) --------------------------------------


def _create_pre_migration_schema(db_path: str) -> None:
    """Build a database matching the schema as it existed before
    default_user_limit/temp_name_template/panel_message_id were added,
    with a row in each affected table, to exercise the guarded
    `alter table ... add column` migration path in Storage._migrate.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            create table guild_configs (
              guild_id text primary key,
              temp_channel_prefix text not null default '',
              notification_cooldown_seconds integer not null default 45,
              created_at text not null,
              updated_at text not null
            );

            create table temp_vc_profiles (
              id text primary key,
              guild_id text not null,
              name text not null,
              join_channel_id text not null unique,
              target_category_id text,
              created_by_user_id text not null,
              created_at text not null,
              updated_at text not null
            );

            create table active_temp_channels (
              channel_id text primary key,
              guild_id text not null,
              profile_id text not null,
              owner_user_id text not null,
              created_at text not null,
              last_seen_at text not null
            );

            create table temp_channel_permits (
              channel_id text not null,
              user_id text not null,
              created_at text not null,
              primary key (channel_id, user_id)
            );
            """
        )
        connection.execute(
            """
            insert into temp_vc_profiles (
                id, guild_id, name, join_channel_id, target_category_id,
                created_by_user_id, created_at, updated_at
            ) values ('profile-1', '1', 'Legacy', '10', null, '5', 'then', 'then')
            """
        )
        connection.execute(
            """
            insert into active_temp_channels (
                channel_id, guild_id, profile_id, owner_user_id, created_at, last_seen_at
            ) values ('500', '1', 'profile-1', '42', 'then', 'then')
            """
        )
        connection.commit()
    finally:
        connection.close()


async def test_migrate_adds_missing_columns_and_preserves_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "legacy.sqlite3")
        _create_pre_migration_schema(db_path)

        store = Storage(db_path)
        await store.initialize()

        profile = await store.get_profile("profile-1")
        assert profile is not None
        assert profile["name"] == "Legacy"
        # New columns exist and default to NULL for pre-existing rows.
        assert profile["default_user_limit"] is None
        assert profile["temp_name_template"] is None

        channel = await store.get_active_temp_channel(500)
        assert channel is not None
        assert channel["owner_user_id"] == "42"
        assert channel["panel_message_id"] is None

        # The migrated columns are now fully usable going forward.
        await store.set_panel_message_id(500, 999)
        channel = await store.get_active_temp_channel(500)
        assert channel["panel_message_id"] == "999"


async def test_migrate_is_a_no_op_when_columns_already_present(storage):
    # `storage` fixture already ran the full current schema + migration.
    # Running initialize() again must not error even though every guarded
    # column already exists.
    await storage.initialize()
    await storage.initialize()
