import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api_recovery import AmbiguousMutation, ApiDeferred, ApiRecovery
from services.lifecycle_recovery import LifecycleRecovery
from storage import Storage
from tests.conftest import FakeDiscord, make_bot, make_permissions
from tests.test_api_recovery import client_for, route
from tests.test_api_recovery import server as server


@pytest.fixture
async def recovery(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "recovery.sqlite3"))
    await storage.initialize()
    world = FakeDiscord(1)
    world.guild.unavailable = False
    world.guild.me.guild_permissions = make_permissions(view_audit_log=False)
    lobby = world.add_channel(100)
    member = world.make_member(7)
    members = {member.id: member}
    world.guild.get_member.side_effect = members.get
    world.move(member, lobby)
    profile = await storage.create_profile(1, "SAL", lobby.id, None, 7)
    bot = make_bot(storage, is_ready=lambda: True, get_guild=lambda _: world.guild)
    queue = LifecycleRecovery(bot)
    bot.lifecycle_recovery = queue
    panel = AsyncMock(return_value=SimpleNamespace(id=501))
    monkeypatch.setattr("services.lifecycle_recovery.send_room_panel", panel)
    monkeypatch.setattr("services.lifecycle_recovery.notify_duplicate_room", AsyncMock())
    await queue.initialize()
    await queue.enqueue(member, lobby, profile)
    return SimpleNamespace(queue=queue, bot=bot, store=queue.store, storage=storage,
                           world=world, lobby=lobby, member=member, profile=profile, panel=panel, members=members)


async def ready_rows(store):
    await store.transaction(lambda c: c.execute("update pending_joins set due=0"))


async def test_waiting_join_recovers_once_and_counts_completion_once(recovery):
    r = recovery
    r.world.guild.create_voice_channel.side_effect = ApiDeferred(600)
    await r.queue.tick()
    assert not r.world.created_ids
    assert len(await r.store.rows("pending_joins")) == 1
    r.world.guild.create_voice_channel.side_effect = r.world._create_voice_channel
    await ready_rows(r.store)
    await r.queue.tick()
    await r.queue.tick()
    assert r.world.created_ids == [200]
    assert r.member.voice.channel.id == 200
    assert not await r.store.rows("pending_joins")
    intent = (await r.store.rows("creation_intents"))[0]
    assert intent["phase"] == "completed"
    assert not await r.store.complete(intent, None, None)
    count = await r.store.transaction(lambda c: c.execute("select sum(count) from telemetry_daily_counts").fetchone()[0])
    assert count == 1
    assert r.world.guild.create_voice_channel.call_args.kwargs["reason"] == f"YapHub intent {intent['id']}"


async def test_departed_user_is_not_moved_back_after_create(recovery):
    r = recovery
    async def leave_during_create(**kwargs):
        r.world.move(r.member, None)
        return await r.world._create_voice_channel(**kwargs)
    r.world.guild.create_voice_channel.side_effect = leave_during_create
    await r.queue.tick()
    r.member.move_to.assert_not_awaited()
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "cancelled"
    assert await r.storage.get_active_temp_channel(200) is not None
    assert len(await r.store.rows("pending_cleanup")) == 1


async def test_deferred_move_survives_cleanup_and_restart(recovery):
    r = recovery
    r.world.move_error = ApiDeferred(600)
    await r.queue.tick()
    await r.store.cleanup(1, 200)
    await r.queue.tick(True)
    assert not r.world.deleted_ids
    replacement = LifecycleRecovery(r.bot)
    await replacement.initialize()
    r.world.move_error = None
    await ready_rows(r.store)
    await replacement.tick()
    assert r.world.created_ids == [200]
    assert r.member.voice.channel.id == 200
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "completed"


async def test_lost_create_response_blocks_replay_and_exact_staff_attribution_resumes(recovery):
    r = recovery
    async def lost_reply(**kwargs):
        await r.world._create_voice_channel(**kwargs)
        raise AmbiguousMutation("lost")
    r.world.guild.create_voice_channel.side_effect = lost_reply
    await r.queue.tick()
    intent = (await r.store.rows("creation_intents"))[0]
    assert intent["phase"] == "unresolved"
    await ready_rows(r.store)
    await r.queue.tick()
    assert r.world.created_ids == [200]
    assert not await r.storage.list_active_temp_channels()
    await r.store.attach(intent, 200)
    await ready_rows(r.store)
    await r.queue.tick()
    assert r.member.voice.channel.id == 200
    assert r.world.created_ids == [200]


async def test_storage_failure_after_create_preserves_unresolved_intent(recovery, monkeypatch):
    r = recovery
    monkeypatch.setattr(r.store, "attach", AsyncMock(side_effect=OSError("disk unavailable")))
    await r.queue.tick()
    assert r.world.created_ids == [200]
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "unresolved"
    assert not r.world.deleted_ids
    await ready_rows(r.store)
    await r.queue.tick()
    assert r.world.created_ids == [200]


