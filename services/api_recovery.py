"""One durable admission policy for Discord REST, including client retries."""

import asyncio
import contextvars
import json
import logging
import math
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import aiohttp
import discord
from discord.http import HTTPClient, Route

logger = logging.getLogger("yaphub")
request_context = contextvars.ContextVar("discord_request", default=None)
rest_precondition = contextvars.ContextVar("discord_mutation_precondition", default=None)


class ApiDeferred(Exception):
    """No new HTTP attempt was sent; durable work may resume later."""

    def __init__(self, retry_after=1.0):
        self.retry_after = max(0.1, retry_after)
        super().__init__("Discord request deferred")


class ExplicitRejection(ApiDeferred):
    """Discord explicitly rejected the attempt with a 429."""


class AmbiguousMutation(Exception):
    """A channel create may have reached Discord; never replay it blindly."""


def retry_delay(headers, body, now):
    value = headers.get("Retry-After")
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(value)
                delay = when.timestamp() - now if when.tzinfo else 0
            except (TypeError, ValueError, OverflowError):
                delay = 0
        if math.isfinite(delay) and delay > 0:
            return delay
    value = (body or {}).get("retry_after")
    if isinstance(value, bool):
        raise ValueError("Boolean retry timing")
    delay = float(value)
    if not math.isfinite(delay) or delay <= 0:
        raise ValueError("Invalid retry timing")
    return delay


@dataclass(frozen=True)
class RequestInfo:
    method: str
    path: str
    major: str = ""

    @property
    def key(self):
        return f"{self.method} {self.path}:{self.major}"

    @property
    def creates_channel(self):
        return self.method == "POST" and self.path == "/guilds/{guild_id}/channels"


def url_info(method, url):
    # Only normalized route names reach logs. In particular, webhook and
    # interaction tokens and query strings are never retained.
    path = re.sub(r"^/api/v\d+", "", url.path)
    parts = path.split("/")
    major = ""
    for index, part in enumerate(parts):
        if part in {"guilds", "channels", "webhooks"} and index + 1 < len(parts):
            major = parts[index + 1]
            break
    path = re.sub(r"/\d+", "/{id}", path)
    if path.startswith("/webhooks/"):
        path = "/webhooks/{id}/{token}"
    elif path.startswith("/interactions/"):
        path = "/interactions/{id}/{token}/callback"
    return RequestInfo(method, path, major)


