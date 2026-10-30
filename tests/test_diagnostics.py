import json
from types import SimpleNamespace

import discord
from unittest.mock import Mock

from services.diagnostics import log_error_snapshot, log_interaction_error
from tests.conftest import make_category, make_guild, make_permissions, make_voice_channel


def snapshots(caplog):
    return [json.loads(record.getMessage().removeprefix("diagnostic_snapshot "))
            for record in caplog.records if record.getMessage().startswith("diagnostic_snapshot {")]


def test_snapshot_distinguishes_guild_grants_from_category_denials(caplog):
    guild = make_guild(123, name="A server\nwith a newline")
    category = make_category(456, guild, permissions=make_permissions(manage_channels=False))
    lobby = make_voice_channel(789, guild, category=category)
    log_error_snapshot("configuration_invalid", guild=guild, lobby=lobby,
                       operation="create_room_preflight", missing=["Manage Channels"],
                       profile_id="profile-1", intent_id="intent-1", phase="prepared")
    snapshot, = snapshots(caplog)
    assert snapshot["guild_id"] == "123"
    assert snapshot["category_id"] == "456"
    assert snapshot["lobby_id"] == "789"
    assert snapshot["missing_permissions"] == ["Manage Channels"]
    assert snapshot["permissions"]["guild"]["manage_channels"] is True
    assert snapshot["permissions"]["category"]["manage_channels"] is False
    assert snapshot["phase"] == "prepared"
    assert snapshot["intent_id"] == "intent-1"
    assert snapshot["profile_id"] == "profile-1"
    assert "\n" not in caplog.records[-1].getMessage()
    assert len(snapshot["error_id"]) == 32
    guild.fetch_channel.assert_not_called()


def test_discord_exception_details_exclude_payload_and_secret_text(caplog):
    error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                              {"code": 50001, "message": "SECRET_TOKEN and private message"})
    log_error_snapshot("lobby_delete_failed", guild=make_guild(123), channel=456,
                       operation="profile_delete", error=error)
    snapshot, = snapshots(caplog)
    assert snapshot["http_status"] == 403
    assert snapshot["discord_code"] == 50001
    assert snapshot["exception_type"] == "Forbidden"
    assert snapshot["channel_id"] == "456"
    assert "SECRET_TOKEN" not in caplog.text
    assert "private message" not in caplog.text


def test_missing_cache_stays_unknown_and_keeps_ids(caplog):
    log_error_snapshot("lifecycle_retry_pending", guild_id="123", channel="456")
    snapshot, = snapshots(caplog)
    assert snapshot["guild_id"] == "123"
    assert snapshot["channel_id"] == "456"
    assert all(value is None for value in snapshot["permissions"].values())
    assert snapshot["gateway_ready"] is None


def test_unreadable_permissions_do_not_hide_the_original_failure(caplog):
    guild = make_guild()
    channel = make_voice_channel(456, guild)
    channel.permissions_for.side_effect = RuntimeError("private failure")
    log_error_snapshot("room_delete_failed", guild=guild, channel=channel,
                       error=RuntimeError("private original"), bot=SimpleNamespace())
    snapshot, = snapshots(caplog)
    assert snapshot["channel_id"] == "456"
    assert snapshot["permissions"]["channel"] is None
    assert snapshot["permissions"]["guild"]["manage_channels"] is True
    assert snapshot["exception_type"] == "RuntimeError"
    assert "private" not in caplog.text


def test_interaction_snapshot_unwraps_errors_without_options_or_token(caplog):
    guild = make_guild()
    original = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                 {"code": 50013, "message": "SECRET"})
    interaction = SimpleNamespace(
        guild=guild, guild_id=guild.id, channel=None, channel_id=456,
        command=SimpleNamespace(name="delete", qualified_name="yap profile delete"),
        client=SimpleNamespace(is_ready=lambda: True),
        token="TOKEN", data={"options": "PRIVATE"}, user=Mock(),
    )
    log_interaction_error("app_command_failed", interaction,
                          discord.app_commands.CommandInvokeError(interaction.command, original))
    snapshot, = snapshots(caplog)
    assert snapshot["discord_code"] == 50013
    assert snapshot["operation"] == "yap profile delete"
    assert snapshot["gateway_ready"] is True
    assert all(secret not in caplog.text for secret in ("TOKEN", "PRIVATE", "SECRET"))
