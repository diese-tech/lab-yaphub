"""Durable, privacy-safe usage telemetry.

Answers "what has YapHub done over time?" -- rooms created, by whom, how
often creation fails -- as opposed to the operational state in
active_temp_channels, which answers "what Discord resources currently
exist and belong to YapHub?" The two are stored separately on purpose:
operational rows are deleted the moment a room is cleaned up; telemetry
rows must survive that and every subsequent restart.

Privacy boundary
-----------------
Nothing here ever persists a raw Discord user or guild ID. Where an exact
unique-entity count requires recognizing the same user or guild again
later (telemetry_known_users / telemetry_known_guilds), the stored key is

    HMAC-SHA256(YAPHUB_ANALYTICS_SECRET, "<user|guild>:<discord_id>")

not the ID itself -- see pseudonymous_user_key / pseudonymous_guild_key.
The "user:" / "guild:" namespace prefix exists so a user and a guild that
happen to share the same numeric snowflake never hash to the same key.
This is pseudonymization, not anonymization: the HMAC output is still an
internal identifier (the same user always produces the same key) and must
never be exposed on any public surface -- only aggregate counts derived
from it may be. A keyed HMAC (not a plain hash) is used specifically so
the key cannot be recomputed or reversed by anyone without the secret,
including someone who has read this source file.

Counter-only metrics (room creation totals, reliability counters) never
touch identity at all -- they are a plain daily count bump with no user or
guild reference stored anywhere. Pseudonymous keys are written only for
the one purpose that actually needs them: recognizing a repeat entity for
an exact lifetime-unique count.

Every function in this module is best-effort: a telemetry failure (a
missing secret, a storage error) must never affect the temp-room lifecycle
it is measuring. Callers in services/temp_channels.py do not need their
own try/except around these calls.
"""

import hashlib
import hmac
import logging
import os

from config import (
    ANALYTICS_SECRET_ENV_VAR,
    TELEMETRY_EVENT_DUPLICATE_BLOCKED,
    TELEMETRY_EVENT_RECONCILE_CLEANUP_FAILED,
    TELEMETRY_EVENT_RECONCILE_CLEANUP_OK,
    TELEMETRY_EVENT_ROLLBACK_FAILED_TRACKING_PRESERVED,
    TELEMETRY_EVENT_ROOM_CREATE_FAILED,
    TELEMETRY_EVENT_ROOM_CREATED,
)

logger = logging.getLogger("yaphub")

_warned_missing_secret = False


def _get_secret() -> str | None:
    """Read the analytics secret fresh on every call (not cached at import
    time) so it can change between a real deploy and a test's monkeypatched
    environment without a process restart.

    Missing is a normal, supported state: telemetry degrades gracefully
    (unique-entity counts pause; everything else keeps working) rather than
    failing startup over an optional feature. Warns once per process so a
    genuinely missing secret is diagnosable without spamming the log on
    every room creation.
    """
    secret = os.getenv(ANALYTICS_SECRET_ENV_VAR)
    if secret:
        return secret

    global _warned_missing_secret
    if not _warned_missing_secret:
        _warned_missing_secret = True
        logger.warning(
            "%s is not set; unique user/guild telemetry is disabled until it is "
            "configured. Room-count and reliability metrics are unaffected.",
            ANALYTICS_SECRET_ENV_VAR,
        )
    return None


def analytics_secret_configured() -> bool:
    """Whether YAPHUB_ANALYTICS_SECRET is set, without the missing-secret
    warning or logging side effects _get_secret() has -- for callers (the
    public stats builder) that need to branch on this on every call and
    must not spam the log doing it."""
    return bool(os.getenv(ANALYTICS_SECRET_ENV_VAR))


def _pseudonymous_key(namespace: str, discord_id: int) -> str | None:
    secret = _get_secret()
    if secret is None:
        return None
    message = f"{namespace}:{discord_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def pseudonymous_user_key(user_id: int) -> str | None:
    """A stable, unreversible-without-the-secret key for this Discord user,
    or None if YAPHUB_ANALYTICS_SECRET is not configured."""
    return _pseudonymous_key("user", user_id)


def pseudonymous_guild_key(guild_id: int) -> str | None:
    """A stable, unreversible-without-the-secret key for this Discord
    guild, or None if YAPHUB_ANALYTICS_SECRET is not configured."""
    return _pseudonymous_key("guild", guild_id)


