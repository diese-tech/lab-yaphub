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
    extra_allowed: tuple[discord.Member, ...] = (),
) -> None:
    default_role = channel.guild.default_role
    default_overwrite = channel.overwrites_for(default_role)
    setattr(default_overwrite, attr, False)
    await channel.set_permissions(default_role, overwrite=default_overwrite, reason=reason)

    # Role-level allows (typically synced from the category) union-override the
    # @everyone deny in Discord's permission resolution, so they must be
    # flipped to deny too or role holders bypass the restriction entirely.
    for target, overwrite in list(channel.overwrites.items()):
        if (
            isinstance(target, discord.Role)
            and target != default_role
            and getattr(overwrite, attr) is True
        ):
            setattr(overwrite, attr, False)
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)

    allowed_members = list(channel.members)
    seen_ids = {member.id for member in allowed_members}
    for extra in (owner, *extra_allowed):
        if extra is not None and extra.id not in seen_ids:
            allowed_members.append(extra)
            seen_ids.add(extra.id)

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

    category = channel.category
    for target, overwrite in list(channel.overwrites.items()):
        if isinstance(target, discord.Member) and getattr(overwrite, attr) is True:
            await _clear_overwrite_attr(channel, target, attr, reason)
        elif (
            isinstance(target, discord.Role)
            and target != channel.guild.default_role
            and getattr(overwrite, attr) is False
        ):
            # Restore the category's value for this role (the category is the
            # permission source of truth); a role the category genuinely denies
            # stays denied.
            desired = (
                getattr(category.overwrites_for(target), attr)
                if category is not None
                else None
            )
            if desired is not False:
                setattr(overwrite, attr, desired)
                if overwrite.is_empty():
                    await channel.set_permissions(target, overwrite=None, reason=reason)
                else:
                    await channel.set_permissions(target, overwrite=overwrite, reason=reason)


async def revoke_member_overwrites(
    channel: discord.VoiceChannel,
    member: discord.Member,
    reason: str,
) -> None:
    """Drop a departed member's lock/hide allows so leaving (or being kicked)
    actually removes their access to a restricted room."""
    overwrite = channel.overwrites_for(member)
    changed = False
    for attr in ("connect", "view_channel"):
        if getattr(overwrite, attr) is not None:
            setattr(overwrite, attr, None)
            changed = True

    if not changed:
        return

    if overwrite.is_empty():
        await channel.set_permissions(member, overwrite=None, reason=reason)
    else:
        await channel.set_permissions(member, overwrite=overwrite, reason=reason)


async def lock_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    owner: discord.Member | None = None,
    extra_allowed: tuple[discord.Member, ...] = (),
) -> None:
    await _restrict_default_allow_members(
        channel, "connect", reason, owner=owner, extra_allowed=extra_allowed
    )


async def unlock_temp_channel(channel: discord.VoiceChannel, reason: str) -> None:
    await _lift_default_restriction(channel, "connect", reason)


async def hide_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    owner: discord.Member | None = None,
    extra_allowed: tuple[discord.Member, ...] = (),
) -> None:
    await _restrict_default_allow_members(
        channel, "view_channel", reason, owner=owner, extra_allowed=extra_allowed
    )


async def grant_member_access(
    channel: discord.VoiceChannel,
    member: discord.Member,
    reason: str,
) -> None:
    """Give a member standing view/connect allows (a permit) so restrictions
    and departure-revocation don't apply to them."""
    overwrite = channel.overwrites_for(member)
    overwrite.view_channel = True
    overwrite.connect = True
    await channel.set_permissions(member, overwrite=overwrite, reason=reason)


async def deny_member_access(
    channel: discord.VoiceChannel,
    member: discord.Member,
    reason: str,
) -> None:
    """Persistently block a member from seeing/joining this room until
    unblocked. Per-member overwrites always beat role/@everyone overwrites,
    so this holds regardless of the room's lock/hide state."""
    overwrite = channel.overwrites_for(member)
    overwrite.view_channel = False
    overwrite.connect = False
    await channel.set_permissions(member, overwrite=overwrite, reason=reason)


async def unhide_temp_channel(channel: discord.VoiceChannel, reason: str) -> None:
    await _lift_default_restriction(channel, "view_channel", reason)


def is_locked(channel: discord.VoiceChannel) -> bool:
    return channel.overwrites_for(channel.guild.default_role).connect is False


def is_hidden(channel: discord.VoiceChannel) -> bool:
    return channel.overwrites_for(channel.guild.default_role).view_channel is False
