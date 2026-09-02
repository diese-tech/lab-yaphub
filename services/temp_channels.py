import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

import discord

from config import DEFAULT_TEMP_CHANNEL_PREFIX, RECONCILE_MIN_ROOM_AGE_SECONDS
from services.notifications import notify_duplicate_room
from services.ownership import active_channel_ids
from services.panel import send_room_panel
from services.permissions import (
    is_hidden,
    is_locked,
    missing_room_permissions,
    revoke_member_overwrites,
)
from services.room_actions import clear_rename_history, permitted_members

logger = logging.getLogger("yaphub")


async def delete_managed_channel(channel: discord.VoiceChannel, reason: str) -> bool:
    """Delete a room YapHub owns and report whether it is now confirmed gone.

    Returns True when the channel no longer exists -- either this call
    deleted it, or Discord answered 404, which is positive proof it is
    already absent. Returns False for a 403 or a 5xx: those mean the channel
    is still there and the caller MUST keep tracking it, because a managed
    Discord resource that still exists may never be forgotten (dropping the
    record is exactly how the orphaned-duplicate-room incident happened).

    NotFound is caught before HTTPException on purpose -- it subclasses it.
    """
    try:
        await channel.delete(reason=reason)
        return True
    except discord.NotFound:
        logger.info(
            "temp_room delete_already_gone guild=%s channel=%s",
            channel.guild.id,
            channel.id,
        )
        return True
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.exception(
            "temp_room delete_failed guild=%s channel=%s status=%s reason=%s",
            channel.guild.id,
            channel.id,
            getattr(error, "status", None),
            reason,
        )
        return False


def _record_age_seconds(row) -> float | None:
    """Seconds since the record was written, or None if it can't be read."""
    try:
        created_at = row["created_at"]
    except (KeyError, IndexError, TypeError):
        return None

    if not created_at:
        return None

    try:
        created = datetime.fromisoformat(str(created_at))
    except ValueError:
        return None

    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (datetime.now(UTC) - created).total_seconds()


def _is_too_young_to_reap(row) -> bool:
    """True while a record is new enough that an empty room is not proof of
    an orphan.

    create_temp_room writes the tracking record before `member.move_to`
    lands, and the member only shows up in `channel.members` once the
    resulting gateway event arrives. A reconcile pass that runs inside that
    window sees a brand-new room with zero members and would delete the room
    out from under the creation that is still in flight. The age check
    persists in SQLite, so it also holds across a restart, and it costs at
    most one extra reconcile interval for a genuinely abandoned room.
    """
    age = _record_age_seconds(row)
    return age is not None and age < RECONCILE_MIN_ROOM_AGE_SECONDS


class UnresolvableTempChannel(Exception):
    """The owner's recorded room could not be confirmed gone.

    Raised instead of reporting "no existing room", because that answer would
    have YapHub create a second room and overwrite the record for the first --
    which is exactly how a live room ends up untracked and permanent.
    """


async def _fetch_guild_channel(bot, guild: discord.Guild, channel_id: int):
    """Resolve a tracked channel id, refusing to hand back another guild's
    channel.

    `bot.fetch_channel` is process-wide, not guild-scoped: a corrupted or
    mis-keyed record would otherwise resolve to a channel in a different
    guild, and every caller of this either deletes that channel or treats it
    as a member's room. Guild scoping is checked here so it cannot be
    forgotten at a call site.

    Returns None when the id resolves to something that is not a voice
    channel (the record is stale). Raises UnresolvableTempChannel when it
    resolves into a different guild: that record is corrupt in a way YapHub
    cannot act on safely, and neither deleting the record nor touching the
    foreign channel would be right.

    NotFound/Forbidden/HTTPException are raised through to the caller, which
    decides whether the failure is proof of absence.
    """
    channel = guild.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    if not isinstance(channel, discord.VoiceChannel):
        return None

    if channel.guild is None or channel.guild.id != guild.id:
        logger.critical(
            "temp_room guild_mismatch expected_guild=%s actual_guild=%s channel=%s "
            "keeping_record=1 taking_no_action=1",
            guild.id,
            getattr(channel.guild, "id", None),
            channel_id,
        )
        raise UnresolvableTempChannel(channel_id)

    return channel