async def test_restart_transmission_intent_and_cancellation_are_not_replayed(recovery):
    r = recovery
    entered = asyncio.Event()
    async def hang(**kwargs):
        await r.world._create_voice_channel(**kwargs)
        entered.set()
        await asyncio.Future()
    r.world.guild.create_voice_channel.side_effect = hang
    task = asyncio.create_task(r.queue.tick())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "unresolved"
    await r.store.transaction(lambda c: c.execute("update creation_intents set phase='sent'"))
    await r.store.restore()
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "unresolved"
    await r.queue.tick()
    assert r.world.created_ids == [200]


async def test_lobby_switch_replaces_stale_target_before_create(recovery):
    r = recovery
    second = r.world.add_channel(101)
    profile = await r.storage.create_profile(1, "YSL", 101, None, 7)
    r.world.move(r.member, second)
    await r.queue.enqueue(r.member, second, profile)
    await r.queue.tick()
    assert r.world.created_ids == [200]
    row = await r.storage.get_active_temp_channel(200)
    assert row["profile_id"] == profile["id"]


async def test_profile_removed_or_permissions_changed_during_pause(recovery):
    r = recovery
    await r.storage.delete_profile(r.profile["id"])
    await r.queue.tick()
    assert not r.world.created_ids
    assert not await r.store.rows("pending_joins")


async def test_unavailable_gateway_or_member_cache_preserves_work(recovery):
    r = recovery
    r.world.guild.unavailable = True
    await r.queue.tick()
    assert len(await r.store.rows("pending_joins")) == 1
    r.world.guild.unavailable = False
    r.members.clear()
    await ready_rows(r.store)
    await r.queue.tick()
    assert len(await r.store.rows("pending_joins")) == 1
    assert not r.world.created_ids


async def test_queue_caps_do_not_create_rooms_and_do_not_expire_old_waits(recovery):
    r = recovery
    for user in range(8, 57):
        assert await r.store.enqueue(1, user, 100, r.profile["id"])
    assert not await r.store.enqueue(1, 57, 100, r.profile["id"])
    for guild in range(2, 11):
        for user in range(50):
            assert await r.store.enqueue(guild, user, 100, r.profile["id"])
    assert not await r.store.enqueue(11, 1, 100, r.profile["id"])
    assert len(await r.store.rows("pending_joins")) == 500
    assert not r.world.created_ids


async def test_panel_backfill_retries_without_creating_another_room(recovery):
    r = recovery
    r.panel.return_value = None
    await r.queue.tick()
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "moved"
    r.panel.return_value = SimpleNamespace(id=501)
    await ready_rows(r.store)
    await r.queue.tick()
    assert r.world.created_ids == [200]
    assert r.member.move_to.await_count == 1


async def test_scheduler_rotates_guilds_and_claims_one_operation_each(recovery):
    q = recovery.queue
    rows = [{"guild_id": str(g), "due": 0} for g in (1, 1, 2, 2)]
    first = q.choose(rows)
    second = q.choose(rows)
    assert {first["guild_id"], second["guild_id"]} == {"1", "2"}
    assert q.choose(rows) is None
    q.busy.clear()
    assert q.choose(rows)["guild_id"] != second["guild_id"]


async def test_permissions_changed_during_pause_prevent_create(recovery):
    r = recovery
    r.world.guild.me.guild_permissions = make_permissions(manage_channels=False)
    await r.queue.tick()
    assert not r.world.created_ids
    assert not await r.store.rows("pending_joins")


async def test_manual_deletion_during_deferred_move_allows_one_replacement(recovery):
    r = recovery
    r.world.move_error = ApiDeferred(600)
    await r.queue.tick()
    r.world.channels.pop(200)
    # These are the local state changes made on a verified deletion event.
    await r.storage.delete_active_temp_channel(200)
    await r.store.deleted(200)
    r.bot.active_temp_channel_ids.discard(200)
    r.world.move_error = None
    await ready_rows(r.store)
    await r.queue.tick()
    assert r.world.created_ids == [200, 201]
    assert r.member.voice.channel.id == 201
    assert len(await r.storage.list_active_temp_channels()) == 1


async def test_cleanup_rejection_preserves_tracking_and_reschedules(recovery):
    r = recovery
    await r.queue.tick()
    room = r.member.voice.channel
    r.world.move(r.member, None)
    r.world.delete_error = ApiDeferred(600)
    await r.store.cleanup(1, room.id, r.member.id)
    await r.queue.tick(True)
    assert await r.storage.get_active_temp_channel(room.id) is not None
    assert not r.world.deleted_ids
    assert (await r.store.rows("pending_cleanup"))[0]["due"] > __import__("time").time() + 599


async def test_complete_flow_cleans_room_and_durable_ownership(recovery):
    r = recovery
    await r.queue.tick()
    room = r.member.voice.channel
    r.world.move(r.member, None)
    await r.store.cleanup(1, room.id, r.member.id)
    await r.queue.tick(True)
    assert r.world.created_ids == r.world.deleted_ids == [200]
    assert not await r.storage.list_active_temp_channels()
    assert not await r.store.rows("pending_cleanup")
    assert not r.bot.active_temp_channel_ids