class ApiRecovery:
    def __init__(self, storage):
        self.storage = storage
        self.lock = asyncio.Lock()
        self.states = {}
        self.aliases = {}
        self.probes = {}
        self.initialized = False
        self.events = deque()
        self.transmissions = deque()
        self.deferred = 0

    async def initialize(self):
        await self.storage.initialize()
        self.states = {row["scope_key"]: dict(row) for row in await self.storage.list_api_cooldowns()}
        for state in self.states.values():
            state["deadline_mono"] = time.monotonic() + max(0, state["deadline_utc"] - time.time())
        self.initialized = True

    def _keys(self, info):
        return ("global", self.aliases.get(info.key, info.key))

    def _remaining(self, state):
        return max(0.0, state["deadline_mono"] - time.monotonic()) if "deadline_mono" in state else max(
            0.0, state["deadline_utc"] - time.time()
        )

    def blocked(self):
        return "global" in self.states

    def retry_at(self):
        state = self.states.get("global")
        return state["deadline_utc"] if state else None

    async def admit(self, info):
        async with self.lock:
            for key in self._keys(info):
                state = self.states.get(key)
                if state is None:
                    continue
                remaining = self._remaining(state)
                owner = self.probes.get(key)
                if remaining > 0 or (owner is not None and owner is not asyncio.current_task()):
                    self.deferred += 1
                    raise ApiDeferred(remaining or 1)
                if key == "global" and (info.method, info.path) != ("GET", "/users/@me"):
                    self.deferred += 1
                    raise ApiDeferred(1)
                self.probes[key] = asyncio.current_task()

    async def release_probe(self, info):
        async with self.lock:
            for key in self._keys(info):
                if self.probes.get(key) is asyncio.current_task():
                    self.probes.pop(key, None)
                    state = self.states.get(key)
                    if state is not None:
                        state["deadline_utc"] = max(state["deadline_utc"], time.time() + 60)
                        state["deadline_mono"] = time.monotonic() + max(0, state["deadline_utc"] - time.time())
                        await self.storage.save_api_cooldown(state)

    async def observe(self, info, response, body, duration):
        headers = response.headers
        status = response.status
        if headers.get("X-RateLimit-Bucket"):
            self.aliases[info.key] = f"bucket:{headers['X-RateLimit-Bucket']}:{info.major}"
        now = time.time()
        self.events.append((now, status))
        while self.events and self.events[0][0] < now - 600:
            self.events.popleft()
        if status == 429:
            scope = headers.get("X-RateLimit-Scope", "").lower()
            local = (
                scope in {"user", "shared"} and body is not None
                and ("global" not in body or body["global"] is False)
                and headers.get("X-RateLimit-Global", "").lower() in {"", "false"}
                and bool(headers.get("Via")) and bool(headers.get("X-RateLimit-Bucket"))
            )
            key = f"bucket:{headers['X-RateLimit-Bucket']}:{info.major}" if local else "global"
            async with self.lock:
                previous = self.states.get(key, {})
                attempt = previous.get("attempt", 0) + 1
                try:
                    delay = retry_delay(headers, body, now)
                    source = "discord"
                except (TypeError, ValueError):
                    delay = min(60 * 2 ** min(attempt - 1, 4), 900)
                    source = "local_backoff"
                deadline = max(previous.get("deadline_utc", 0), now + delay)
                state = {"scope_key": key, "deadline_utc": deadline, "attempt": attempt,
                         "route_key": info.key, "reason": source}
                self.states[key] = {**state, "deadline_mono": time.monotonic() + max(0, deadline - now)}
                self.aliases[info.key] = key
                self.probes.pop(key, None)
                # Remain blocked in memory if storage fails. Never allow
                # subsequent traffic merely because persisting failed.
                await self.storage.save_api_cooldown(state)
            logger.warning(
                "api rejected method=%s route=%s status=429 pause_scope=%s retry_after=%.1f "
                "timing_source=%s cf_ray=%s scope=%s",
                info.method, info.path, "resource" if local else "global", deadline - now,
                source, headers.get("CF-Ray"), scope or "unknown",
            )
        elif 200 <= status < 300:
            async with self.lock:
                for key in self._keys(info):
                    if self.probes.get(key) is asyncio.current_task():
                        await self.storage.delete_api_cooldown(key)
                        self.states.pop(key, None)
                        self.probes.pop(key, None)
                        logger.info("api recovered scope=%s", "global" if key == "global" else "resource")
        logger.info("api attempt method=%s route=%s status=%s duration_ms=%.1f bucket=%s scope=%s cf_ray=%s",
                    info.method, info.path, status, duration * 1000, headers.get("X-RateLimit-Bucket"),
                    headers.get("X-RateLimit-Scope"), headers.get("CF-Ray"))

    def metrics(self):
        now = time.time()
        counts = Counter(status for at, status in self.events if at >= now - 600)
        return {"requests_10m": sum(at >= now - 600 for at in self.transmissions), "http_401_10m": counts[401],
                "http_403_10m": counts[403], "http_429_10m": counts[429], "transport_errors_10m": counts[0],
                "deferred": self.deferred}

    def trace(self):
        trace = aiohttp.TraceConfig()

        async def start(session, ctx, params):
            ctx.info = request_context.get() or url_info(params.method, params.url)
            ctx.started = time.monotonic()
            ctx.send_count = 0
            await self.admit(ctx.info)

        async def headers(session, ctx, params):
            # In the pinned aiohttp writer this supported hook runs before
            # header serialization/writing, including connection retries.
            await self.admit(ctx.info)
            predicate = rest_precondition.get()
            if predicate and (ctx.info.creates_channel or (ctx.info.method == "PATCH" and ctx.info.path == "/guilds/{guild_id}/members/{user_id}")):
                if not predicate():
                    self.deferred += 1
                    raise ApiDeferred(1)
            if ctx.info.creates_channel and ctx.send_count:
                raise AmbiguousMutation("Channel create transmission cannot be replayed")
            ctx.send_count += 1
            now = time.time()
            self.transmissions.append(now)
            while self.transmissions and self.transmissions[0] < now - 600:
                self.transmissions.popleft()
            logger.info("api transmission method=%s route=%s transport_attempt=%s",
                        ctx.info.method, ctx.info.path, ctx.send_count)

        async def end(session, ctx, params):
            body = None
            if params.response.status == 429 and params.response.content_type == "application/json":
                try:
                    payload = await params.response.json()
                    if isinstance(payload, dict):
                        body = payload
                except (ValueError, aiohttp.ClientError):
                    pass
            await self.observe(ctx.info, params.response, body, time.monotonic() - ctx.started)
            if ctx.info.creates_channel and params.response.status >= 500:
                raise AmbiguousMutation("Discord create returned 5xx")

        async def exception(session, ctx, params):
            if isinstance(params.exception, ApiDeferred):
                raise params.exception
            self.events.append((time.time(), 0))
            logger.warning("api transport_failed method=%s route=%s exception_type=%s",
                           ctx.info.method, ctx.info.path, type(params.exception).__name__)
            if getattr(ctx, "info", None) and ctx.info.creates_channel:
                raise AmbiguousMutation("Discord create transport failed") from params.exception

        trace.on_request_start.append(start)
        trace.on_request_headers_sent.append(headers)
        trace.on_request_end.append(end)
        trace.on_request_exception.append(exception)
        return trace