async def reconcile_active_temp_channels(bot) -> None:
    """Converge SQLite tracking with Discord's actual state.

    Never removes a record unless the room is confirmed absent or was just
    deleted successfully. Every row is processed independently: a single bad
    row must not abort the pass, because reconciliation is the recovery path
    for rooms whose cleanup already failed once.
    """
    tracked_ids: set[int] = set()
    # Captured up front so ids added by a create that runs concurrently with
    # this pass survive the reassignment at the end. Without this, a room
    # created mid-pass is dropped from the in-memory set while its record
    # lives on, and cleanup_temp_channel then ignores it until the next pass.
    ids_before: set[int] = set(bot.active_temp_channel_ids)

    for row in await bot.storage.list_active_temp_channels():
        channel_id = int(row["channel_id"])
        try:
            if await _reconcile_row(bot, row, channel_id):
                tracked_ids.add(channel_id)
        except Exception:
            # One unexpected failure (a storage error, a malformed row, an
            # unmodelled discord.py error) must not kill the pass or, via the
            # tasks.loop, reconciliation for the rest of the process's life.
            # Keep the room tracked: nothing here proved it is gone.
            logger.exception(
                "temp_room reconcile_row_failed guild=%s channel=%s",
                row["guild_id"],
                channel_id,
            )
            tracked_ids.add(channel_id)

    bot.active_temp_channel_ids = tracked_ids | (bot.active_temp_channel_ids - ids_before)


async def _reconcile_row(bot, row, channel_id: int) -> bool:
    """Reconcile one tracked room. Returns True if it should stay tracked."""
    guild = bot.get_guild(int(row["guild_id"]))

    if guild is None:
        # A guild missing from the cache is NOT proof YapHub left it -- it is
        # also what an outage or an incomplete startup looks like. Deleting
        # the record here would turn every live room in that guild into an
        # untracked orphan, permanently. Keep it and retry next pass.
        logger.warning(
            "temp_room reconcile_guild_uncached guild=%s channel=%s keeping_record=1",
            row["guild_id"],
            channel_id,
        )
        return True

    try:
        channel = await _fetch_guild_channel(bot, guild, channel_id)
    except UnresolvableTempChannel:
        # Already logged as critical. Keep the record: deleting it would
        # forget a channel that exists, and acting on it would touch another
        # guild's resource.
        return True
    except discord.NotFound:
        # 404 is the only positive proof the room is gone.
        logger.info(
            "temp_room reconcile_record_dropped guild=%s channel=%s reason=not_found",
            guild.id,
            channel_id,
        )
        await bot.storage.delete_active_temp_channel(channel_id)
        clear_rename_history(channel_id)
        return False
    except (discord.Forbidden, discord.HTTPException):
        # A 403 means it still exists but the bot currently can't see it (a
        # hide that denied @everyone without allowing the bot), and a 5xx
        # just means the API is having a bad minute. Dropping the record on
        # either one untracks a live room permanently: cleanup stops firing
        # for it and it outlives everyone in it.
        logger.warning(
            "temp_room reconcile_unfetchable guild=%s channel=%s keeping_record=1",
            guild.id,
            channel_id,
            exc_info=True,
        )
        return True

    if channel is None:
        logger.info(
            "temp_room reconcile_record_dropped guild=%s channel=%s reason=not_a_voice_channel",
            guild.id,
            channel_id,
        )
        await bot.storage.delete_active_temp_channel(channel_id)
        clear_rename_history(channel_id)
        return False

    if len(channel.members) == 0:
        if _is_too_young_to_reap(row):
            logger.debug(
                "temp_room reconcile_skip_young guild=%s channel=%s",
                guild.id,
                channel_id,
            )
            return True

        if await delete_managed_channel(
            channel, "YapHub reconcile cleanup for empty temp VC"
        ):
            await bot.storage.delete_active_temp_channel(channel_id)
            clear_rename_history(channel_id)
            logger.info(
                "temp_room reconcile_cleanup_ok guild=%s channel=%s",
                guild.id,
                channel_id,
            )
            return False

        # Cleanup failed again. The room still exists, so it stays tracked
        # and reconciliation retries on the next pass.
        logger.warning(
            "temp_room reconcile_cleanup_retry_pending guild=%s channel=%s tracking_preserved=1",
            guild.id,
            channel_id,
        )
        return True

    await bot.storage.touch_active_temp_channel(channel_id)

    if row["panel_message_id"] is None:
        await _backfill_panel_message(bot, guild, channel, row)

    return True


