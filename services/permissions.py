import discord


def require_manage_channels(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_channels
    )


async def _clear_overwrite_attr(
    channel: discord.VoiceChannel,
    target: discord.Role | discord.Member,
    attr: str,
    reason: str,
) -> None:
    overwrite = channel.overwrites_for(target)
    setattr(overwrite, attr, None)

    if overwrite.is_empty():
        await channel.set_permissions(target, overwrite=None, reason=reason)
        return

    await channel.set_permissions(target, overwrite=overwrite, reason=reason)


async def _restrict_default_allow_members(
    channel: discord.VoiceChannel,
    attr: str,
    reason: str,
    owner: discord.Member | None = None,
) -> None:
    default_role = channel.guild.default_role
    default_overwrite = channel.overwrites_for(default_role)
    setattr(default_overwrite, attr, False)
    await channel.set_permissions(default_role, overwrite=default_overwrite, reason=reason)

    allowed_members = list(channel.members)
    if owner is not None and all(member.id != owner.id for member in allowed_members):
        allowed_members.append(owner)

    for member in allowed_members:
        member_overwrite = channel.overwrites_for(member)
        setattr(member_overwrite, attr, True)
        await channel.set_permissions(member, overwrite=member_overwrite, reason=reason)


async def _lift_default_restriction(
    channel: discord.VoiceChannel,
    attr: str,
    reason: str,
) -> None:
    await _clear_overwrite_attr(channel, channel.guild.default_role, attr, reason)

    for target, overwrite in list(channel.overwrites.items()):
        if isinstance(target, discord.Member) and getattr(overwrite, attr) is True:
            await _clear_overwrite_attr(channel, target, attr, reason)


async def lock_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    owner: discord.Member | None = None,
) -> None:
    await _restrict_default_allow_members(channel, "connect", reason, owner=owner)


async def unlock_temp_channel(channel: discord.VoiceChannel, reason: str) -> None:
    await _lift_default_restriction(channel, "connect", reason)


async def hide_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    owner: discord.Member | None = None,
) -> None:
    await _restrict_default_allow_members(channel, "view_channel", reason, owner=owner)


async def unhide_temp_channel(channel: discord.VoiceChannel, reason: str) -> None:
    await _lift_default_restriction(channel, "view_channel", reason)


def is_locked(channel: discord.VoiceChannel) -> bool:
    return channel.overwrites_for(channel.guild.default_role).connect is False


def is_hidden(channel: discord.VoiceChannel) -> bool:
    return channel.overwrites_for(channel.guild.default_role).view_channel is False
