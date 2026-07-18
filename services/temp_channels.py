import logging
from collections.abc import Mapping

import discord

from config import DEFAULT_TEMP_CHANNEL_PREFIX
from services.notifications import notify_duplicate_room
from services.ownership import active_channel_ids
from services.panel import send_room_panel

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

    bot.active_temp_channel_ids = tracked_ids


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
    lock = bot.user_creation_locks[(member.guild.id, member.id)]

    async with lock:
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

        temp_channel_name = f"{member.display_name}'s Yap"
        if prefix:
            temp_channel_name = f"{prefix} {temp_channel_name}"

        temp_channel = await member.guild.create_voice_channel(
            name=temp_channel_name,
            category=category,
            reason=f"YapHub temp VC for user {member.id}",
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

        await send_room_panel(temp_channel, member)


async def cleanup_temp_channel(bot, channel: discord.VoiceChannel) -> None:
    if channel.id not in bot.active_temp_channel_ids:
        return

    if len(channel.members) != 0:
        await bot.storage.touch_active_temp_channel(channel.id)
        return

    try:
        await channel.delete(reason="YapHub deleting empty temp VC")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to delete empty temp VC %s", channel.id)
        return

    await bot.storage.delete_active_temp_channel(channel.id)
    bot.active_temp_channel_ids.discard(channel.id)


async def runtime_active_channel_ids(bot) -> set[int]:
    return active_channel_ids(await bot.storage.list_active_temp_channels())