async def _backfill_panel_message(bot, guild: discord.Guild, channel: discord.VoiceChannel, row) -> None:
    # Rooms created before panel_message_id existed (or whose original post
    # failed) have no way to be refreshed on ownership change. Repost a
    # fresh panel and persist its id so it self-heals on the next
    # reconcile; if the owner has left the guild, leave it for a later pass.
    owner = guild.get_member(int(row["owner_user_id"]))
    if owner is None:
        return

    panel_message = await send_room_panel(
        channel,
        owner,
        locked=is_locked(channel),
        hidden=is_hidden(channel),
        permitted=await permitted_members(bot, guild, channel.id),
    )
    if panel_message is not None:
        await bot.storage.set_panel_message_id(channel.id, panel_message.id)


async def resolve_existing_owned_channel(
    bot,
    guild: discord.Guild,
    owner_user_id: int,
) -> discord.VoiceChannel | None:
    existing_record = await bot.storage.get_active_temp_channel_by_owner(guild.id, owner_user_id)
    if existing_record is None:
        return None

    channel_id = int(existing_record["channel_id"])

    try:
        channel = await _fetch_guild_channel(bot, guild, channel_id)
    except discord.NotFound:
        channel = None
    except (discord.Forbidden, discord.HTTPException) as error:
        # 403 (a hidden room the bot can't see) and 5xx are not proof the
        # room is gone; see the matching branch in reconcile.
        raise UnresolvableTempChannel(channel_id) from error

    if channel is None:
        await bot.storage.delete_active_temp_channel(channel_id)
        bot.active_temp_channel_ids.discard(channel_id)
        clear_rename_history(channel_id)
        return None

    if len(channel.members) == 0:
        if not await delete_managed_channel(
            channel, "YapHub removing empty replaced temp VC"
        ):
            # Still there and still ours: report it as the owner's existing
            # room so the caller blocks a duplicate instead of creating one.
            return channel

        await bot.storage.delete_active_temp_channel(channel_id)
        bot.active_temp_channel_ids.discard(channel_id)
        clear_rename_history(channel_id)
        return None

    return channel


