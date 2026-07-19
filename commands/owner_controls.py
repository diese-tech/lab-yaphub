import discord

from services.ownership import resolve_owned_temp_channel
from services.room_actions import (
    apply_block,
    apply_hide,
    apply_limit,
    apply_lock,
    apply_permit,
    apply_rename,
    apply_transfer,
    apply_unblock,
    apply_unhide,
    apply_unlock,
    apply_unpermit,
    blocked_members,
    build_room_info_embed,
    permitted_members,
)


async def rename_temp_channel(bot, interaction: discord.Interaction, name: str) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_rename(bot, interaction, channel, name)


async def limit_temp_channel(bot, interaction: discord.Interaction, count: int) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_limit(bot, interaction, channel, count)


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
    await apply_unlock(bot, interaction, channel)


async def hide_owned_temp_channel(bot, interaction: discord.Interaction) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_hide(bot, interaction, channel)


async def unhide_owned_temp_channel(bot, interaction: discord.Interaction) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_unhide(bot, interaction, channel)


async def permit_member(bot, interaction: discord.Interaction, user: discord.Member) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_permit(bot, interaction, channel, user)


async def unpermit_member(bot, interaction: discord.Interaction, user: discord.Member) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_unpermit(bot, interaction, channel, user)


async def block_member(bot, interaction: discord.Interaction, user: discord.Member) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_block(bot, interaction, channel, user)


async def unblock_member(bot, interaction: discord.Interaction, user: discord.Member) -> None:
    channel = await resolve_owned_temp_channel(interaction, bot.storage)
    if channel is None:
        return
    await apply_unblock(bot, interaction, channel, user)


async def show_room_info(bot, interaction: discord.Interaction) -> None:
    # Read-only: anyone in a tracked room may view its info, so this skips
    # the ownership check that resolve_owned_temp_channel performs.
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    voice = interaction.user.voice
    channel = voice.channel if voice else None
    if not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message(
            "Join a Yap room before using this command.",
            ephemeral=True,
        )
        return

    record = await bot.storage.get_active_temp_channel(channel.id)
    if record is None or int(record["guild_id"]) != interaction.guild.id:
        await interaction.response.send_message(
            "That voice channel is not a tracked YapHub temp room.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=build_room_info_embed(
            channel,
            record,
            interaction.guild,
            permitted=await permitted_members(bot, interaction.guild, channel.id),
            blocked=await blocked_members(bot, interaction.guild, channel.id),
        ),
        ephemeral=True,
    )
