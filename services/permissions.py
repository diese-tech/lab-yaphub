import logging

import discord

logger = logging.getLogger("yaphub")


def missing_room_permissions(
    guild: discord.Guild,
    lobby_channel: discord.VoiceChannel,
    category: discord.CategoryChannel | None,
) -> list[str]:
    """Names of the permissions YapHub needs to create a temp room and move
    the member into it, and does not currently have.

    Empty means "nothing provably missing" -- including when the bot's own
    member object isn't cached, where guessing would be worse than trying.
    This deliberately fails OPEN: a false positive would silently stop rooms
    being created in a working server, which is worse than the failure this
    preflight prevents.

    It is a preflight, not a substitute for exception handling. Discord
    resolves permissions server-side at request time and can still answer
    403 for a request this function approved.
    """
    me = getattr(guild, "me", None)
    if me is None:
        return []

    missing: list[str] = []

    # Creating the room: Manage Channels where the room will land.
    create_scope = category if category is not None else lobby_channel
    if not _has(create_scope, me, "manage_channels"):
        missing.append("Manage Channels")

    # Moving the member: Move Members is evaluated on the source channel and
    # on the destination. The destination does not exist yet, so its
    # permissions are the category's (the room is created inside it).
    move_scopes = [lobby_channel] if category is None else [lobby_channel, category]
    if not all(_has(scope, me, "move_members") for scope in move_scopes):
        missing.append("Move Members")

    return missing


def _has(scope, member: discord.Member, attribute: str) -> bool:
    """Read one resolved permission, treating an unreadable scope as allowed.

    Anything other than a real False (a mock, a missing attribute, a
    discord.py object that cannot resolve permissions) must not block room
    creation -- see the fail-open contract above.
    """
    permissions_for = getattr(scope, "permissions_for", None)
    if permissions_for is None:
        return True

    try:
        resolved = permissions_for(member)
    except (AttributeError, TypeError):
        return True

    return getattr(resolved, attribute, True) is not False


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
    denied: tuple[discord.Member, ...] = (),
) -> None:
    default_role = channel.guild.default_role
    denied_ids = {member.id for member in denied}

    # YapHub is not a voice member of the room, so the @everyone deny below
    # applies to the bot itself: without a member-level allow, hiding a room
    # hides it *from YapHub*. The bot is invited without Administrator, so it
    # then loses View Channel on the room -- unhide can no longer resolve the
    # channel, the panel can't be refreshed, the empty-room cleanup never
    # fires, and reconcile sees an unfetchable channel. That is how a hidden
    # room outlives everyone in it. The allow is written first so there is
    # never a window where the bot is locked out, and it is a member-level
    # overwrite so it also survives the role deny loop below (member
    # overwrites win over role overwrites in Discord's resolution).
    bot_member = channel.guild.me
    if bot_member is not None:
        bot_overwrite = channel.overwrites_for(bot_member)
        setattr(bot_overwrite, attr, True)
        await channel.set_permissions(bot_member, overwrite=bot_overwrite, reason=reason)

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

    allowed_members: list[discord.Member] = []
    seen_ids: set[int] = {bot_member.id} if bot_member is not None else set()
    for candidate in (owner, *extra_allowed, *channel.members):
        # A blocked member is normally disconnected on block, but if that
        # disconnect failed they are still in channel.members -- granting them
        # an allow here would silently undo their block.
        if candidate is None or candidate.id in seen_ids or candidate.id in denied_ids:
            continue
        allowed_members.append(candidate)
        seen_ids.add(candidate.id)

    for member in allowed_members:
        member_overwrite = channel.overwrites_for(member)
        setattr(member_overwrite, attr, True)
        await channel.set_permissions(member, overwrite=member_overwrite, reason=reason)


async def _lift_default_restriction(
    channel: discord.VoiceChannel,
    attr: str,
    reason: str,
    preserve: tuple[discord.Member, ...] = (),
) -> None:
    # Permitted members hold standing allows that are theirs until they are
    # unpermitted or the room closes; clearing them here would let one
    # hide/unhide cycle silently empty the room's permit list. The bot's own
    # allow is kept too: the room may still be restricted on the *other*
    # attribute, and it costs nothing on a temp channel that gets deleted.
    preserved_ids = {member.id for member in preserve if member is not None}
    bot_member = channel.guild.me
    if bot_member is not None:
        preserved_ids.add(bot_member.id)

    await _clear_overwrite_attr(channel, channel.guild.default_role, attr, reason)

    category = channel.category
    for target, overwrite in list(channel.overwrites.items()):
        if isinstance(target, discord.Member) and getattr(overwrite, attr) is True:
            if target.id in preserved_ids:
                continue
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
    denied: tuple[discord.Member, ...] = (),
) -> None:
    await _restrict_default_allow_members(
        channel, "connect", reason, owner=owner, extra_allowed=extra_allowed, denied=denied
    )


async def unlock_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    preserve: tuple[discord.Member, ...] = (),
) -> None:
    await _lift_default_restriction(channel, "connect", reason, preserve=preserve)


async def hide_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    owner: discord.Member | None = None,
    extra_allowed: tuple[discord.Member, ...] = (),
    denied: tuple[discord.Member, ...] = (),
) -> None:
    await _restrict_default_allow_members(
        channel, "view_channel", reason, owner=owner, extra_allowed=extra_allowed, denied=denied
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


async def unhide_temp_channel(
    channel: discord.VoiceChannel,
    reason: str,
    preserve: tuple[discord.Member, ...] = (),
) -> None:
    await _lift_default_restriction(channel, "view_channel", reason, preserve=preserve)


def is_locked(channel: discord.VoiceChannel) -> bool:
    return channel.overwrites_for(channel.guild.default_role).connect is False


def is_hidden(channel: discord.VoiceChannel) -> bool:
    return channel.overwrites_for(channel.guild.default_role).view_channel is False
