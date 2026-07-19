import logging

import discord
from discord import ui

from services.ownership import resolve_owned_temp_channel_by_id
from services.permissions import is_hidden, is_locked
from services.room_actions import (
    apply_block,
    apply_claim,
    apply_hide,
    apply_kick,
    apply_limit,
    apply_lock,
    apply_permit,
    apply_rename,
    apply_transfer,
    apply_unblock,
    apply_unhide,
    apply_unlock,
    apply_unpermit,
    permitted_members,
)

logger = logging.getLogger("yaphub")

PANEL_EMBED_COLOR = 0x23D8FF
NOT_IN_ROOM_MESSAGE = "This panel only works inside its own Yap room."


def build_panel_embed(
    owner: discord.Member,
    *,
    locked: bool = False,
    hidden: bool = False,
    permitted: tuple[discord.Member, ...] = (),
) -> discord.Embed:
    embed = discord.Embed(
        title="Yap Room Controls",
        description=(
            f"{owner.mention} owns this room.\n"
            "Use the buttons below to manage it — no commands to remember."
        ),
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(
        name="State",
        value=(
            ("\U0001f512 Locked" if locked else "\U0001f513 Unlocked")
            + " / "
            + ("\U0001f648 Hidden" if hidden else "\U0001f441 Visible")
        ),
        inline=True,
    )
    if permitted:
        shown = ", ".join(member.mention for member in permitted[:10])
        if len(permitted) > 10:
            shown += f" +{len(permitted) - 10} more"
        embed.add_field(name="Permitted", value=shown, inline=True)
    embed.add_field(name="\U0001f512 Lock / \U0001f513 Unlock", value="Control who can join.", inline=True)
    embed.add_field(name="\U0001f648 Hide / \U0001f441 Unhide", value="Control who can see it.", inline=True)
    embed.add_field(name="✏️ Rename / \U0001f522 Limit", value="Customize your room.", inline=True)
    embed.add_field(
        name="\U0001f451 Transfer / \U0001f64b Claim / \U0001f462 Kick",
        value="Manage membership.",
        inline=True,
    )
    embed.add_field(
        name="\U0001f39f Permit / \U0001f6ab Block",
        value="Give or deny standing access.",
        inline=True,
    )
    embed.set_footer(text="YapHub")
    return embed


class RenameModal(ui.Modal, title="Rename your Yap room"):
    name = ui.TextInput(label="New name", max_length=100, min_length=1)

    def __init__(self, channel_id: int) -> None:
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        channel = await resolve_owned_temp_channel_by_id(interaction, bot.storage, self.channel_id)
        if channel is None:
            return
        await apply_rename(interaction, channel, self.name.value.strip())


class LimitModal(ui.Modal, title="Set Yap room limit"):
    limit = ui.TextInput(label="Limit (0-99, 0 = unlimited)", max_length=2, min_length=1)

    def __init__(self, channel_id: int) -> None:
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.limit.value.strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 99:
            await interaction.response.send_message(
                "Limit must be a whole number between 0 and 99.",
                ephemeral=True,
            )
            return

        bot = interaction.client
        channel = await resolve_owned_temp_channel_by_id(interaction, bot.storage, self.channel_id)
        if channel is None:
            return
        await apply_limit(interaction, channel, int(raw))


class TransferSelect(ui.UserSelect):
    def __init__(self, channel_id: int) -> None:
        super().__init__(placeholder="Choose a member to transfer ownership to", min_values=1, max_values=1)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        channel = await resolve_owned_temp_channel_by_id(interaction, bot.storage, self.channel_id)
        if channel is None:
            return

        target = self.values[0]
        if not isinstance(target, discord.Member) or target not in channel.members:
            await interaction.response.send_message(
                "Transfer target must be in your Yap room.",
                ephemeral=True,
            )
            return
        await apply_transfer(bot, interaction, channel, target)


class TransferView(ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=60)
        self.add_item(TransferSelect(channel_id))


class KickSelect(ui.UserSelect):
    def __init__(self, channel_id: int) -> None:
        super().__init__(placeholder="Choose a member to remove", min_values=1, max_values=1)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        channel = await resolve_owned_temp_channel_by_id(interaction, bot.storage, self.channel_id)
        if channel is None:
            return

        target = self.values[0]
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Pick a server member.", ephemeral=True)
            return
        await apply_kick(bot, interaction, channel, target)


class KickView(ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=60)
        self.add_item(KickSelect(channel_id))


class PermitSelect(ui.UserSelect):
    def __init__(self, channel_id: int) -> None:
        super().__init__(placeholder="Choose a member to permit", min_values=1, max_values=1)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        channel = await resolve_owned_temp_channel_by_id(interaction, bot.storage, self.channel_id)
        if channel is None:
            return

        target = self.values[0]
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Pick a server member.", ephemeral=True)
            return
        await apply_permit(bot, interaction, channel, target)


class PermitView(ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=60)
        self.add_item(PermitSelect(channel_id))


class BlockSelect(ui.UserSelect):
    def __init__(self, channel_id: int) -> None:
        super().__init__(placeholder="Choose a member to block", min_values=1, max_values=1)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        channel = await resolve_owned_temp_channel_by_id(interaction, bot.storage, self.channel_id)
        if channel is None:
            return

        target = self.values[0]
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Pick a server member.", ephemeral=True)
            return
        await apply_block(bot, interaction, channel, target)


class BlockView(ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=60)
        self.add_item(BlockSelect(channel_id))


class RoomControlPanel(ui.View):
    """Persistent control panel posted into each temp room's text chat.

    custom_ids are static (not per-channel) because the panel is registered
    once at startup via bot.add_view(); the target room is always resolved
    from interaction.channel, i.e. wherever the panel message lives.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _resolve(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        bot = interaction.client
        if not isinstance(interaction.channel, discord.VoiceChannel):
            await interaction.response.send_message(NOT_IN_ROOM_MESSAGE, ephemeral=True)
            return None
        return await resolve_owned_temp_channel_by_id(interaction, bot.storage, interaction.channel.id)

    @ui.button(label="Lock", emoji="\U0001f512", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:lock", row=0)
    async def lock_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        bot = interaction.client
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await apply_lock(bot, interaction, channel)

    @ui.button(label="Unlock", emoji="\U0001f513", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:unlock", row=0)
    async def unlock_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        bot = interaction.client
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await apply_unlock(bot, interaction, channel)

    @ui.button(label="Hide", emoji="\U0001f648", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:hide", row=0)
    async def hide_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        bot = interaction.client
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await apply_hide(bot, interaction, channel)

    @ui.button(label="Unhide", emoji="\U0001f441", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:unhide", row=0)
    async def unhide_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        bot = interaction.client
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await apply_unhide(bot, interaction, channel)

    @ui.button(label="Claim", emoji="\U0001f64b", style=discord.ButtonStyle.primary, custom_id="yaphub_panel:claim", row=0)
    async def claim_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        bot = interaction.client
        if not isinstance(interaction.channel, discord.VoiceChannel):
            await interaction.response.send_message(NOT_IN_ROOM_MESSAGE, ephemeral=True)
            return
        await apply_claim(bot, interaction, interaction.channel)

    @ui.button(label="Rename", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:rename", row=1)
    async def rename_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await interaction.response.send_modal(RenameModal(channel.id))

    @ui.button(label="Limit", emoji="\U0001f522", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:limit", row=1)
    async def limit_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await interaction.response.send_modal(LimitModal(channel.id))

    @ui.button(label="Transfer", emoji="\U0001f451", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:transfer", row=1)
    async def transfer_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            "Choose who to transfer ownership to:",
            view=TransferView(channel.id),
            ephemeral=True,
        )

    @ui.button(label="Permit", emoji="\U0001f39f", style=discord.ButtonStyle.secondary, custom_id="yaphub_panel:permit", row=1)
    async def permit_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            "Choose who to permit (they keep access to this room even while it is "
            "hidden or locked, until unpermitted or the room closes):",
            view=PermitView(channel.id),
            ephemeral=True,
        )

    @ui.button(label="Kick", emoji="\U0001f462", style=discord.ButtonStyle.danger, custom_id="yaphub_panel:kick", row=1)
    async def kick_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            "Choose who to remove from your room:",
            view=KickView(channel.id),
            ephemeral=True,
        )

    @ui.button(label="Block", emoji="\U0001f6ab", style=discord.ButtonStyle.danger, custom_id="yaphub_panel:block", row=2)
    async def block_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        channel = await self._resolve(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            "Choose who to block (they lose all access to this room until unblocked):",
            view=BlockView(channel.id),
            ephemeral=True,
        )


async def send_room_panel(
    channel: discord.VoiceChannel,
    owner: discord.Member,
    *,
    locked: bool = False,
    hidden: bool = False,
    permitted: tuple[discord.Member, ...] = (),
) -> discord.Message | None:
    try:
        embed = build_panel_embed(owner, locked=locked, hidden=hidden, permitted=permitted)
        return await channel.send(embed=embed, view=RoomControlPanel())
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to post control panel in channel %s", channel.id)
        return None


async def refresh_panel_message(bot, channel: discord.VoiceChannel) -> None:
    """Best-effort: re-render the panel embed (owner, lock/hide state,
    permit list) so it never goes stale after an action changes any of
    them. Never raises -- a missing/deleted message or permission issue
    must not break the caller's response. Owner and state are always
    re-derived here rather than passed in, so every caller gets the
    actual current state regardless of what triggered the refresh."""
    record = await bot.storage.get_active_temp_channel(channel.id)
    if record is None or record["panel_message_id"] is None:
        return

    owner = channel.guild.get_member(int(record["owner_user_id"]))
    if owner is None:
        return

    try:
        message = await channel.fetch_message(int(record["panel_message_id"]))
        embed = build_panel_embed(
            owner,
            locked=is_locked(channel),
            hidden=is_hidden(channel),
            permitted=await permitted_members(bot, channel.guild, channel.id),
        )
        await message.edit(embed=embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        logger.exception("Failed to refresh panel message for channel %s", channel.id)
