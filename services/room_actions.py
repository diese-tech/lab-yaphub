import logging
import time

import discord

from services.permissions import (
    hide_temp_channel,
    is_hidden,
    is_locked,
    lock_temp_channel,
    unhide_temp_channel,
    unlock_temp_channel,
)

logger = logging.getLogger("yaphub")

# Discord hard-limits channel renames to 2 per 10 minutes per channel; edits
# beyond that are silently queued by discord.py until the bucket resets, which
# outlives the 3-second interaction window and surfaces as a confusing failure.
# Track renames per channel and refuse early with an honest message instead.
RENAME_LIMIT = 2
RENAME_WINDOW_SECONDS = 600.0
_rename_history: dict[int, list[float]] = {}


def _rename_retry_after(channel_id: int) -> float | None:
    now = time.monotonic()
    times = [t for t in _rename_history.get(channel_id, []) if now - t < RENAME_WINDOW_SECONDS]
    if times:
        _rename_history[channel_id] = times
    else:
        _rename_history.pop(channel_id, None)

    if len(times) >= RENAME_LIMIT:
        return times[0] + RENAME_WINDOW_SECONDS - now
    return None


def clear_rename_history(channel_id: int) -> None:
    _rename_history.pop(channel_id, None)


async def apply_rename(interaction: discord.Interaction, channel: discord.VoiceChannel, name: str) -> None:
    retry_after = _rename_retry_after(channel.id)
    if retry_after is not None:
        await interaction.response.send_message(
            "Discord only allows 2 channel renames per 10 minutes. "
            f"Try again in {int(retry_after) + 1} seconds.",
            ephemeral=True,
        )
        return

    await channel.edit(name=name, reason=f"YapHub rename by user {interaction.user.id}")
    _rename_history.setdefault(channel.id, []).append(time.monotonic())
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

    existing_owned = await bot.storage.get_active_temp_channel_by_owner(channel.guild.id, user.id)
    if existing_owned is not None and int(existing_owned["channel_id"]) != channel.id:
        await interaction.response.send_message(
            f"{user.mention} already owns another active Yap room. "
            "They need to close or transfer that one before receiving this one.",
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


async def apply_hide(bot, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
    owner = None
    record = await bot.storage.get_active_temp_channel(channel.id)
    if interaction.guild is not None and record is not None:
        owner = interaction.guild.get_member(int(record["owner_user_id"]))

    await hide_temp_channel(
        channel,
        reason=f"YapHub hide by user {interaction.user.id}",
        owner=owner,
    )
    await interaction.response.send_message(
        "Hid your Yap room. Only current members can see it.",
        ephemeral=True,
    )


async def apply_unhide(interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
    await unhide_temp_channel(channel, reason=f"YapHub unhide by user {interaction.user.id}")
    await interaction.response.send_message(
        "Your Yap room is visible again.",
        ephemeral=True,
    )


def build_room_info_embed(
    channel: discord.VoiceChannel,
    record,
    guild: discord.Guild,
) -> discord.Embed:
    owner = guild.get_member(int(record["owner_user_id"]))
    states = []
    if is_locked(channel):
        states.append("\U0001f512 Locked")
    if is_hidden(channel):
        states.append("\U0001f648 Hidden")
    if not states:
        states.append("\U0001f513 Open")

    embed = discord.Embed(title=channel.name, color=0x23D8FF)
    embed.add_field(
        name="Owner",
        value=owner.mention if owner else f"Unknown ({record['owner_user_id']})",
        inline=True,
    )
    embed.add_field(name="Members", value=str(len(channel.members)), inline=True)
    embed.add_field(
        name="Limit",
        value="Unlimited" if channel.user_limit == 0 else str(channel.user_limit),
        inline=True,
    )
    embed.add_field(name="State", value=" / ".join(states), inline=True)
    embed.add_field(name="Created", value=str(record["created_at"]), inline=True)
    embed.set_footer(text="YapHub")
    return embed


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

    existing_owned = await bot.storage.get_active_temp_channel_by_owner(
        channel.guild.id, interaction.user.id
    )
    if existing_owned is not None and int(existing_owned["channel_id"]) != channel.id:
        await interaction.response.send_message(
            "You already own another active Yap room. Transfer or close it before claiming a new one.",
            ephemeral=True,
        )
        return

    await bot.storage.transfer_active_temp_channel_owner(channel.id, interaction.user.id)
    await interaction.response.send_message(
        f"You are now the owner of {channel.mention}.",
        ephemeral=True,
    )
