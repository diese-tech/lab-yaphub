import asyncio
import datetime
import logging
import os
from collections.abc import Mapping
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from commands import YapGroup
from config import (
    DATABASE_PATH,
    RECONCILE_INTERVAL_MINUTES,
    STATS_REFRESH_HOUR_ET,
    STATS_REFRESH_MINUTE_ET,
    STATS_SERVER_HOST,
    STATS_SERVER_PORT,
)
from services.panel import RoomControlPanel
from services.public_stats import refresh_public_stats_snapshot
from services.stats_server import start_stats_server
from services.telemetry import backfill_known_guilds
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
        # message_content stays off deliberately. YapHub reads no message
        # content: every interaction is a slash command, a component, or a
        # voice-state event. See `command_prefix` below.

        super().__init__(
            # YapHub registers zero prefix commands. Keeping a literal prefix
            # ("!") made discord.py warn on every boot that the privileged
            # message content intent was missing -- for a command surface that
            # does not exist. `when_mentioned` is the one prefix discord.py
            # accepts without that warning, so the log stays honest without
            # requesting a privileged intent YapHub has no use for.
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=intents,
        )

        self.storage = Storage(DATABASE_PATH)
        self.profile_cache: dict[int, Mapping[str, object]] = {}
        self.active_temp_channel_ids: set[int] = set()
        self.notification_cooldowns: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> [Lock, refcount]; entries are evicted by
        # create_temp_room once the last holder releases, so the dict does
        # not grow unboundedly over the process lifetime.
        self.user_creation_locks: dict[tuple[int, int], list] = {}
        # Reconciliation runs from two places (startup and the periodic
        # loop) and rewrites active_temp_channel_ids wholesale. Overlapping
        # passes would race each other into deleting the same room twice and
        # into clobbering each other's view of what is tracked.
        self.reconcile_lock = asyncio.Lock()
        self.started_once = False
        self.stats_server_runner: object | None = None
        # The one thing services/stats_server.py's request handler reads.
        # A plain in-process attribute, never a storage call, specifically
        # so a flood of public HTTP requests cannot queue work onto the
        # same asyncio.to_thread executor real Discord operations share.
        # refresh_public_stats_snapshot is the only writer after startup;
        # setup_hook below does the one-time warm read from durable
        # storage so a restart doesn't 503 while waiting for the first
        # daily refresh.
        self.public_stats_cache: str | None = None

    async def setup_hook(self) -> None:
        await self.storage.initialize()
        self.tree.add_command(YapGroup(self))
        self.add_view(RoomControlPanel())

        try:
            row = await self.storage.get_public_stats_snapshot()
            if row is not None:
                self.public_stats_cache = row["payload"]
        except Exception:
            logger.exception(
                "Failed to warm the public stats cache from storage; "
                "the stats endpoint will 503 until the next refresh"
            )

        try:
            self.stats_server_runner = await start_stats_server(
                self, STATS_SERVER_HOST, STATS_SERVER_PORT
            )
        except Exception:
            # The public stats endpoint is supplementary -- a bound-port
            # conflict or a container without public networking must not
            # stop the bot from logging into Discord and doing its actual
            # job.
            logger.exception(
                "Failed to start the public stats server on port %s; "
                "continuing without it",
                STATS_SERVER_PORT,
            )

    async def close(self) -> None:
        if self.stats_server_runner is not None:
            await self.stats_server_runner.cleanup()
        await super().close()

    async def load_runtime_cache(self) -> None:
        self.profile_cache = {
            int(profile["join_channel_id"]): profile
            for profile in await self.storage.list_all_profiles()
        }
        self.active_temp_channel_ids = await runtime_active_channel_ids(self)

    async def reconcile_active_temp_channels(self) -> None:
        async with self.reconcile_lock:
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
        try:
            await bot.reconcile_active_temp_channels()
        except Exception:
            # A failed first pass must not stop the periodic loop from
            # starting; otherwise one bad startup leaves the process with no
            # reconciliation at all until it is redeployed.
            logger.exception("Startup reconcile failed; the periodic loop will retry")
        if not reconcile_loop.is_running():
            reconcile_loop.start()

        # Fold every already-configured guild into the pseudonymous telemetry
        # set before building the snapshot, so servers_served reflects real
        # usage from the moment YAPHUB_ANALYTICS_SECRET is turned on -- not
        # just guilds that happen to create a fresh room afterward. Both
        # calls are already best-effort internally (see services/
        # telemetry.py and services/public_stats.py), so no try/except is
        # needed here -- unlike reconcile, a failure has no periodic-loop
        # startup to guard.
        await backfill_known_guilds(bot)
        await refresh_public_stats_snapshot(bot)
        if not stats_refresh_loop.is_running():
            stats_refresh_loop.start()

        bot.started_once = True

    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


