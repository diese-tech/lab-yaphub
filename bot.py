import asyncio
import logging
import os
from collections.abc import Mapping

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from commands import YapGroup
from config import (
    DATABASE_PATH,
    RECONCILE_INTERVAL_MINUTES,
    STATS_REFRESH_INTERVAL_HOURS,
    STATS_SERVER_HOST,
    STATS_SERVER_PORT,
)
from services.panel import RoomControlPanel
from services.create_backoff import CreateBackoff
from services.login_backoff import login_with_backoff
from services.api_recovery import ApiDeferred, ApiRecovery, RecoveryHTTPClient, status_json
from services.lifecycle_recovery import LifecycleRecovery
from services.diagnostics import log_interaction_error
from services.public_stats import refresh_public_stats_snapshot
from services.stats_server import start_stats_server
from services.telemetry import backfill_known_guilds
from services.temp_channels import (
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
        self.api_recovery = ApiRecovery(self.storage)
        self.http = RecoveryHTTPClient(self.http, self.api_recovery)
        self._connection.http = self.http
        # CommandTree captures the original HTTP client in Bot.__init__.
        # Bind it to the same guarded, logged-in session as gateway state.
        # This private integration is exercised against pinned discord.py.
        self.tree._http = self.http
        self.runtime_initialized = False
        self.status_cache = None
        self.status_task = None
        self.command_sync_pending = False
        self.lifecycle_recovery = LifecycleRecovery(self)
        self.profile_cache: dict[int, Mapping[str, object]] = {}
        self.active_temp_channel_ids: set[int] = set()
        self.notification_cooldowns: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> [Lock, refcount]; entries are evicted by
        # create_temp_room once the last holder releases, so the dict does
        # not grow unboundedly over the process lifetime.
        self.user_creation_locks: dict[tuple[int, int], list] = {}
        self.room_create_backoff = CreateBackoff()
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
        # scheduled refresh.
        self.public_stats_cache: str | None = None

    async def login(self, token: str) -> None:
        if not self.api_recovery.initialized:
            self.status_cache = status_json(self)
            try:
                self.stats_server_runner = await start_stats_server(self, STATS_SERVER_HOST, STATS_SERVER_PORT)
            except OSError:
                logger.exception("Failed to start the public stats server; continuing Discord recovery")
            self.refresh_status()
            await self.api_recovery.initialize()
            await self.load_runtime_cache()
            await self.lifecycle_recovery.initialize()

        async def reset_http() -> None:
            await self.http.close()
            # Closing the session also closes its owned connector. Let
            # static_login construct a fresh connector on the next attempt.
            self.http.connector = discord.utils.MISSING

        await login_with_backoff(super().login, reset_http, token)

    def refresh_status(self):
        self.status_cache = status_json(self)
        self.status_task = asyncio.get_running_loop().call_later(1, self.refresh_status)

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
            if self.stats_server_runner is None:
                self.stats_server_runner = await start_stats_server(self, STATS_SERVER_HOST, STATS_SERVER_PORT)
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
        if getattr(self, "lifecycle_recovery", None) is not None:
            await self.lifecycle_recovery.close()
        if getattr(self, "status_task", None) is not None:
            self.status_task.cancel()
        if self.stats_server_runner is not None:
            await self.stats_server_runner.cleanup()
        await super().close()

    async def load_runtime_cache(self) -> None:
        self.profile_cache = {
            int(profile["join_channel_id"]): profile
            for profile in await self.storage.list_all_profiles()
        }
        self.active_temp_channel_ids = await runtime_active_channel_ids(self)
        valid_profiles = {str(p["id"]) for p in self.profile_cache.values()}
        for row in await self.lifecycle_recovery.store.rows("pending_joins"):
            if row["profile_id"] not in valid_profiles:
                await self.lifecycle_recovery.store.cancel(row["guild_id"], row["user_id"])

    async def reconcile_active_temp_channels(self) -> None:
        async with self.reconcile_lock:
            await self.lifecycle_recovery.schedule_reconcile()

    async def create_temp_room(
        self,
        member: discord.Member,
        lobby_channel: discord.VoiceChannel,
        profile: Mapping[str, object],
    ) -> None:
        await self.lifecycle_recovery.enqueue(member, lobby_channel, profile)

    async def cleanup_temp_channel(
        self,
        channel: discord.VoiceChannel,
        leaver: discord.Member | None = None,
    ) -> None:
        await self.lifecycle_recovery.store.cleanup(channel.guild.id, channel.id, getattr(leaver, "id", 0))


bot = YapHubBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"You're doing that too fast. Try again in {error.retry_after:.0f} seconds."
    elif isinstance(getattr(error, "original", error), ApiDeferred):
        message = "Discord is temporarily unavailable. Please try again after service recovers."
    else:
        log_interaction_error("app_command_failed", interaction, error)
        logger.error("Unhandled app command error", exc_info=error)
        message = "Something went wrong running that command. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except ApiDeferred:
        return
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to notify user about a command error")


@bot.event
async def on_ready() -> None:
    if not bot.started_once:
        await bot.load_runtime_cache()
        try:
            await bot.tree.sync()
        except ApiDeferred:
            bot.command_sync_pending = True
            logger.info("startup command_sync_deferred")
        except discord.HTTPException:
            bot.command_sync_pending = True
            logger.exception("Startup command sync failed; room workers will start and sync will retry")
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
        bot.runtime_initialized = True

    await bot.lifecycle_recovery.scan()
    bot.lifecycle_recovery.start()
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


@tasks.loop(minutes=RECONCILE_INTERVAL_MINUTES)
async def reconcile_loop() -> None:
    await bot.wait_until_ready()
    try:
        if bot.command_sync_pending:
            try:
                await bot.tree.sync()
                bot.command_sync_pending = False
            except ApiDeferred:
                pass
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


@tasks.loop(hours=STATS_REFRESH_INTERVAL_HOURS)
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
    during a shutdown race) would otherwise leave the refresh loop
    permanently dead for the rest of the process's life."""
    logger.exception("Stats refresh loop stopped unexpectedly; restarting", exc_info=error)
    if not stats_refresh_loop.is_running():
        stats_refresh_loop.restart()


@bot.event
async def on_member_remove(member):
    await bot.lifecycle_recovery.cancel_member_removal(member)


@bot.event
async def on_guild_remove(guild):
    for row in await bot.lifecycle_recovery.store.rows("pending_joins"):
        if row["guild_id"] == str(guild.id):
            await bot.lifecycle_recovery.store.cancel(guild.id, row["user_id"])


@bot.event
async def on_guild_channel_delete(channel):
    if channel.id in bot.active_temp_channel_ids:
        row = await bot.storage.get_active_temp_channel(channel.id)
        if row is not None and int(row["guild_id"]) == channel.guild.id:
            await bot.storage.delete_active_temp_channel(channel.id)
            bot.active_temp_channel_ids.discard(channel.id)
            await bot.lifecycle_recovery.store.deleted(channel.id)
            logger.info("temp_room gateway_deletion_confirmed channel=%s", channel.id)


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

    if before_channel is not None:
        await bot.lifecycle_recovery.cancel_departure(member, after_channel)

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
        except ApiDeferred:
            logger.info("voice_state join_deferred")
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
