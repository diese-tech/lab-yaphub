import asyncio
import logging
from collections.abc import Mapping

import discord

from config import DEFAULT_TEMP_CHANNEL_PREFIX
from services.notifications import notify_duplicate_room
from services.ownership import active_channel_ids
from services.panel import send_room_panel
from services.permissions import is_hidden, is_locked, revoke_member_overwrites
from services.room_actions import clear_rename_history, permitted_members

logger = logging.getLogger("yaphub")


async def reconcile_active_temp_channels(bot) -> None:
    tracked_ids: set[int] = set()

    for row in await bot.storage.list_active_temp_channels():
        guild = bot.get_guild(int(row["guild_id"]))
        channel_id = int(row["channel_id"])

        if guild is None:
            logger.info(
                "Removing stale temp channel record for missing guild %s",
                row["guild_id"],
            )
            await bot.storage.delete_active_temp_channel(channel_id)
            continue

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                fetched = None
            channel = fetched if isinstance(fetched, discord.VoiceChannel) else None

        if not isinstance(channel, discord.VoiceChannel):
            logger.info(
                "Removing stale temp channel record for missing channel %s",
                channel_id,
            )
            await bot.storage.delete_active_temp_channel(channel_id)
            continue

        if len(channel.members) == 0:
            try:
                await channel.delete(reason="YapHub reconcile cleanup for empty temp VC")
                await bot.storage.delete_active_temp_channel(channel_id)
                logger.info("Deleted empty orphan temp channel %s", channel_id)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to delete empty temp channel %s", channel_id)
            continue

        await bot.storage.touch_active_temp_channel(channel_id)
        tracked_ids.add(channel_id)

        if row["panel_message_id"] is None:
            await _backfill_panel_message(bot, guild, channel, row)

    bot.active_temp_channel_ids = tracked_ids


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
    channel = guild.get_channel(channel_id)

    if channel is None:
        try:
            fetched = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            fetched = None
        channel = fetched if isinstance(fetched, discord.VoiceChannel) else None

    if not isinstance(channel, discord.VoiceChannel):
        await bot.storage.delete_active_temp_channel(channel_id)
        bot.active_temp_channel_ids.discard(channel_id)
        return None

    if len(channel.members) == 0:
        try:
            await channel.delete(reason="YapHub removing empty replaced temp VC")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to delete empty replaced temp VC %s", channel_id)
            return channel

        await bot.storage.delete_active_temp_channel(channel_id)
        bot.active_temp_channel_ids.discard(channel_id)
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


async def _create_temp_room_locked(
    bot,
    member: discord.Member,
    lobby_channel: discord.VoiceChannel,
    profile: Mapping[str, object],
) -> None:
    existing_channel = await resolve_existing_owned_channel(bot, member.guild, member.id)
    if existing_channel is not None:
        await notify_duplicate_room(bot, member, lobby_channel, existing_channel)
        return

    category = None
    category_id = profile["target_category_id"]
    if category_id:
        category = member.guild.get_channel(int(category_id))
        if not isinstance(category, discord.CategoryChannel):
            category = None

    if category is None:
        category = lobby_channel.category

    guild_config = await bot.storage.get_guild_config(member.guild.id)
    prefix = DEFAULT_TEMP_CHANNEL_PREFIX
    if guild_config and guild_config["temp_channel_prefix"] is not None:
        prefix = str(guild_config["temp_channel_prefix"]).strip()

    create_kwargs: dict[str, object] = {}
    default_limit = _profile_value(profile, "default_user_limit")
    if default_limit:
        create_kwargs["user_limit"] = max(0, min(99, int(default_limit)))

    temp_channel = await member.guild.create_voice_channel(
        name=build_temp_channel_name(member, profile, prefix),
        category=category,
        reason=f"YapHub temp VC for user {member.id}",
        **create_kwargs,
    )

    await bot.storage.create_active_temp_channel(
        channel_id=temp_channel.id,
        guild_id=member.guild.id,
        profile_id=str(profile["id"]),
        owner_user_id=member.id,
    )
    bot.active_temp_channel_ids.add(temp_channel.id)

    try:
        await member.move_to(temp_channel, reason="Moved to newly created Yap room")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Failed to move user %s into temp channel %s", member.id, temp_channel.id
        )
        try:
            await temp_channel.delete(reason="Cleanup after failed move")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Failed to cleanup temp channel %s after failed move", temp_channel.id
            )
        await bot.storage.delete_active_temp_channel(temp_channel.id)
        bot.active_temp_channel_ids.discard(temp_channel.id)
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

    try:
        await channel.delete(reason="YapHub deleting empty temp VC")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to delete empty temp VC %s", channel.id)
        return

    await bot.storage.delete_active_temp_channel(channel.id)
    bot.active_temp_channel_ids.discard(channel.id)
    clear_rename_history(channel.id)


async def runtime_active_channel_ids(bot) -> set[int]:
    return active_channel_ids(await bot.storage.list_active_temp_channels())
