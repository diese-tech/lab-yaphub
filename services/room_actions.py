import logging

import discord

from services.permissions import lock_temp_channel, unlock_temp_channel

logger = logging.getLogger("yaphub")


async def apply_rename(interaction: discord.Interaction, channel: discord.VoiceChannel, name: str) -> None:
    await channel.edit(name=name, reason=f"YapHub rename by user {interaction.user.id}")
    await interaction.response.send_message(
        f"Renamed your Yap room to `{name}`.",
        ephemeral=True,
    )


async def apply_limit(interaction: discord.Interaction, channel: discord.VoiceChannel, count: int) -> None:
    await channel.edit(user_limit=count, reason=f"YapHub limit by user {interaction.user.id}")
    label = "unlimited" if count == 0 else str(count)
    await interaction.response.send_message(
        f"Set your Yap room limit to `{label}`.",
        ephemeral=True,
    )


async def apply_transfer(
    bot,
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    user: discord.Member,
) -> None:
    if user.bot:
        await interaction.response.send_message(
            "Yap rooms can only be transferred to server members.",
            ephemeral=True,
        )
        return

    if user not in channel.members:
        await interaction.response.send_message(
            "Transfer target must be in your Yap room.",
            ephemeral=True,
        )
        return

    await bot.storage.transfer_active_temp_channel_owner(channel.id, user.id)
    await interaction.response.send_message(
        f"Transferred ownership of {channel.mention} to {user.mention}.",
        ephemeral=True,
    )


async def apply_lock(bot, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
    owner = None
    record = await bot.storage.get_active_temp_channel(channel.id)
    if interaction.guild is not None and record is not None:
        owner = interaction.guild.get_member(int(record["owner_user_id"]))

    await lock_temp_channel(
        channel,
        reason=f"YapHub lock by user {interaction.user.id}",
        owner=owner,
    )
    await interaction.response.send_message(
        "Locked your Yap room.",
        ephemeral=True,
    )


async def apply_unlock(interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
    await unlock_temp_channel(channel, reason=f"YapHub unlock by user {interaction.user.id}")
    await interaction.response.send_message(
        "Unlocked your Yap room.",
        ephemeral=True,
    )


async def apply_kick(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    member: discord.Member,
) -> None:
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You can't remove yourself from your own room.",
            ephemeral=True,
        )
        return

    if member not in channel.members:
        await interaction.response.send_message(
            f"{member.mention} is not in your Yap room.",
            ephemeral=True,
        )
        return

    try:
        await member.move_to(None, reason=f"YapHub kick by user {interaction.user.id}")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to kick member %s from channel %s", member.id, channel.id)
        await interaction.response.send_message(
            "I couldn't remove that member. Check my Move Members permission.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Removed {member.mention} from your Yap room.",
        ephemeral=True,
    )


async def apply_claim(bot, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
    record = await bot.storage.get_active_temp_channel(channel.id)
    if record is None:
        await interaction.response.send_message(
            "That voice channel is not a tracked YapHub temp room.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or interaction.user not in channel.members:
        await interaction.response.send_message(
            "Join the room before claiming it.",
            ephemeral=True,
        )
        return

    owner_id = int(record["owner_user_id"])
    if any(member.id == owner_id for member in channel.members):
        await interaction.response.send_message(
            "The current owner is still in the room.",
            ephemeral=True,
        )
        return

    await bot.storage.transfer_active_temp_channel_owner(channel.id, interaction.user.id)
    await interaction.response.send_message(
        f"You are now the owner of {channel.mention}.",
        ephemeral=True,
    )