@tasks.loop(minutes=RECONCILE_INTERVAL_MINUTES)
async def reconcile_loop() -> None:
    await bot.wait_until_ready()
    try:
        await bot.reconcile_active_temp_channels()
    except Exception:
        # discord.ext.tasks stops a loop that raises. Reconciliation is the
        # recovery path for rooms whose cleanup already failed once, so it
        # must survive a bad pass and try again on the next tick.
        logger.exception("Reconcile pass failed; the loop will retry on the next tick")


@reconcile_loop.error
async def on_reconcile_loop_error(error: BaseException) -> None:
    """Last resort: restart the loop if it ever does stop.

    The body above swallows its own failures, so this only fires for a
    failure raised outside it (for example wait_until_ready during a
    shutdown race). Without it the bot keeps running with reconciliation
    permanently dead and no orphan ever gets cleaned up again.
    """
    logger.exception("Reconcile loop stopped unexpectedly; restarting", exc_info=error)
    if not reconcile_loop.is_running():
        reconcile_loop.restart()


@reconcile_loop.before_loop
async def before_reconcile_loop() -> None:
    await bot.wait_until_ready()


@tasks.loop(
    time=datetime.time(
        hour=STATS_REFRESH_HOUR_ET,
        minute=STATS_REFRESH_MINUTE_ET,
        tzinfo=ZoneInfo("America/New_York"),
    )
)
async def stats_refresh_loop() -> None:
    await bot.wait_until_ready()
    # refresh_public_stats_snapshot never raises (see services/
    # public_stats.py) -- no try/except needed to keep this loop alive.
    await refresh_public_stats_snapshot(bot)


@stats_refresh_loop.before_loop
async def before_stats_refresh_loop() -> None:
    await bot.wait_until_ready()


@stats_refresh_loop.error
async def on_stats_refresh_loop_error(error: BaseException) -> None:
    """Same rationale as on_reconcile_loop_error: the body above cannot
    raise on its own, but a failure outside it (e.g. wait_until_ready
    during a shutdown race) would otherwise leave the daily refresh
    permanently dead for the rest of the process's life."""
    logger.exception("Stats refresh loop stopped unexpectedly; restarting", exc_info=error)
    if not stats_refresh_loop.is_running():
        stats_refresh_loop.restart()


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return

    before_channel = before.channel
    after_channel = after.channel

    # Discord fires this event for mute, deafen, camera and streaming changes
    # too, where the member never moved. Treating those as a join re-ran
    # room creation for anyone parked in a lobby -- which is how a single
    # failed move amplified into a screen full of duplicate rooms -- and
    # treating them as a departure revoked a still-present member's access to
    # a locked or hidden room. Only an actual channel change is a move.
    if before_channel is not None and before_channel == after_channel:
        return

    if before_channel is not None and before_channel.id in bot.active_temp_channel_ids:
        try:
            await bot.cleanup_temp_channel(before_channel, leaver=member)
        except Exception:
            logger.exception(
                "voice_state cleanup_failed guild=%s member=%s channel=%s",
                member.guild.id,
                member.id,
                before_channel.id,
            )

    if after_channel is not None and after_channel.id in bot.profile_cache:
        profile = bot.profile_cache[after_channel.id]
        try:
            await bot.create_temp_room(member, after_channel, profile)
        except Exception:
            # Contained so one member's failure cannot take down the handler
            # for everyone else. create_temp_room already rolls back or
            # preserves tracking for every Discord failure it models; this
            # catches the unmodelled ones and keeps them diagnosable.
            logger.exception(
                "voice_state create_failed guild=%s member=%s lobby=%s",
                member.guild.id,
                member.id,
                after_channel.id,
            )


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
