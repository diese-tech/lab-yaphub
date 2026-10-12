"""Explicit staff attribution for mutations with an unknown result."""

import logging

import discord
from discord import app_commands

from services.permissions import require_manage_channels

logger = logging.getLogger("yaphub")


class RecoveryGroup(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="recovery", description="Inspect and resolve uncertain room creation")
        self.bot = bot

    @app_commands.command(name="list", description="List creation intents needing staff verification")
    async def list_intents(self, interaction: discord.Interaction):
        if not require_manage_channels(interaction):
            await interaction.response.send_message("Manage Channels permission is required.", ephemeral=True)
            return
        rows = [r for r in await self.bot.lifecycle_recovery.store.rows("creation_intents")
                if r["guild_id"] == str(interaction.guild.id) and r["phase"] == "unresolved"]
        lines = [f"Intent `{r['id']}` - owner <@{r['user_id']}>" for r in rows[:20]]
        await interaction.response.send_message("\n".join(lines) or "No unresolved creation intents.", ephemeral=True)

    @app_commands.command(name="resolve", description="Attach a staff-verified room or confirm verified absence")
    @app_commands.describe(verified="You verified the exact intent in audit/response evidence, not a room name",
                           channel="Exact room identified by the evidence; omit only after verifying no room exists")
    async def resolve(self, interaction: discord.Interaction, intent_id: str, verified: bool,
                      channel: discord.VoiceChannel | None = None):
        if not require_manage_channels(interaction) or not verified:
            await interaction.response.send_message("Manage Channels and explicit verification are required.", ephemeral=True)
            return
        intent = next((r for r in await self.bot.lifecycle_recovery.store.rows("creation_intents")
                       if r["id"] == intent_id and r["guild_id"] == str(interaction.guild.id)
                       and r["phase"] == "unresolved"), None)
        if intent is None or (channel and channel.guild.id != interaction.guild.id):
            await interaction.response.send_message("No matching unresolved intent in this server.", ephemeral=True)
            return
        if channel and channel.id in self.bot.profile_cache:
            await interaction.response.send_message("A configured lobby cannot be attached as a recovered room.", ephemeral=True)
            return
        if str(intent["guild_id"]) in self.bot.lifecycle_recovery.busy:
            await interaction.response.send_message("Recovery is currently processing this server. Try again shortly.", ephemeral=True)
            return
        self.bot.lifecycle_recovery.busy.add(intent["guild_id"])
        try:
            if channel:
                await self.bot.lifecycle_recovery.store.attach(intent, channel.id)
                self.bot.active_temp_channel_ids.add(channel.id)
            else:
                await self.bot.lifecycle_recovery.store.phase(intent_id, "cancelled")
            logger.warning("recovery staff_verified intent=%s channel=%s actor=%s", intent_id, getattr(channel, "id", None), interaction.user.id)
        finally:
            self.bot.lifecycle_recovery.busy.discard(intent["guild_id"])
        await self.bot.lifecycle_recovery.refresh_counts()
        await interaction.response.send_message("Verification recorded. Waiting recovery work will resume safely.", ephemeral=True)
