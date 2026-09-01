"""Tests for bot.py's on_voice_state_update handler.

This handler is the entry point for the entire temp-room lifecycle and was
previously untested, because bot.py called bot.run() at import time. It now
guards that behind `if __name__ == "__main__"`, so the module can be imported
and its event handler driven directly.

Discord fires VOICE_STATE_UPDATE for far more than joins and leaves: mute,
deafen, self-mute, camera and go-live changes all arrive as an event where
before.channel and after.channel are the same channel. Those must not be
read as a join or as a departure.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
from tests.conftest import make_guild, make_member, make_profile, make_voice_channel


def _state(channel=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(channel=channel)


@pytest.fixture
def wired(monkeypatch):
    """The module-level bot with clean runtime state and stubbed services."""
    guild = make_guild(1)
    lobby = make_voice_channel(100, guild, name="Join to Yap")
    room = make_voice_channel(200, guild, name="user7's Yap")
    member = make_member(7, guild)
    profile = make_profile(join_channel_id=lobby.id)

    monkeypatch.setattr(bot_module.bot, "profile_cache", {lobby.id: profile})
    monkeypatch.setattr(bot_module.bot, "active_temp_channel_ids", {room.id})
    create = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(bot_module.bot, "create_temp_room", create)
    monkeypatch.setattr(bot_module.bot, "cleanup_temp_channel", cleanup)

    return types.SimpleNamespace(
        guild=guild,
        lobby=lobby,
        room=room,
        member=member,
        profile=profile,
        create=create,
        cleanup=cleanup,
    )


async def _dispatch(before_channel, after_channel, member):
    await bot_module.bot.on_voice_state_update(
        member, _state(before_channel), _state(after_channel)
    )


# --- happy paths ------------------------------------------------------------


async def test_joining_a_lobby_creates_a_room(wired):
    await _dispatch(None, wired.lobby, wired.member)

    wired.create.assert_awaited_once_with(wired.member, wired.lobby, wired.profile)
    wired.cleanup.assert_not_called()


async def test_leaving_a_tracked_room_runs_cleanup(wired):
    await _dispatch(wired.room, None, wired.member)

    wired.cleanup.assert_awaited_once_with(wired.room, leaver=wired.member)
    wired.create.assert_not_called()


async def test_moving_from_a_tracked_room_into_a_lobby_does_both(wired):
    await _dispatch(wired.room, wired.lobby, wired.member)

    wired.cleanup.assert_awaited_once_with(wired.room, leaver=wired.member)
    wired.create.assert_awaited_once_with(wired.member, wired.lobby, wired.profile)


async def test_unrelated_channels_are_ignored(wired):
    other = make_voice_channel(900, wired.guild, name="General")

    await _dispatch(other, other, wired.member)
    await _dispatch(None, other, wired.member)

    wired.create.assert_not_called()
    wired.cleanup.assert_not_called()


# --- events that are not moves ---------------------------------------------


async def test_mute_inside_a_tracked_room_is_not_a_departure(wired):
    """A self-mute used to revoke a present member's access to the room.

    In a locked or hidden room the departure path strips the member's
    view/connect allow, so toggling mute silently removed their access to a
    room they were still sitting in.
    """
    await _dispatch(wired.room, wired.room, wired.member)

    wired.cleanup.assert_not_called()


async def test_mute_inside_a_lobby_does_not_retry_room_creation(wired):
    """The incident's amplifier.

    After a failed move the member is still parked in the lobby. Every
    subsequent mute/deafen/camera event arrived with after.channel == lobby,
    which re-ran room creation. Before PR #20 that produced one more orphan
    room per event -- the screenful in the incident screenshot.
    """
    await _dispatch(wired.lobby, wired.lobby, wired.member)

    wired.create.assert_not_called()


async def test_bots_are_ignored_entirely(wired):
    bot_member = make_member(999, wired.guild, is_bot=True)

    await _dispatch(wired.room, wired.lobby, bot_member)

    wired.create.assert_not_called()
    wired.cleanup.assert_not_called()


async def test_a_bot_driven_move_into_a_lobby_shaped_room_does_not_recurse(wired):
    """Invariant: bot-generated movement cannot cascade into more rooms.

    Modelled as the worst case -- a room whose id is also registered as a
    lobby. The member is a human, so the bot guard does not apply; the
    same-channel guard and the one-room-per-owner record must carry it.
    """
    bot_module.bot.profile_cache[wired.room.id] = make_profile(
        profile_id="profile-2", join_channel_id=wired.room.id
    )

    # The move the bot just performed: lobby -> room.
    await _dispatch(wired.lobby, wired.room, wired.member)
    # create_temp_room is reached exactly once and is itself responsible for
    # refusing (the owner already has a tracked room); it is not re-entered.
    assert wired.create.await_count == 1

    # The follow-up mute event inside that room must add nothing.
    await _dispatch(wired.room, wired.room, wired.member)
    assert wired.create.await_count == 1


# --- duplicate and concurrent events ----------------------------------------


async def test_duplicate_join_events_both_reach_creation_which_dedupes(wired):
    # The handler does not dedupe; create_temp_room's per-user lock and
    # ownership record do (see test_incident_regression). What matters here
    # is that the handler stays idempotent in shape: same call, same args.
    await _dispatch(None, wired.lobby, wired.member)
    await _dispatch(None, wired.lobby, wired.member)

    assert wired.create.await_count == 2
    assert all(
        call.args == (wired.member, wired.lobby, wired.profile)
        for call in wired.create.await_args_list
    )


async def test_join_then_immediate_leave_is_ordered_cleanup_after_create(wired):
    order: list[str] = []
    wired.create.side_effect = lambda *a, **k: order.append("create")
    wired.cleanup.side_effect = lambda *a, **k: order.append("cleanup")

    await _dispatch(None, wired.lobby, wired.member)
    await _dispatch(wired.room, None, wired.member)

    assert order == ["create", "cleanup"]


# --- failure containment -----------------------------------------------------


async def test_a_failing_cleanup_does_not_block_room_creation(wired, caplog):
    wired.cleanup.side_effect = RuntimeError("storage down")

    await _dispatch(wired.room, wired.lobby, wired.member)

    wired.create.assert_awaited_once()
    assert "voice_state cleanup_failed" in caplog.text


async def test_a_failing_creation_is_contained_and_logged(wired, caplog):
    wired.create.side_effect = RuntimeError("boom")

    await _dispatch(None, wired.lobby, wired.member)

    assert "voice_state create_failed" in caplog.text


async def test_one_members_failure_does_not_affect_another_member(wired):
    other = make_member(8, wired.guild)
    failed_for = []

    async def _create(member, lobby, profile):
        failed_for.append(member.id)
        if member.id == wired.member.id:
            raise RuntimeError("boom")

    wired.create.side_effect = _create

    await _dispatch(None, wired.lobby, wired.member)
    await _dispatch(None, wired.lobby, other)

    assert failed_for == [wired.member.id, other.id]


# --- module import safety ----------------------------------------------------


def test_importing_bot_does_not_start_the_client():
    """bot.run() must stay behind the __main__ guard.

    If it ever moves back to module scope, importing this module in CI would
    try to open a gateway connection with whatever token is in the
    environment.
    """
    source = (bot_module.__file__ or "").replace("\\", "/")
    assert source.endswith("bot.py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    run_line = next(line for line in text.splitlines() if "bot.run(" in line)
    assert run_line.startswith("    "), "bot.run must be inside main(), not at module scope"
    assert 'if __name__ == "__main__":' in text
