"""Tests for services/room_actions.py."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, Mock, patch

import pytest

from services.room_actions import (
    apply_claim,
    apply_hide,
    apply_kick,
    apply_lock,
    apply_transfer,
    apply_unhide,
    apply_unlock,
)
from tests.conftest import make_interaction, make_member, make_voice_channel


@pytest.fixture
def guild(guild_factory):
    return guild_factory(guild_id=1)


def _bot(**storage_overrides):
    defaults = dict(
        get_active_temp_channel=AsyncMock(return_value=None),
        get_active_temp_channel_by_owner=AsyncMock(return_value=None),
        transfer_active_temp_channel_owner=AsyncMock(),
        remove_permit=AsyncMock(),
        list_permits=AsyncMock(return_value=[]),
        list_blocks=AsyncMock(return_value=[]),
    )
    defaults.update(storage_overrides)
    storage = types.SimpleNamespace(**defaults)
    return types.SimpleNamespace(storage=storage)


# --- apply_transfer ---------------------------------------------------


async def test_apply_transfer_rejects_bot_target(guild):
    owner = make_member(1, guild)
    target = make_member(2, guild, is_bot=True)
    channel = make_voice_channel(500, guild, members=[owner, target])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    await apply_transfer(bot, interaction, channel, target)

    bot.storage.transfer_active_temp_channel_owner.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Yap rooms can only be transferred to server members.", ephemeral=True
    )


async def test_apply_transfer_rejects_target_not_in_channel(guild):
    owner = make_member(1, guild)
    target = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[owner])  # target absent
    interaction = make_interaction(owner, guild)
    bot = _bot()

    await apply_transfer(bot, interaction, channel, target)

    bot.storage.transfer_active_temp_channel_owner.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Transfer target must be in your Yap room.", ephemeral=True
    )


async def test_apply_transfer_rejects_target_who_owns_another_room(guild):
    owner = make_member(1, guild)
    target = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[owner, target])
    interaction = make_interaction(owner, guild)
    bot = _bot(
        get_active_temp_channel_by_owner=AsyncMock(
            return_value={"channel_id": "999"}  # a different channel than 500
        )
    )

    await apply_transfer(bot, interaction, channel, target)

    bot.storage.transfer_active_temp_channel_owner.assert_not_called()
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "already owns another active Yap room" in args[0]


async def test_apply_transfer_success_calls_refresh_after_response_exactly_once(guild):
    owner = make_member(1, guild)
    target = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[owner, target])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    call_order: list[str] = []
    interaction.response.send_message.side_effect = lambda *a, **k: call_order.append(
        "send_message"
    )

    async def _refresh(*args, **kwargs):
        call_order.append("refresh_panel_message")

    with patch("services.panel.refresh_panel_message", new=AsyncMock(side_effect=_refresh)) as refresh_mock:
        await apply_transfer(bot, interaction, channel, target)

    bot.storage.transfer_active_temp_channel_owner.assert_awaited_once_with(500, target.id)
    refresh_mock.assert_awaited_once_with(bot, channel)
    assert call_order == ["send_message", "refresh_panel_message"]


async def test_apply_transfer_admin_override_logs_with_pre_transfer_owner(guild):
    # An admin (not the room's owner) transferring the room must be logged
    # as an override, attributed to the owner *before* the transfer -- a
    # post-mutation re-fetch would see the new owner and misfire on an
    # ordinary owner-initiated transfer instead (this is what regressed
    # when log_admin_action was first added).
    admin = make_member(9, guild, manage_channels=True)
    target = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[admin, target])
    interaction = make_interaction(admin, guild)
    pre_record = {"owner_user_id": "1"}
    bot = _bot(get_active_temp_channel=AsyncMock(return_value=pre_record))

    with patch("services.panel.refresh_panel_message", new=AsyncMock()), patch(
        "services.room_actions.log_admin_action", new=AsyncMock()
    ) as log_mock:
        await apply_transfer(bot, interaction, channel, target)

    log_mock.assert_awaited_once_with(bot, interaction, channel, record=pre_record)


# --- apply_claim --------------------------------------------------------


def _record(owner_id: int) -> dict:
    return {"owner_user_id": str(owner_id)}


async def test_apply_claim_untracked_channel_denied(guild):
    claimant = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[claimant])
    interaction = make_interaction(claimant, guild)
    bot = _bot(get_active_temp_channel=AsyncMock(return_value=None))

    await apply_claim(bot, interaction, channel)

    interaction.response.send_message.assert_awaited_once_with(
        "That voice channel is not a tracked YapHub temp room.", ephemeral=True
    )


async def test_apply_claim_requires_claimant_present(guild):
    claimant = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[])  # claimant not present
    interaction = make_interaction(claimant, guild)
    bot = _bot(get_active_temp_channel=AsyncMock(return_value=_record(1)))

    await apply_claim(bot, interaction, channel)

    interaction.response.send_message.assert_awaited_once_with(
        "Join the room before claiming it.", ephemeral=True
    )


async def test_apply_claim_denied_when_owner_still_present(guild):
    owner = make_member(1, guild)
    claimant = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[owner, claimant])
    interaction = make_interaction(claimant, guild)
    bot = _bot(get_active_temp_channel=AsyncMock(return_value=_record(owner.id)))

    await apply_claim(bot, interaction, channel)

    interaction.response.send_message.assert_awaited_once_with(
        "The current owner is still in the room.", ephemeral=True
    )


async def test_apply_claim_denied_when_claimant_owns_another_room(guild):
    claimant = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[claimant])  # old owner has left
    interaction = make_interaction(claimant, guild)
    bot = _bot(
        get_active_temp_channel=AsyncMock(return_value=_record(1)),
        get_active_temp_channel_by_owner=AsyncMock(return_value={"channel_id": "999"}),
    )

    await apply_claim(bot, interaction, channel)

    interaction.response.send_message.assert_awaited_once()
    args, _ = interaction.response.send_message.call_args
    assert "already own another active Yap room" in args[0]
    bot.storage.transfer_active_temp_channel_owner.assert_not_called()


async def test_apply_claim_success_calls_refresh_after_response_exactly_once(guild):
    claimant = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[claimant])  # old owner has left
    interaction = make_interaction(claimant, guild)
    bot = _bot(get_active_temp_channel=AsyncMock(return_value=_record(1)))

    call_order: list[str] = []
    interaction.response.send_message.side_effect = lambda *a, **k: call_order.append(
        "send_message"
    )

    async def _refresh(*args, **kwargs):
        call_order.append("refresh_panel_message")

    with patch("services.panel.refresh_panel_message", new=AsyncMock(side_effect=_refresh)) as refresh_mock:
        await apply_claim(bot, interaction, channel)

    bot.storage.transfer_active_temp_channel_owner.assert_awaited_once_with(500, claimant.id)
    refresh_mock.assert_awaited_once_with(bot, channel)
    assert call_order == ["send_message", "refresh_panel_message"]


# --- apply_kick -----------------------------------------------------------


async def test_apply_kick_rejects_self_kick(guild):
    owner = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[owner])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    await apply_kick(bot, interaction, channel, owner)

    owner.move_to.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "You can't remove yourself from your own room.", ephemeral=True
    )


async def test_apply_kick_rejects_member_not_in_channel(guild):
    owner = make_member(1, guild)
    target = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[owner])  # target absent
    interaction = make_interaction(owner, guild)
    bot = _bot()

    await apply_kick(bot, interaction, channel, target)

    target.move_to.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        f"{target.mention} is not in your Yap room.", ephemeral=True
    )


async def test_apply_kick_removes_permit_and_revokes_overwrites(guild):
    owner = make_member(1, guild)
    target = make_member(2, guild)
    channel = make_voice_channel(500, guild, members=[owner, target])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    with patch(
        "services.room_actions.revoke_member_overwrites", new=AsyncMock()
    ) as revoke_mock:
        await apply_kick(bot, interaction, channel, target)

    target.move_to.assert_awaited_once()
    bot.storage.remove_permit.assert_awaited_once_with(500, target.id)
    revoke_mock.assert_awaited_once()
    interaction.response.send_message.assert_awaited_once_with(
        f"Removed {target.mention} from your Yap room.", ephemeral=True
    )


async def test_apply_kick_handles_move_failure_gracefully(guild):
    import discord

    owner = make_member(1, guild)
    target = make_member(2, guild)
    target.move_to = AsyncMock(side_effect=discord.HTTPException(
        types.SimpleNamespace(status=500, reason="Server Error"), "boom"
    ))
    channel = make_voice_channel(500, guild, members=[owner, target])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    with patch("services.room_actions.revoke_member_overwrites", new=AsyncMock()) as revoke_mock:
        await apply_kick(bot, interaction, channel, target)

    bot.storage.remove_permit.assert_not_called()
    revoke_mock.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "I couldn't remove that member. Check my Move Members permission.", ephemeral=True
    )


# --- lock/unlock/hide/unhide: the 3-second interaction deadline -----------
#
# Each of these issues one set_permissions call per target (@everyone, every
# role the category granted, the bot, the owner, every permit, every member in
# the room). Discord gives 3 seconds to acknowledge an interaction, so the
# reply cannot wait until after that work -- it has to defer first, or the
# user sees "YapHub didn't respond in time" on an action that is applying.

STATE_ACTIONS = pytest.mark.parametrize(
    "action, patched, message",
    [
        (apply_lock, "lock_temp_channel", "Locked your Yap room."),
        (apply_unlock, "unlock_temp_channel", "Unlocked your Yap room."),
        (apply_hide, "hide_temp_channel", "Hid your Yap room. Only current members can see it."),
        (apply_unhide, "unhide_temp_channel", "Your Yap room is visible again."),
    ],
)


@STATE_ACTIONS
async def test_state_action_defers_before_touching_discord(guild, action, patched, message):
    owner = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[owner])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    call_order: list[str] = []
    interaction.response.defer.side_effect = lambda *a, **k: call_order.append("defer")

    async def _permission_call(*args, **kwargs):
        call_order.append("permissions")

    with patch(
        f"services.room_actions.{patched}", new=AsyncMock(side_effect=_permission_call)
    ), patch("services.panel.refresh_panel_message", new=AsyncMock()):
        await action(bot, interaction, channel)

    assert call_order == ["defer", "permissions"]


@STATE_ACTIONS
async def test_state_action_replies_via_followup_after_deferring(guild, action, patched, message):
    owner = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[owner])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    with patch(f"services.room_actions.{patched}", new=AsyncMock()), patch(
        "services.panel.refresh_panel_message", new=AsyncMock()
    ):
        await action(bot, interaction, channel)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_awaited_once_with(message, ephemeral=True)


# --- permit/block lists are carried into the permission maths -------------


async def test_apply_hide_passes_permits_as_allowed_and_blocks_as_denied(guild):
    owner = make_member(1, guild)
    permitted = make_member(2, guild)
    blocked = make_member(3, guild)
    guild.get_member = Mock(
        side_effect=lambda user_id: {1: owner, 2: permitted, 3: blocked}.get(int(user_id))
    )
    channel = make_voice_channel(500, guild, members=[owner])
    interaction = make_interaction(owner, guild)
    bot = _bot(
        get_active_temp_channel=AsyncMock(return_value={"owner_user_id": "1"}),
        list_permits=AsyncMock(return_value=[{"user_id": "2"}]),
        list_blocks=AsyncMock(return_value=[{"user_id": "3"}]),
    )

    with patch("services.room_actions.hide_temp_channel", new=AsyncMock()) as hide_mock, patch(
        "services.panel.refresh_panel_message", new=AsyncMock()
    ):
        await apply_hide(bot, interaction, channel)

    _, kwargs = hide_mock.call_args
    assert kwargs["owner"] is owner
    assert kwargs["extra_allowed"] == (permitted,)
    assert kwargs["denied"] == (blocked,)


async def test_apply_unhide_preserves_permitted_members(guild):
    owner = make_member(1, guild)
    permitted = make_member(2, guild)
    guild.get_member = Mock(side_effect=lambda user_id: {1: owner, 2: permitted}.get(int(user_id)))
    channel = make_voice_channel(500, guild, members=[owner])
    interaction = make_interaction(owner, guild)
    bot = _bot(list_permits=AsyncMock(return_value=[{"user_id": "2"}]))

    with patch(
        "services.room_actions.unhide_temp_channel", new=AsyncMock()
    ) as unhide_mock, patch("services.panel.refresh_panel_message", new=AsyncMock()):
        await apply_unhide(bot, interaction, channel)

    _, kwargs = unhide_mock.call_args
    assert kwargs["preserve"] == (permitted,)


# --- the panel is repainted with the state that was just applied ----------


@pytest.mark.parametrize(
    "action, patched, expected",
    [
        (apply_hide, "hide_temp_channel", {"hidden": True}),
        (apply_unhide, "unhide_temp_channel", {"hidden": False}),
        (apply_lock, "lock_temp_channel", {"locked": True}),
        (apply_unlock, "unlock_temp_channel", {"locked": False}),
    ],
)
async def test_state_action_repaints_panel_with_the_applied_state(
    guild, action, patched, expected
):
    # Re-deriving from channel.overwrites here would read discord.py's cache,
    # which set_permissions does not update -- the panel would repaint with
    # the pre-action state and the room would look like the action failed.
    owner = make_member(1, guild)
    channel = make_voice_channel(500, guild, members=[owner])
    interaction = make_interaction(owner, guild)
    bot = _bot()

    with patch(f"services.room_actions.{patched}", new=AsyncMock()), patch(
        "services.panel.refresh_panel_message", new=AsyncMock()
    ) as refresh_mock:
        await action(bot, interaction, channel)

    refresh_mock.assert_awaited_once_with(bot, channel, **expected)