class RecoveryHTTPClient(HTTPClient):
    def __init__(self, original, recovery):
        super().__init__(original.loop, connector=None, proxy=original.proxy, proxy_auth=original.proxy_auth,
                         unsync_clock=original.use_clock, http_trace=recovery.trace(),
                         max_ratelimit_timeout=original.max_ratelimit_timeout)
        self.recovery = recovery

    async def request(self, route, **kwargs):
        # discord.py's major_parameters contains webhook tokens. Our key
        # only needs the resource id, never the credential.
        major = str(route.channel_id or route.guild_id or route.webhook_id or "")
        info = RequestInfo(route.method, route.path, major)
        if not self.recovery.initialized:
            raise ApiDeferred()
        # Restore route->bucket aliases from durable records before access.
        for key, state in self.recovery.states.items():
            if key != "global":
                self.recovery.aliases[state["route_key"]] = key
        if self.recovery.blocked() and (route.method, route.path) != ("GET", "/users/@me"):
            await self.request(Route("GET", "/users/@me"))
        await self.recovery.admit(info)
        marker = request_context.set(info)
        try:
            return await super().request(route, **kwargs)
        except discord.HTTPException as error:
            if error.status == 429:
                raise ExplicitRejection(self.recovery._remaining(
                    self.recovery.states.get("global") or self.recovery.states.get(
                        self.recovery.aliases.get(info.key, info.key), {"deadline_utc": time.time() + 60}
                    )
                )) from error
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as error:
            if info.creates_channel:
                raise AmbiguousMutation("Discord create outcome unknown") from error
            raise
        finally:
            request_context.reset(marker)
            await self.recovery.release_probe(info)


def status_snapshot(bot):
    api = getattr(bot, "api_recovery", None)
    initialized = bool(getattr(bot, "runtime_initialized", False))
    connected = bot.is_ready()
    blocked = api.blocked() if api else False
    state = "starting" if not initialized else "disconnected" if not connected else "blocked" if blocked else (
        "partial" if (api and api.states) or getattr(bot, "blocked_panel_count", 0) else "available"
    )
    return {"state": state, "observed_at": time.time(), "retry_at": api.retry_at() if api else None,
            "metrics": api.metrics() if api else {}, "pending_joins": getattr(bot, "pending_join_count", 0),
            "pending_cleanup": getattr(bot, "pending_cleanup_count", 0),
            "unresolved_creations": getattr(bot, "unresolved_creation_count", 0),
            "blocked_panels": getattr(bot, "blocked_panel_count", 0),
            "queue_overflow": getattr(bot, "queue_overflow_count", 0),
            "last_queue_wait_seconds": getattr(bot, "queue_wait_seconds", 0),
            "last_room_success": getattr(bot, "last_room_success", None)}


def status_json(bot):
    return json.dumps(status_snapshot(bot))
