"""Load and concurrency stress tests for the temp-room lifecycle.

These do not test new *behavior* -- every failure mode here (duplicate
blocking, guild scoping, the mid-creation reap hazard) is already proven at
small scale in test_incident_regression.py and test_temp_room_lifecycle.py.
What this file adds is scale: the same invariants re-proven for hundreds of
guilds and members, all firing through asyncio.gather at once, against the
real per-(guild, member) lock and the real SQLite storage layer -- only
Discord's network I/O is faked.

Run this whenever a change touches locking, storage, or the
create/cleanup/reconcile paths, and read the assertions as the contract:
if a change makes any of these fail, that change made YapHub less safe
under load, whatever else it does. It runs as part of the normal `pytest`
invocation (see pytest.ini's testpaths), so it already executes on every
push via CI -- no separate step to remember.

Timing assertions here are generous canaries for gross regressions (e.g. a
lock or connection-handling change that accidentally serializes
everything), not a performance benchmark: there is no real Discord rate
limit or network variance in play, so a healthy run should finish well
inside the bound.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from services.temp_channels import (
    cleanup_temp_channel,
    create_temp_room,
    reconcile_active_temp_channels,
)
from storage import Storage
from tests.conftest import FakeDiscord, make_bot, make_profile

GUILD_COUNT = 15
MEMBERS_PER_GUILD = 8
DUPLICATE_ATTEMPTS_PER_MEMBER = 3
TOTAL_MEMBERS = GUILD_COUNT * MEMBERS_PER_GUILD


class MultiGuildWorld:
    """Wires many independent FakeDiscord guilds behind one bot.

    Each guild gets its own numeric id range for its lobby and rooms (guild
    N owns ids N*100_000..N*100_000+99_999) so that a bug which resolved a
    channel id against the wrong guild would produce a collision loud enough
    to fail these tests, rather than silently aliasing one guild's room onto
    another's.
    """

    def __init__(self, guild_count: int, first_guild_id: int = 1) -> None:
        self.worlds: dict[int, FakeDiscord] = {}
        self.lobbies: dict[int, object] = {}
        for offset in range(guild_count):
            guild_id = first_guild_id + offset
            world = FakeDiscord(guild_id=guild_id, first_channel_id=guild_id * 100_000 + 200)
            lobby = world.add_channel(guild_id * 100_000 + 100, name="Join to Yap")
            self.worlds[guild_id] = world
            self.lobbies[guild_id] = lobby

    def get_guild(self, guild_id: int):
        world = self.worlds.get(guild_id)
        return world.guild if world else None

    def managed_room_ids(self, guild_id: int) -> set[int]:
        world = self.worlds[guild_id]
        lobby_id = self.lobbies[guild_id].id
        return world.live_channel_ids - {lobby_id}

    def profile_for(self, guild_id: int):
        return make_profile(guild_id=guild_id, join_channel_id=self.lobbies[guild_id].id)


@pytest.fixture
async def world(tmp_path):
    multi = MultiGuildWorld(GUILD_COUNT)
    storage = Storage(str(tmp_path / "yaphub.sqlite3"))
    await storage.initialize()
    bot = make_bot(storage)
    bot.get_guild = multi.get_guild
    return multi, bot


@pytest.fixture(autouse=True)
def _stub_side_channels():
    # A single patch spanning the whole test, entered before any concurrent
    # task starts -- unittest.mock.patch is not itself safe to enter/exit
    # from many overlapping coroutines on the same target.
    with (
        patch("services.temp_channels.send_room_panel", new=AsyncMock(return_value=None)),
        patch("services.temp_channels.notify_duplicate_room", new=AsyncMock()),
    ):
        yield


def _assert_exactly_one_room_per_member(multi: MultiGuildWorld, rows) -> None:
    assert len(rows) == TOTAL_MEMBERS
    for guild_id in multi.worlds:
        assert len(multi.managed_room_ids(guild_id)) == MEMBERS_PER_GUILD
        guild_rows = [row for row in rows if int(row["guild_id"]) == guild_id]
        assert len(guild_rows) == MEMBERS_PER_GUILD
        assert len({row["owner_user_id"] for row in guild_rows}) == MEMBERS_PER_GUILD


async def test_hundreds_of_concurrent_creates_across_many_guilds_yield_exactly_one_room_each(world):
    """The baseline load case: no failures, no duplicates -- just scale.

    ONE LOGICAL ROOM == AT MOST ONE ACTIVE MANAGED DISCORD ROOM must hold
    for every one of TOTAL_MEMBERS members, all created at once, across
    GUILD_COUNT guilds with overlapping member ids (guild N's member 7 and
    guild M's member 7 are different people; this is also therefore a
    guild-isolation check at scale).
    """
    multi, bot = world
    tasks = []
    for guild_id, fake_discord in multi.worlds.items():
        profile = multi.profile_for(guild_id)
        for member_index in range(MEMBERS_PER_GUILD):
            member = fake_discord.make_member(1000 + member_index)
            tasks.append(create_temp_room(bot, member, multi.lobbies[guild_id], profile))

    started = time.perf_counter()
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - started

    rows = await bot.storage.list_active_temp_channels()
    _assert_exactly_one_room_per_member(multi, rows)
    assert bot.user_creation_locks == {}, "every lock must be evicted once its holders finish"
    assert elapsed < 20, (
        f"{TOTAL_MEMBERS} concurrent in-memory creates took {elapsed:.1f}s -- "
        "investigate for accidental new serialization (lock scope, a new "
        "blocking call on the event loop, a connection-per-call regression)"
    )


async def test_duplicate_join_storm_across_every_guild_blocks_every_extra_attempt(world):
    """Each member spams the lobby DUPLICATE_ATTEMPTS_PER_MEMBER times at
    once, everywhere, simultaneously. Exactly one room must survive per
    member no matter how many concurrent attempts raced for it -- this is
    the core incident invariant (repeated attempts cannot amplify) proven
    under real concurrency pressure rather than two sequential calls.
    """
    multi, bot = world
    tasks = []
    for guild_id, fake_discord in multi.worlds.items():
        profile = multi.profile_for(guild_id)
        for member_index in range(MEMBERS_PER_GUILD):
            member = fake_discord.make_member(2000 + member_index)
            for _ in range(DUPLICATE_ATTEMPTS_PER_MEMBER):
                tasks.append(create_temp_room(bot, member, multi.lobbies[guild_id], profile))

    await asyncio.gather(*tasks)

    rows = await bot.storage.list_active_temp_channels()
    _assert_exactly_one_room_per_member(multi, rows)
    assert bot.user_creation_locks == {}


async def test_reconcile_running_concurrently_with_load_does_not_reap_live_rooms(world):
    """The mid-creation-reap hazard, at scale.

    A reconcile pass reading a half-written creation (record exists,
    member not yet moved in) must not delete it. Firing several reconcile
    passes at the exact moment hundreds of creates are landing is the
    highest-pressure version of that race this harness can produce without
    a real Discord gateway.
    """
    multi, bot = world
    create_tasks = []
    for guild_id, fake_discord in multi.worlds.items():
        profile = multi.profile_for(guild_id)
        for member_index in range(MEMBERS_PER_GUILD):
            member = fake_discord.make_member(3000 + member_index)
            create_tasks.append(create_temp_room(bot, member, multi.lobbies[guild_id], profile))

    reconcile_tasks = [reconcile_active_temp_channels(bot) for _ in range(5)]

    await asyncio.gather(*create_tasks, *reconcile_tasks)

    rows = await bot.storage.list_active_temp_channels()
    _assert_exactly_one_room_per_member(multi, rows)


async def test_create_and_full_teardown_leaves_no_orphans_at_scale(world):
    """Occupy every room, then empty every room concurrently -- interleaved
    with reconcile passes -- and demand storage and Discord agree on zero
    afterward. This is the scale version of the invariant that matters most
    in production: nothing is ever left behind."""
    multi, bot = world
    for guild_id, fake_discord in multi.worlds.items():
        profile = multi.profile_for(guild_id)
        for member_index in range(MEMBERS_PER_GUILD):
            member = fake_discord.make_member(4000 + member_index)
            await create_temp_room(bot, member, multi.lobbies[guild_id], profile)

    leave_tasks = []
    for guild_id, fake_discord in multi.worlds.items():
        for channel_id in list(multi.managed_room_ids(guild_id)):
            channel = fake_discord.channels[channel_id]
            member = channel.members[0]
            fake_discord.move(member, None)
            leave_tasks.append(cleanup_temp_channel(bot, channel, leaver=member))

    await asyncio.gather(*leave_tasks, reconcile_active_temp_channels(bot))

    assert await bot.storage.list_active_temp_channels() == []
    for guild_id in multi.worlds:
        assert multi.managed_room_ids(guild_id) == set()
