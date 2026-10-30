"""Private, cache-only error snapshots. Never fetch Discord data to diagnose it."""

import json
import logging
import uuid

logger = logging.getLogger("yaphub")

PERMISSIONS = (
    "administrator", "view_channel", "manage_channels", "manage_roles",
    "move_members", "connect", "send_messages", "embed_links",
    "read_message_history", "view_audit_log",
)


def _id(value):
    value = getattr(value, "id", value)
    if isinstance(value, int) or isinstance(value, str) and value.isdecimal():
        return str(value)
    return None


def _text(value):
    return value[:100] if isinstance(value, str) else None


def _permissions(scope, member):
    if scope is None or member is None:
        return None
    try:
        resolved = scope.permissions_for(member)
    except Exception:
        return None
    return _flags(resolved)


def _flags(resolved):
    if resolved is None:
        return None
    # Unknown/cache-missing must never be represented as a permission denial.
    return {key: value if isinstance(value := getattr(resolved, key, None), bool) else None
            for key in PERMISSIONS}


def _ready(bot):
    try:
        ready = bot.is_ready()
        return ready if isinstance(ready, bool) else None
    except Exception:
        return None


def log_interaction_error(event, interaction, error, *, operation=None):
    """Exclude command options, user identities, messages and interaction tokens."""
    log_error_snapshot(
        event, guild=interaction.guild, guild_id=interaction.guild_id,
        channel=interaction.channel or interaction.channel_id,
        error=error, bot=interaction.client,
        operation=operation or getattr(getattr(interaction, "command", None), "qualified_name", None),
    )


def log_error_snapshot(event, *, guild=None, guild_id=None, channel=None, lobby=None,
                       category=None, error=None, operation=None, profile_id=None,
                       intent_id=None, phase=None, missing=(), bot=None):
    """One bounded JSON record with effective permissions, never payloads/secrets.

    Callers pass operation names, not command arguments or API URLs. Diagnostic
    collection is best effort and must not change the failure/retry policy.
    """
    try:
        if category is None:
            category = getattr(channel, "category", None) or getattr(lobby, "category", None)
        me = getattr(guild, "me", None)
        original = getattr(error, "original", error)
        snapshot = {
            "schema": 1,
            "error_id": uuid.uuid4().hex,
            "event": event,
            "operation": _text(operation),
            "guild_id": _id(guild) or _id(guild_id),
            "guild_name": _text(getattr(guild, "name", None)),
            "bot_id": _id(me),
            "channel_id": _id(channel),
            "lobby_id": _id(lobby),
            "category_id": _id(category),
            "profile_id": _text(profile_id),
            "intent_id": _text(intent_id),
            "phase": _text(phase),
            "missing_permissions": list(missing)[:len(PERMISSIONS)],
            "permissions": {
                "guild": _flags(getattr(me, "guild_permissions", None)),
                "lobby": _permissions(lobby, me),
                "category": _permissions(category, me),
                "channel": _permissions(channel, me),
            },
            "exception_type": type(original).__name__ if original is not None else None,
            "http_status": getattr(original, "status", None) if isinstance(original, Exception) else None,
            "discord_code": getattr(original, "code", None) if isinstance(original, Exception) else None,
            "gateway_ready": _ready(bot),
        }
        logger.error("diagnostic_snapshot %s", json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True))
    except Exception:
        # Do not log the diagnostic exception: its message may contain private
        # object representations. The caller still logs/handles its own error.
        logger.error("diagnostic_snapshot collection_failed event=%s", event)
