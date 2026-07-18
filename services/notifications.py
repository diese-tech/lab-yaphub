import asyncio
import logging

import discord

from config import (
    DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
    NOTIFICATION_DELETE_AFTER_SECONDS,
)

logger = logging.getLogger("yaphub")


async def notify_duplicate_room(
    bot,
    member: discord.Member,
    lobby_channel: discord.VoiceChannel,
    existing_channel: discord.VoiceChannel,
) -> None:
    cooldown_seconds = DEFAULT_NOTIFICATION_COOLDOWN_SECONDS
    guild_config = await bot.storage.get_guild_config(member.guild.id)
    if guild_config:
        cooldown_seconds = int(
            guild_config["notification_cooldown_seconds"]
            or DEFAULT_NOTIFICATION_COOLDOWN_SECONDS
        )

    cooldown_key = (member.guild.id, member.id)
    now = asyncio.get_running_loop().time()
    next_allowed_at = bot.notification_cooldowns.get(cooldown_key, 0.0)
    if now < next_allowed_at:
        return

    bot.notification_cooldowns[cooldown_key] = now + cooldown_seconds

    message = (
        f"You already have an active Yap room in **{member.guild.name}**: "
        f"{existing_channel.name}. Close that VC before creating a new one."
    )

    try:
        await member.send(message)
        return
    except (discord.Forbidden, discord.HTTPException):
        logger.info("DM failed for duplicate-room notice to user %s", member.id)

    fallback_message = (
        f"{member.mention} you already have an active Yap room: "
        f"{existing_channel.mention}. Close that VC before creating a new one."
    )

    for target in (lobby_channel, member.guild.system_channel):
        if target is None or not hasattr(target, "send"):
            continue

        try:
            await target.send(
                fallback_message,
                delete_after=NOTIFICATION_DELETE_AFTER_SECONDS,
            )
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.info("Failed to send duplicate-room fallback notice in %s", target.id)
