"""Fair, durable join recovery with one lifecycle operation per guild."""

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime

import discord

from config import DEFAULT_TEMP_CHANNEL_PREFIX
from services.api_recovery import AmbiguousMutation, ApiDeferred
from services.notifications import notify_duplicate_room
from services.panel import send_room_panel
from services.permissions import missing_room_permissions
from services.recovery_store import RecoveryStore
from services.telemetry import pseudonymous_guild_key, pseudonymous_user_key
from services.temp_channels import (
    _fetch_guild_channel, _reconcile_row, build_temp_channel_name, cleanup_temp_channel,
)

logger = logging.getLogger("yaphub")


class LifecycleRecovery:
    def __init__(self, bot):
        self.bot = bot
        self.store = RecoveryStore(bot.storage)
        self.busy = set()
        self.rotation = deque()
        self.tasks = []
        self.overflow = 0
        self.last_sweep = 0
        self.wait_seconds = 0.0
        self.audit_checked = set()
        self.join_turns = {}

    async def initialize(self):
        await self.store.restore()
        intents = await self.store.rows("creation_intents")
        complete = [i["completed"] for i in intents if i["phase"] == "completed" and i["completed"] is not None]
        if complete:
            self.bot.last_room_success = max(complete)
        await self.refresh_counts()

    async def refresh_counts(self):
        self.bot.pending_join_count = len(await self.store.rows("pending_joins"))
        self.bot.pending_cleanup_count = len(await self.store.rows("pending_cleanup"))
        self.bot.unresolved_creation_count = sum(i["phase"] == "unresolved" for i in await self.store.rows("creation_intents"))
        self.bot.queue_overflow_count = self.overflow
        self.bot.queue_wait_seconds = self.wait_seconds

    async def enqueue(self, member, lobby, profile):
        accepted = await self.store.enqueue(member.guild.id, member.id, lobby.id, str(profile["id"]))
        if not accepted:
            self.overflow += 1
            logger.error("recovery queue_overflow guild=%s", member.guild.id)
        await self.refresh_counts()
        return accepted

    async def cancel_departure(self, member, after):
        # Our successful move must not erase panel work before it completes.
        intents = await self.store.rows("creation_intents")
        moving = any(i["guild_id"] == str(member.guild.id) and i["user_id"] == str(member.id)
                     and i["channel_id"] == str(getattr(after, "id", None))
                     and i["phase"] in {"created", "moved"} for i in intents)
        if not moving:
            await self.store.cancel(member.guild.id, member.id)

    async def cancel_member_removal(self, member):
        await self.store.cancel(member.guild.id, member.id)
        for intent in await self.store.rows("creation_intents"):
            if (intent["guild_id"], intent["user_id"]) == (str(member.guild.id), str(member.id)):
                if intent["phase"] in {"prepared", "created", "moved"}:
                    await self.store.phase(intent["id"], "cancelled")
                    if intent["channel_id"]:
                        await self.store.cleanup(member.guild.id, intent["channel_id"])

    async def scan(self):
        if not self.bot.is_ready():
            return
        for profile in await self.bot.storage.list_all_profiles():
            guild = self.bot.get_guild(int(profile["guild_id"]))
            if guild is None or getattr(guild, "unavailable", False):
                continue
            lobby = guild.get_channel(int(profile["join_channel_id"]))
            if isinstance(lobby, discord.VoiceChannel):
                for member in list(lobby.members):
                    if not member.bot:
                        await self.enqueue(member, lobby, profile)
        # Abandoned created rooms must become cleanup work after a restart.
        pending = {(r["guild_id"], r["user_id"]) for r in await self.store.rows("pending_joins")}
        for intent in await self.store.rows("creation_intents"):
            if intent["phase"] not in {"prepared", "created", "moved"}:
                continue
            if (intent["guild_id"], intent["user_id"]) in pending:
                continue
            guild = self.bot.get_guild(int(intent["guild_id"]))
            if guild is None or getattr(guild, "unavailable", False):
                continue
            if intent["phase"] == "prepared":
                await self.store.phase(intent["id"], "cancelled")
            elif intent["channel_id"]:
                member = guild.get_member(int(intent["user_id"]))
                if member is None:
                    continue
                voice = getattr(member, "voice", None)
                if voice and voice.channel and voice.channel.id == int(intent["channel_id"]):
                    await self.store.enqueue(int(intent["guild_id"]), int(intent["user_id"]),
                                             int(intent["lobby_id"]), intent["profile_id"])
                else:
                    await self.store.phase(intent["id"], "cancelled")
                    await self.store.cleanup(intent["guild_id"], intent["channel_id"])
        await self.schedule_reconcile()

    async def schedule_reconcile(self):
        for row in await self.bot.storage.list_active_temp_channels():
            await self.store.cleanup(row["guild_id"], row["channel_id"])
        await self.refresh_counts()

    def start(self):
        if not self.tasks:
            # One worker is exclusively reserved for cleanup.
            self.tasks = [asyncio.create_task(self.worker(False)) for _ in range(3)]
            self.tasks.append(asyncio.create_task(self.worker(True)))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    def choose(self, rows):
        eligible = [r for r in rows if r["due"] <= time.time() and r["guild_id"] not in self.busy]
        guilds = {r["guild_id"] for r in eligible}
        for guild in sorted(guilds):
            if guild not in self.rotation:
                self.rotation.append(guild)
        for _ in range(len(self.rotation)):
            guild = self.rotation.popleft()
            self.rotation.append(guild)
            if guild in guilds:
                self.busy.add(guild)
                return next(r for r in eligible if r["guild_id"] == guild)
        return None

    async def tick(self, cleanup=False):
        if not self.bot.is_ready():
            return False
        rows = await self.store.rows("pending_cleanup" if cleanup else "pending_joins")
        if not cleanup:
            cleanup_guilds = {r["guild_id"] for r in await self.store.rows("pending_cleanup") if r["due"] <= time.time()}
            rows = [r for r in rows if self.join_turns.get(r["guild_id"], 0) < 3 or r["guild_id"] not in cleanup_guilds]
        row = self.choose(rows)
        if row is None:
            return False
        try:
            if cleanup:
                self.join_turns[row["guild_id"]] = 0
                await self.run_cleanup(row)
            else:
                self.join_turns[row["guild_id"]] = self.join_turns.get(row["guild_id"], 0) + 1
                await self.run_join(row)
        except ApiDeferred as error:
            if cleanup:
                await self.store.cleanup_result(row, error.retry_after)
            else:
                await self.store.defer(row, error.retry_after)
        except AmbiguousMutation:
            logger.exception("recovery creation_unresolved staff_verification_required=1")
            await self.store.defer(row, 60)
        except Exception:
            logger.exception("recovery lifecycle_retry_pending cleanup=%s", cleanup)
            if cleanup:
                await self.store.cleanup_result(row, 60)
            else:
                await self.store.defer(row, 60)
        finally:
            self.busy.discard(row["guild_id"])
            await self.refresh_counts()
        return True

    async def worker(self, cleanup):
        while True:
            try:
                if cleanup and time.monotonic() - self.last_sweep > 30:
                    self.last_sweep = time.monotonic()
                    await self.scan()
                await self.tick(cleanup)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recovery worker_retry_pending")
            await asyncio.sleep(1)

    async def abandon(self, row, intent=None):
        await self.store.cancel(row["guild_id"], row["user_id"], row["revision"])
        if intent and intent["phase"] not in {"unresolved", "sent"}:
            await self.store.phase(intent["id"], "cancelled")
            if intent["channel_id"]:
                await self.store.cleanup(row["guild_id"], intent["channel_id"])

    async def run_join(self, row):
        guild = self.bot.get_guild(int(row["guild_id"]))
        if guild is None or getattr(guild, "unavailable", False) or not self.bot.is_ready():
            raise ApiDeferred(5)
        member = guild.get_member(int(row["user_id"]))
        if member is None:
            # Incomplete member cache is not evidence of departure.
            raise ApiDeferred(5)
        intent = await self.store.intent(row)
        if intent["phase"] == "sent":
            await self.store.phase(intent["id"], "unresolved")
            intent["phase"] = "unresolved"
        if intent["phase"] == "unresolved":
            await self.recover_audit(guild, intent)
            raise ApiDeferred(60)
        profile = await self.bot.storage.get_profile(row["profile_id"])
        lobby = guild.get_channel(int(row["lobby_id"]))
        voice = getattr(member, "voice", None)
        current = getattr(voice, "channel", None)
        own_room = intent["channel_id"] and getattr(current, "id", None) == int(intent["channel_id"])
        if not await self.store.current(row):
            return
        if (profile is None or str(profile["guild_id"]) != row["guild_id"]
                or str(profile["join_channel_id"]) != row["lobby_id"]
                or not isinstance(lobby, discord.VoiceChannel)
                or (getattr(current, "id", None) != lobby.id and not own_room)):
            await self.abandon(row, intent)
            return
        if intent["lobby_id"] != row["lobby_id"] or intent["profile_id"] != row["profile_id"]:
            # A later lobby target invalidates unsent work. An existing room
            # must be cleaned first, never overwritten by another create.
            await self.store.phase(intent["id"], "cancelled")
            if intent["channel_id"]:
                await self.store.cleanup(row["guild_id"], intent["channel_id"])
                raise ApiDeferred(5)
            intent = await self.store.intent(row)
        category_id = profile["target_category_id"]
        category = guild.get_channel(int(category_id)) if category_id else lobby.category
        if category_id and not isinstance(category, discord.CategoryChannel):
            await self.abandon(row, intent)
            return
        if missing_room_permissions(guild, lobby, category):
            await self.abandon(row, intent)
            logger.error("recovery configuration_invalid reason=permissions")
            return
        channel = None
        if intent["channel_id"]:
            try:
                channel = await _fetch_guild_channel(self.bot, guild, int(intent["channel_id"]))
            except discord.NotFound:
                await self.bot.storage.delete_active_temp_channel(int(intent["channel_id"]))
                self.bot.active_temp_channel_ids.discard(int(intent["channel_id"]))
                await self.store.deleted(intent["channel_id"])
                raise ApiDeferred(1) from None
            if channel is None:
                raise ApiDeferred(60)
        if channel is None:
            existing = await self.bot.storage.get_active_temp_channel_by_owner(guild.id, member.id)
            if existing:
                try:
                    existing_channel = await _fetch_guild_channel(self.bot, guild, int(existing["channel_id"]))
                except discord.NotFound:
                    await self.bot.storage.delete_active_temp_channel(int(existing["channel_id"]))
                    self.bot.active_temp_channel_ids.discard(int(existing["channel_id"]))
                    raise ApiDeferred(1) from None
                if existing_channel:
                    if existing_channel.members:
                        await notify_duplicate_room(self.bot, member, lobby, existing_channel)
                        await self.abandon(row, intent)
                        return
                    await self.store.cleanup(guild.id, existing_channel.id)
                raise ApiDeferred(5)
            config = await self.bot.storage.get_guild_config(guild.id)
            prefix = str(config["temp_channel_prefix"]) if config else DEFAULT_TEMP_CHANNEL_PREFIX
            # Persist transmission intent before entering the client. A crash
            # anywhere afterward cannot silently replay the create.
            await self.store.phase(intent["id"], "sent")
            try:
                channel = await guild.create_voice_channel(
                    name=build_temp_channel_name(member, profile, prefix), category=category,
                    user_limit=max(0, min(99, int(profile["default_user_limit"] or 0))),
                    reason=f"YapHub intent {intent['id']}",
                )
            except ApiDeferred:
                await self.store.phase(intent["id"], "prepared")
                raise
            except discord.HTTPException as error:
                if error.status >= 500:
                    await self.store.phase(intent["id"], "unresolved")
                    raise AmbiguousMutation("Create response may follow a successful mutation") from error
                await self.store.phase(intent["id"], "cancelled")
                await self.store.cancel(guild.id, member.id, row["revision"])
                raise
            except BaseException:
                await asyncio.shield(self.store.phase(intent["id"], "unresolved"))
                raise
            try:
                await self.store.attach(intent, channel.id)
            except BaseException:
                # Do not roll back an untracked channel through an uncertain
                # path. The durable intent and audit reason retain attribution.
                logger.critical("recovery attach_failed intent=%s channel=%s tracking_unresolved=1", intent["id"], channel.id)
                await asyncio.shield(self.store.phase(intent["id"], "unresolved"))
                raise
            intent.update(channel_id=str(channel.id), phase="created")
            self.bot.active_temp_channel_ids.add(channel.id)
        # Reload gateway membership immediately before moving. No departed
        # user is pulled back, even if they left while create was in flight.
        current = getattr(getattr(member, "voice", None), "channel", None)
        if getattr(current, "id", None) == channel.id:
            await self.store.phase(intent["id"], "moved")
        elif getattr(current, "id", None) == lobby.id and await self.store.current(row):
            try:
                await member.move_to(channel, reason=f"YapHub intent {intent['id']}")
            except discord.Forbidden:
                await self.abandon(row, intent)
                raise
            await self.store.phase(intent["id"], "moved")
        else:
            await self.abandon(row, intent)
            return
        tracked = await self.bot.storage.get_active_temp_channel(channel.id)
        if tracked is None:
            raise ApiDeferred(5)
        if tracked["panel_message_id"] is None:
            panel = await send_room_panel(channel, member)
            if panel is None:
                raise ApiDeferred(10)
            await self.bot.storage.set_panel_message_id(channel.id, panel.id)
        if await self.store.complete(intent, pseudonymous_guild_key(guild.id), pseudonymous_user_key(member.id)):
            self.bot.last_room_success = time.time()
            self.wait_seconds = max(0, time.time() - row["enqueued"])
            logger.info("recovery room_lifecycle_completed intent=%s guild=%s channel=%s wait_seconds=%.1f",
                        intent["id"], guild.id, channel.id, self.wait_seconds)
        await self.store.cancel(guild.id, member.id, row["revision"])

    async def recover_audit(self, guild, intent):
        if intent["id"] in self.audit_checked:
            return
        if not getattr(getattr(getattr(guild, "me", None), "guild_permissions", None), "view_audit_log", False):
            self.audit_checked.add(intent["id"])
            return
        matches = []
        after = datetime.fromtimestamp(intent["created"] - 5, UTC)
        try:
            async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.channel_create, after=after):
                if entry.reason == f"YapHub intent {intent['id']}" and entry.user and entry.user.id == self.bot.user.id:
                    matches.append(entry.target.id)
        except (discord.Forbidden, discord.NotFound):
            self.audit_checked.add(intent["id"])
            return
        self.audit_checked.add(intent["id"])
        if len(matches) == 1:
            channel = await _fetch_guild_channel(self.bot, guild, matches[0])
            if channel is not None:
                await self.store.attach(intent, channel.id)
                self.bot.active_temp_channel_ids.add(channel.id)
                logger.info("recovery intent_attributed_from_audit intent=%s channel=%s", intent["id"], channel.id)

    async def run_cleanup(self, row):
        tracked = await self.bot.storage.get_active_temp_channel(int(row["channel_id"]))
        if tracked is None:
            await self.store.cleanup_result(row)
            return
        if await self.store.protects(row["channel_id"]):
            await self.store.cleanup_result(row, 5)
            return
        guild = self.bot.get_guild(int(row["guild_id"]))
        if guild is None or getattr(guild, "unavailable", False):
            raise ApiDeferred(5)
        if row["leaver_id"] != "0":
            channel = await _fetch_guild_channel(self.bot, guild, int(row["channel_id"]))
            if channel:
                leaver = guild.get_member(int(row["leaver_id"]))
                if leaver is not None and leaver not in channel.members:
                    await cleanup_temp_channel(self.bot, channel, leaver)
        remains = await _reconcile_row(self.bot, tracked, int(row["channel_id"]))
        if not remains:
            self.bot.active_temp_channel_ids.discard(int(row["channel_id"]))
            await self.store.deleted(row["channel_id"])
            await self.store.cleanup_result(row)
        else:
            await self.store.cleanup_result(row, 60)
