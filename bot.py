import logging
import os
from collections.abc import Mapping

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from commands import YapGroup
from config import DATABASE_PATH, RECONCILE_INTERVAL_MINUTES
from services.panel import RoomControlPanel
from services.temp_channels import (
    cleanup_temp_channel,
    create_temp_room,
    reconcile_active_temp_channels,
    runtime_active_channel_ids,
)
from storage import Storage

load_dotenv(".env.local")
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("yaphub")

TOKEN = os.getenv("DISCORD_TOKEN")


class YapHubBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

        self.storage = Storage(DATABASE_PATH)
        self.profile_cache: dict[int, Mapping[str, object]] = {}
        self.active_temp_channel_ids: set[int] = set()
        self.notification_cooldowns: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> [Lock, refcount]; entries are evicted by
        # create_temp_room once the last holder releases, so the dict does
        # not grow unboundedly over the process lifetime.
        self.user_creation_locks: dict[tuple[int, int], list] = {}
        self.started_once = False

    async def setup_hook(self) -> None:
        await self.storage.initialize()
        self.tree.add_command(YapGroup(self))
        self.add_view(RoomControlPanel())

    async def load_runtime_cache(self) -> None:
        self.profile_cache = {
            int(profile["join_channel_id"]): profile
            for profile in await self.storage.list_all_profiles()
        }
        self.active_temp_channel_ids = await runtime_active_channel_ids(self)

    async def reconcile_active_temp_channels(self) -> None:
        await reconcile_active_temp_channels(self)

    async def create_temp_room(
        self,
        member: discord.Member,
        lobby_channel: discord.VoiceChannel,
        profile: Mapping[str, object],
    ) -> None:
        await create_temp_room(self, member, lobby_channel, profile)

    async def cleanup_temp_channel(
        self,
        channel: discord.VoiceChannel,
        leaver: discord.Member | None = None,
    ) -> None:
        await cleanup_temp_channel(self, channel, leaver)


bot = YapHubBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"You're doing that too fast. Try again in {error.retry_after:.0f} seconds."
    else:
        logger.error("Unhandled app command error", exc_info=error)
        message = "Something went wrong running that command. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to notify user about a command error")


@bot.event
async def on_ready() -> None:
    if not bot.started_once:
        await bot.tree.sync()
        await bot.load_runtime_cache()
        await bot.reconcile_active_temp_channels()
        if not reconcile_loop.is_running():
            reconcile_loop.start()
        bot.started_once = True

    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


@tasks.loop(minutes=RECONCILE_INTERVAL_MINUTES)
async def reconcile_loop() -> None:
    await bot.wait_until_ready()
    await bot.reconcile_active_temp_channels()


@reconcile_loop.before_loop
async def before_reconcile_loop() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return

    if before.channel and before.channel.id in bot.active_temp_channel_ids:
        await bot.cleanup_temp_channel(before.channel, leaver=member)

    if after.channel and after.channel.id in bot.profile_cache:
        profile = bot.profile_cache[after.channel.id]
        await bot.create_temp_room(member, after.channel, profile)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set.")


bot.run(TOKEN)
