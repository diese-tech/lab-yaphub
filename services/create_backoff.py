"""Back off room creation when a 429 escapes discord.py's normal limiter."""

import asyncio
import logging
import math
import time

import discord

logger = logging.getLogger("yaphub")

# A local safety pause, not an assumed Discord quota. Real Retry-After
# headers take precedence, including waits longer than this fallback.
UNKNOWN_RETRY_PAUSE_SECONDS = 60.0


class RoomCreationPaused(Exception):
    """No request was sent because a previous creation was rate limited."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Room creation paused for {retry_after:.1f} seconds")


class CreateBackoff:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.until = 0.0

    async def create(self, guild, **kwargs):
        # Only the create request is serialized, not storage, movement or
        # panels. A rejected request closes the gate before another caller
        # (including one in a different guild) can send its create request.
        async with self.lock:
            remaining = self.until - time.monotonic()
            if remaining > 0:
                raise RoomCreationPaused(remaining)
            try:
                return await guild.create_voice_channel(**kwargs)
            except discord.HTTPException as error:
                if error.status != 429:
                    raise
                headers = error.response.headers
                try:
                    delay = float(headers.get("Retry-After", ""))
                    if not math.isfinite(delay) or delay <= 0:
                        raise ValueError
                    source = "retry_after_header"
                except (TypeError, ValueError):
                    delay = UNKNOWN_RETRY_PAUSE_SECONDS
                    source = "local_safety_pause"
                self.until = time.monotonic() + delay
                # Allowlisted metadata only: never tokens, request headers
                # or the response body. These distinguish API buckets from
                # edge restrictions without guessing from the error text.
                logger.warning(
                    "temp_room api_rate_limited guild=%s pause_seconds=%.1f "
                    "pause_source=%s scope=%s global=%s via=%s cf_ray=%s "
                    "content_type=%s",
                    guild.id, delay, source,
                    headers.get("X-RateLimit-Scope"),
                    headers.get("X-RateLimit-Global"),
                    headers.get("Via"), headers.get("CF-Ray"),
                    headers.get("Content-Type"),
                )
                # A 429 is not proof of a created resource. Do not retry
                # automatically or write tracking; the caller reports failure.
                raise
