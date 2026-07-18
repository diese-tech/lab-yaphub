import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from commands.owner_controls import (
    limit_temp_channel,
    lock_owned_temp_channel,
    rename_temp_channel,
    transfer_temp_channel,
    unlock_owned_temp_channel,
)
from commands.profiles import ProfileGroup
from config import JOIN_TO_CREATE_NAME
from services.confirm import ConfirmView
from services.permissions import require_manage_channels

if TYPE_CHECKING:
    from bot import YapHubBot

logger = logging.getLogger("yaphub")


class YapGroup(app_commands.Group):
    def __init__(self, bot: "YapHubBot") -> None:
        super().__init__(name="yap", description="YapHub temp VC controls")
        self.bot = bot
        self.add_command(ProfileGroup(bot))

    @app_commands.command(name="setup", description="Create a Join to Yap lobby in a category")
    async def setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        if not require_manage_channels(interaction):
            await interaction.response.send_message(
                "You need Manage Channels permission.",
                ephemeral=True,
            )
            return

        assert interaction.guild is not None
        profiles = await self.bot.storage.list_profiles(interaction.guild.id)
        category_id = category.id if category else None

        for profile in profiles:
            profile_category_id = (
                int(profile["target_category_id"]) if profile["target_category_id"] else None
            )
            if profile_category_id == category_id:
                lobby_channel = interaction.guild.get_channel(int(profile["join_channel_id"]))
                await interaction.response.send_message(
                    (
                        "YapHub is already set up for "
                        f"{category.mention if category else 'top-level voice channels'}.\n"
                        f"Lobby: {lobby_channel.mention if lobby_channel else profile['join_channel_id']}"
                    ),
                    ephemeral=True,
                )
                return

        profile_name = category.name if category else "Default"
        if await self.bot.storage.get_profile_by_name(interaction.guild.id, profile_name):
            profile_name = (
                f"{profile_name} {len(profiles) + 1}" if category else f"Default {len(profiles) + 1}"
            )

        try:
            lobby_channel = await interaction.guild.create_voice_channel(
                JOIN_TO_CREATE_NAME,
                category=category,
                reason="YapHub setup",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to create setup lobby in guild %s", interaction.guild.id)
            await interaction.response.send_message(
                "I could not create the Join to Yap lobby. Check my Manage Channels permission.",
                ephemeral=True,
            )
            return

        profile = await self.bot.storage.create_profile(
            guild_id=interaction.guild.id,
            name=profile_name,
            join_channel_id=lobby_channel.id,
            target_category_id=category_id,
            created_by_user_id=interaction.user.id,
        )
        self.bot.profile_cache[int(profile["join_channel_id"])] = profile

        await interaction.response.send_message(
            (
                f"Created YapHub setup for `{profile_name}`.\n"
                f"Lobby: {lobby_channel.mention}\n"
                f"Category: {category.name if category else 'Top level'}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="help", description="Show YapHub commands")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            (
                "**You usually won't need this.** Every Yap room gets a control panel "
                "with buttons (lock, unlock, rename, limit, transfer, claim, kick) posted "
                "in its own text chat — just click instead of remembering commands.\n\n"
                "**Setup**\n"
                "`/yap setup category:<category>` - Create a Join to Yap lobby in a category.\n"
                "`/yap config` - Show configured lobbies and active rooms.\n"
                "`/yap reset` - Remove YapHub setup for this server (asks for confirmation).\n\n"
                "**Room Controls**\n"
                "`/yap rename name:<name>` - Rename your current Yap room.\n"
                "`/yap limit count:<0-99>` - Set a user limit. Use `0` for unlimited.\n"
                "`/yap transfer user:<member>` - Transfer ownership to someone in the room.\n"
                "`/yap lock` / `/yap unlock` - Control new joins.\n\n"
                "`/yap profile create` is available for advanced/manual setups."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="config", description="Show the current YapHub configuration")
    async def config(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_config = await self.bot.storage.get_or_create_guild_config(interaction.guild.id)
        profiles = await self.bot.storage.list_profiles(interaction.guild.id)
        active_rooms = await self.bot.storage.list_active_temp_channels(interaction.guild.id)

        lines = [
            f"Temp prefix: `{guild_config['temp_channel_prefix']}`",
            f"Duplicate-room cooldown: `{guild_config['notification_cooldown_seconds']}s`",
            f"Profiles: `{len(profiles)}`",
            f"Active temp rooms: `{len(active_rooms)}`",
        ]

        if profiles:
            lines.append("")
            lines.append("Configured profiles:")
            for profile in profiles:
                lobby_channel = interaction.guild.get_channel(int(profile["join_channel_id"]))
                category_name = "Top level"
                if profile["target_category_id"]:
                    category = interaction.guild.get_channel(int(profile["target_category_id"]))
                    if isinstance(category, discord.CategoryChannel):
                        category_name = category.name

                lines.append(
                    (
                        f"- {profile['name']}: "
                        f"{lobby_channel.mention if lobby_channel else profile['join_channel_id']} "
                        f"-> {category_name}"
                    )
                )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="reset", description="Remove all Join to Yap profiles for this server")
    async def reset(self, interaction: discord.Interaction) -> None:
        if not require_manage_channels(interaction):
            await interaction.response.send_message(
                "You need Manage Channels permission.",
                ephemeral=True,
            )
            return

        assert interaction.guild is not None
        guild = interaction.guild
        profiles = await self.bot.storage.list_profiles(guild.id)

        if not profiles:
            await interaction.response.send_message(
                "There is nothing configured to reset for this server.",
                ephemeral=True,
            )
            return

        async def do_reset(confirm_interaction: discord.Interaction) -> None:
            deleted_channels = 0
            for profile in profiles:
                join_channel_id = int(profile["join_channel_id"])
                join_channel = guild.get_channel(join_channel_id)
                if isinstance(join_channel, discord.VoiceChannel):
                    try:
                        await join_channel.delete(reason="YapHub reset")
                        deleted_channels += 1
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception(
                            "Failed to delete lobby channel %s during reset", join_channel_id
                        )
                self.bot.profile_cache.pop(join_channel_id, None)

            await self.bot.storage.reset_guild_configuration(guild.id)

            await confirm_interaction.followup.send(
                (
                    f"Reset YapHub for this server.\n"
                    f"Deleted lobby channels: `{deleted_channels}`\n"
                    "Existing active temp rooms will continue to be tracked until they empty."
                ),
                ephemeral=True,
            )

        view = ConfirmView(author_id=interaction.user.id, on_confirm=do_reset)
        await interaction.response.send_message(
            (
                f"This removes all `{len(profiles)}` configured profile(s) and their lobby "
                "channels for this server. This cannot be undone."
            ),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="rename", description="Rename your active Yap room")
    async def rename(self, interaction: discord.Interaction, name: str) -> None:
        await rename_temp_channel(self.bot, interaction, name)

    @app_commands.command(name="limit", description="Set the user limit for your active Yap room")
    async def limit(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 0, 99],
    ) -> None:
        await limit_temp_channel(self.bot, interaction, count)

    @app_commands.command(name="transfer", description="Transfer your active Yap room to another member")
    async def transfer(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await transfer_temp_channel(self.bot, interaction, user)

    @app_commands.command(name="lock", description="Lock your active Yap room")
    async def lock(self, interaction: discord.Interaction) -> None:
        await lock_owned_temp_channel(self.bot, interaction)

    @app_commands.command(name="unlock", description="Unlock your active Yap room")
    async def unlock(self, interaction: discord.Interaction) -> None:
        await unlock_owned_temp_channel(self.bot, interaction)