async def record_event(bot, event_type: str) -> None:
    """Bump today's UTC counter for event_type by one. Best-effort."""
    try:
        await bot.storage.record_telemetry_event(event_type)
    except Exception:
        logger.exception("Failed to record telemetry event %s", event_type)


async def record_room_created(bot, guild_id: int, user_id: int) -> None:
    """Record a successful room creation, and -- if the analytics secret is
    configured -- fold the guild and user into the lifetime-unique-entity
    tables. Each step is independently best-effort: a failure folding in
    the guild must not skip the user, or the room-created count itself."""
    await record_event(bot, TELEMETRY_EVENT_ROOM_CREATED)

    guild_key = pseudonymous_guild_key(guild_id)
    if guild_key is not None:
        try:
            await bot.storage.record_known_guild(guild_key)
        except Exception:
            logger.exception("Failed to record known guild for telemetry")

    user_key = pseudonymous_user_key(user_id)
    if user_key is not None:
        try:
            await bot.storage.record_known_user(user_key)
        except Exception:
            logger.exception("Failed to record known user for telemetry")


def _secret_fingerprint(secret: str) -> str:
    """A non-reversible fingerprint of the configured secret, stored only to
    detect whether it changed between backfill runs. Never used for
    pseudonymization itself -- that stays HMAC-keyed per entity, see
    _pseudonymous_key."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def backfill_known_guilds(bot) -> None:
    """One-time-per-secret reconciliation: fold every guild with an actual
    configured profile (ran /yap setup or /yap profile create) into
    telemetry_known_guilds.

    Without this, servers_served only grows from record_room_created()
    going forward -- so a guild that configured and used YapHub *before*
    YAPHUB_ANALYTICS_SECRET was ever set (or before this deployment) would
    sit uncounted until it happens to create a fresh room after the secret
    is live.

    Guarded against secret rotation: a rotated secret hashes the same guild
    differently, and record_known_guild's `insert or ignore` only dedupes
    identical hashes -- so blindly re-running this on every startup after a
    rotation would insert a second, permanently-unmatchable row per guild
    and silently inflate servers_served. This function fingerprints the
    configured secret and compares it against the fingerprint stored from
    the last successful backfill (see storage.py's
    get/set_telemetry_backfill_secret_fingerprint); a mismatch means the
    secret changed, and the backfill is skipped (with a warning) rather
    than guessing how to reconcile the old entries -- that needs a human
    decision (see README's Usage Telemetry section). Best-effort: never
    raises.
    """
    secret = _get_secret()
    if secret is None:
        return
    fingerprint = _secret_fingerprint(secret)

    try:
        previous_fingerprint = await bot.storage.get_telemetry_backfill_secret_fingerprint()
    except Exception:
        logger.exception("Failed to read telemetry backfill state")
        return

    if previous_fingerprint is not None and previous_fingerprint != fingerprint:
        logger.warning(
            "%s appears to have changed since the last telemetry backfill; "
            "skipping backfill to avoid inflating servers_served with "
            "duplicate guild entries. See README's Usage Telemetry section.",
            ANALYTICS_SECRET_ENV_VAR,
        )
        return

    try:
        guild_ids = await bot.storage.list_guild_ids_with_profiles()
    except Exception:
        logger.exception("Failed to list guild ids for telemetry backfill")
        return

    for guild_id in guild_ids:
        guild_key = pseudonymous_guild_key(guild_id)
        if guild_key is None:
            continue
        try:
            await bot.storage.record_known_guild(guild_key)
        except Exception:
            logger.exception("Failed to backfill known guild for telemetry")

    try:
        await bot.storage.set_telemetry_backfill_secret_fingerprint(fingerprint)
    except Exception:
        logger.exception("Failed to persist telemetry backfill fingerprint")


async def record_room_create_failed(bot) -> None:
    await record_event(bot, TELEMETRY_EVENT_ROOM_CREATE_FAILED)


async def record_duplicate_blocked(bot) -> None:
    await record_event(bot, TELEMETRY_EVENT_DUPLICATE_BLOCKED)


async def record_rollback_failed_tracking_preserved(bot) -> None:
    await record_event(bot, TELEMETRY_EVENT_ROLLBACK_FAILED_TRACKING_PRESERVED)


async def record_reconcile_cleanup_ok(bot) -> None:
    await record_event(bot, TELEMETRY_EVENT_RECONCILE_CLEANUP_OK)


async def record_reconcile_cleanup_failed(bot) -> None:
    await record_event(bot, TELEMETRY_EVENT_RECONCILE_CLEANUP_FAILED)