async def create_temp_room(
    bot,
    member: discord.Member,
    lobby_channel: discord.VoiceChannel,
    profile: Mapping[str, object],
) -> None:
    key = (member.guild.id, member.id)
    entry = bot.user_creation_locks.get(key)
    if entry is None:
        entry = bot.user_creation_locks[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            await _create_temp_room_locked(bot, member, lobby_channel, profile)
    finally:
        entry[1] -= 1
        if entry[1] <= 0:
            bot.user_creation_locks.pop(key, None)


def _profile_value(profile: Mapping[str, object], key: str):
    try:
        return profile[key]
    except (KeyError, IndexError):
        return None


def build_temp_channel_name(
    member: discord.Member,
    profile: Mapping[str, object],
    prefix: str,
) -> str:
    template = _profile_value(profile, "temp_name_template")
    if template:
        return str(template).replace("{user}", member.display_name)[:100]

    name = f"{member.display_name}'s Yap"
    if prefix:
        name = f"{prefix} {name}"
    return name[:100]


def _profile_belongs_to_guild(profile: Mapping[str, object], guild_id: int) -> bool:
    """Guild scoping for the profile cache.

    The cache is keyed by lobby channel id across every guild YapHub is in.
    Channel ids are globally unique so a mismatch means the cache is stale or
    corrupt, and acting on it would create a room in one guild from another
    guild's configuration.
    """
    profile_guild_id = _profile_value(profile, "guild_id")
    if profile_guild_id is None:
        return True
    try:
        return int(profile_guild_id) == guild_id
    except (TypeError, ValueError):
        return False


async def _create_temp_room_locked(
    bot,
    member: discord.Member,
    lobby_channel: discord.VoiceChannel,
    profile: Mapping[str, object],
) -> None:
    guild = member.guild

    if not _profile_belongs_to_guild(profile, guild.id):
        logger.error(
            "temp_room profile_guild_mismatch guild=%s member=%s lobby=%s profile_guild=%s",
            guild.id,
            member.id,
            lobby_channel.id,
            _profile_value(profile, "guild_id"),
        )
        return

    try:
        existing_channel = await resolve_existing_owned_channel(bot, guild, member.id)
    except UnresolvableTempChannel:
        logger.warning(
            "temp_room create_blocked guild=%s member=%s reason=existing_room_unresolvable",
            guild.id,
            member.id,
            exc_info=True,
        )
        return

    if existing_channel is not None:
        logger.info(
            "temp_room create_blocked guild=%s member=%s existing_channel=%s reason=duplicate",
            guild.id,
            member.id,
            existing_channel.id,
        )
        await notify_duplicate_room(bot, member, lobby_channel, existing_channel)
        return

    category = None
    category_id = _profile_value(profile, "target_category_id")
    if category_id:
        category = guild.get_channel(int(category_id))
        if not isinstance(category, discord.CategoryChannel):
            category = None

    if category is None:
        category = lobby_channel.category

    # Preflight: a room YapHub can create but cannot move anyone into is the
    # exact shape of the orphaned-duplicate incident. Refusing before the
    # create means there is no Discord resource to roll back at all. This is
    # a guard, not a guarantee -- Discord can still reject the eventual
    # request, so the exception handling below stays mandatory.
    missing = missing_room_permissions(guild, lobby_channel, category)
    if missing:
        logger.error(
            "temp_room create_blocked guild=%s member=%s lobby=%s category=%s "
            "reason=missing_permissions missing=%s",
            guild.id,
            member.id,
            lobby_channel.id,
            getattr(category, "id", None),
            ",".join(missing),
        )
        return

    guild_config = await bot.storage.get_guild_config(guild.id)
    prefix = DEFAULT_TEMP_CHANNEL_PREFIX
    if guild_config and guild_config["temp_channel_prefix"] is not None:
        prefix = str(guild_config["temp_channel_prefix"]).strip()

    create_kwargs: dict[str, object] = {}
    default_limit = _profile_value(profile, "default_user_limit")
    if default_limit:
        create_kwargs["user_limit"] = max(0, min(99, int(default_limit)))

    temp_channel = await guild.create_voice_channel(
        name=build_temp_channel_name(member, profile, prefix),
        category=category,
        reason=f"YapHub temp VC for user {member.id}",
        **create_kwargs,
    )
    logger.info(
        "temp_room created guild=%s member=%s lobby=%s channel=%s category=%s",
        guild.id,
        member.id,
        lobby_channel.id,
        temp_channel.id,
        getattr(category, "id", None),
    )

    try:
        await bot.storage.create_active_temp_channel(
            channel_id=temp_channel.id,
            guild_id=guild.id,
            profile_id=str(profile["id"]),
            owner_user_id=member.id,
        )
    except Exception:
        # Broad on purpose: the channel already exists in Discord, so ANY
        # failure to record it would leave a resource YapHub has no memory
        # of. Undo the Discord side rather than keep an untrackable room.
        logger.critical(
            "temp_room persist_failed guild=%s member=%s channel=%s rolling_back=1",
            guild.id,
            member.id,
            temp_channel.id,
            exc_info=True,
        )
        if await delete_managed_channel(
            temp_channel, "Cleanup after failed YapHub tracking write"
        ):
            logger.info(
                "temp_room persist_rollback_ok guild=%s channel=%s",
                guild.id,
                temp_channel.id,
            )
        else:
            logger.critical(
                "temp_room orphan_untracked guild=%s member=%s channel=%s "
                "reason=persist_failed_and_rollback_failed",
                guild.id,
                member.id,
                temp_channel.id,
            )
        return

    bot.active_temp_channel_ids.add(temp_channel.id)

    try:
        await member.move_to(temp_channel, reason="Moved to newly created Yap room")
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.exception(
            "temp_room move_failed guild=%s member=%s from_channel=%s to_channel=%s status=%s",
            guild.id,
            member.id,
            lobby_channel.id,
            temp_channel.id,
            getattr(error, "status", None),
        )
        if await delete_managed_channel(temp_channel, "Cleanup after failed move"):
            logger.info(
                "temp_room rollback_ok guild=%s member=%s channel=%s",
                guild.id,
                member.id,
                temp_channel.id,
            )
            await bot.storage.delete_active_temp_channel(temp_channel.id)
            bot.active_temp_channel_ids.discard(temp_channel.id)
            clear_rename_history(temp_channel.id)
            return

        # The room still exists, so keep its ownership record. Dropping
        # the record here lets the next lobby event create another room
        # and leaves this one permanently invisible to reconciliation.
        logger.error(
            "temp_room rollback_failed guild=%s member=%s channel=%s "
            "tracking_preserved=1 duplicate_creation_blocked=1",
            guild.id,
            member.id,
            temp_channel.id,
        )
        return

    panel_message = await send_room_panel(temp_channel, member)
    if panel_message is not None:
        await bot.storage.set_panel_message_id(temp_channel.id, panel_message.id)


async def cleanup_temp_channel(
    bot,
    channel: discord.VoiceChannel,
    leaver: discord.Member | None = None,
) -> None:
    if channel.id not in bot.active_temp_channel_ids:
        return

    if len(channel.members) != 0:
        await bot.storage.touch_active_temp_channel(channel.id)
        if leaver is not None:
            record = await bot.storage.get_active_temp_channel(channel.id)
            permitted_ids = {
                int(row["user_id"]) for row in await bot.storage.list_permits(channel.id)
            }
            blocked_ids = {
                int(row["user_id"]) for row in await bot.storage.list_blocks(channel.id)
            }
            if (
                record is not None
                and int(record["owner_user_id"]) != leaver.id
                and leaver.id not in permitted_ids
                and leaver.id not in blocked_ids
            ):
                try:
                    await revoke_member_overwrites(
                        channel,
                        leaver,
                        reason="YapHub revoking room access for departed member",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to revoke overwrites for member %s in channel %s",
                        leaver.id,
                        channel.id,
                    )
        return

    if not await delete_managed_channel(channel, "YapHub deleting empty temp VC"):
        # Still there: keep tracking so reconciliation retries. Two members
        # leaving at once both land here; the second call sees a 404, which
        # counts as confirmed-gone below, so the record is still cleared.
        return

    await bot.storage.delete_active_temp_channel(channel.id)
    bot.active_temp_channel_ids.discard(channel.id)
    clear_rename_history(channel.id)
    logger.info(
        "temp_room cleanup_ok guild=%s channel=%s",
        channel.guild.id,
        channel.id,
    )


async def runtime_active_channel_ids(bot) -> set[int]:
    return active_channel_ids(await bot.storage.list_active_temp_channels())
