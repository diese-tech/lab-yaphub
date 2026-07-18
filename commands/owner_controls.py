import discord

from services.ownership import resolve_owned_temp_channel
from services.room_actions import apply_lock, apply_rename, apply_transfer, apply_limit, apply_unlock


async def rename_temp_channel(bot, interaction: discord.Interaction, name: str) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_rename(interaction, channel, name)


async def limit_temp_channel(bot, interaction: discord.Interaction, count: int) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_limit(interaction, channel, count)


async def transfer_temp_channel(
    bot,
    interaction: discord.Interaction,
    user: discord.Member,
) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_transfer(bot, interaction, channel, user)


async def lock_owned_temp_channel(bot, interaction: discord.Interaction) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_lock(bot, interaction, channel)


async def unlock_owned_temp_channel(bot, interaction: discord.Interaction) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_unlock(interaction, channel)