async def test_real_http_rejection_defers_join_and_cleanup_without_more_attempts(recovery, server):
    r = recovery
    url, attempts, replies = server
    api = ApiRecovery(r.storage)
    await api.initialize()
    client = await client_for(api)
    replies.append((429, {"Retry-After": "600"}, {"message": "blocked"}))
    async def create(**kwargs):
        await client.request(route(url, "POST", "/guilds/{guild_id}/channels", guild_id=1), json={"name": "room"})
        return await r.world._create_voice_channel(**kwargs)
    r.world.guild.create_voice_channel.side_effect = create
    try:
        await r.queue.tick()
        assert len(attempts) == 1
        assert not r.world.created_ids
        room = r.world.add_channel(222)
        async def delete(**kwargs):
            await client.request(route(url, "DELETE", "/channels/{channel_id}", channel_id=222))
        room.delete.side_effect = delete
        await r.storage.create_active_temp_channel(222, 1, r.profile["id"], 99)
        r.bot.active_temp_channel_ids.add(222)
        await r.store.cleanup(1, 222, r.member.id)
        await r.queue.tick(True)
        await ready_rows(r.store)
        await r.queue.tick()
        assert len(attempts) == 1
        assert await r.storage.get_active_temp_channel(222) is not None
        restored = ApiRecovery(r.storage)
        await restored.initialize()
        assert restored.retry_at() == api.retry_at()
        assert len(await r.store.rows("pending_joins")) == 1
    finally:
        await client.close()


async def test_real_http_create_5xx_cannot_replay_after_restart(recovery, server):
    r = recovery
    url, attempts, replies = server
    api = ApiRecovery(r.storage)
    await api.initialize()
    client = await client_for(api)
    replies.append((503, {}, {"message": "response lost after mutation"}))
    async def create(**kwargs):
        # Simulate server-side creation despite a lost/5xx response.
        await r.world._create_voice_channel(**kwargs)
        await client.request(route(url, "POST", "/guilds/{guild_id}/channels", guild_id=1), json={"name": "room"})
    r.world.guild.create_voice_channel.side_effect = create
    try:
        await r.queue.tick()
        replacement = LifecycleRecovery(r.bot)
        await replacement.initialize()
        await ready_rows(r.store)
        await replacement.tick()
        assert len(attempts) == 1
        assert r.world.created_ids == [200]
        assert (await r.store.rows("creation_intents"))[0]["phase"] == "unresolved"
    finally:
        await client.close()


async def test_explicit_member_removal_cancels_protection_and_retains_cleanup(recovery):
    r = recovery
    r.world.move_error = ApiDeferred(600)
    await r.queue.tick()
    await r.queue.cancel_member_removal(r.member)
    assert not await r.store.rows("pending_joins")
    assert not await r.store.protects(200)
    assert await r.storage.get_active_temp_channel(200) is not None
    assert len(await r.store.rows("pending_cleanup")) == 1


async def test_interrupted_sent_state_cannot_replay_without_a_restart(recovery):
    r = recovery
    row = (await r.store.rows("pending_joins"))[0]
    intent = await r.store.intent(row)
    await r.store.phase(intent["id"], "sent")
    await r.queue.tick()
    assert not r.world.created_ids
    assert (await r.store.rows("creation_intents"))[0]["phase"] == "unresolved"


async def test_slow_sal_join_does_not_block_ysl_join_and_cleanup(recovery):
    r = recovery
    ysl = FakeDiscord(2, first_channel_id=500)
    ysl.guild.unavailable = False
    lobby = ysl.add_channel(300)
    member = ysl.make_member(8)
    ysl.guild.get_member.side_effect = lambda identifier: member if identifier == 8 else None
    ysl.move(member, lobby)
    profile = await r.storage.create_profile(2, "YSL", 300, None, 8)
    r.bot.get_guild = lambda identifier: r.world.guild if identifier == 1 else ysl.guild
    await r.queue.enqueue(member, lobby, profile)
    old = ysl.add_channel(599)
    await r.storage.create_active_temp_channel(599, 2, profile["id"], 88)
    r.bot.active_temp_channel_ids.add(599)
    await r.store.cleanup(2, 599, member.id)
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_sal(**kwargs):
        entered.set()
        await release.wait()
        return await r.world._create_voice_channel(**kwargs)
    r.world.guild.create_voice_channel.side_effect = slow_sal
    task = asyncio.create_task(r.queue.tick())
    try:
        await entered.wait()
        await r.queue.tick()
        assert member.voice.channel.id == 500
        await r.queue.tick(True)
        assert old.id in ysl.deleted_ids
        assert not r.world.created_ids
        release.set()
        await task
        assert r.world.created_ids == [200]
        assert not r.queue.busy
    finally:
        release.set()
        await task
