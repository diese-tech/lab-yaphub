"""Keep startup alive while Discord explicitly rejects login with HTTP 429."""

import asyncio
import logging
import math

import discord

logger = logging.getLogger("yaphub")


async def login_with_backoff(login, close_http, token):
    fallback = 60.0
    attempt = 0
    while True:
        try:
            await login(token)
            if attempt:
                logger.info("startup login_recovered attempts=%s", attempt + 1)
            return
        except discord.HTTPException as error:
            # Invalid credentials, permissions and other errors must remain
            # visible instead of being turned into an endless retry loop.
            if error.status != 429:
                raise
            attempt += 1
            headers = error.response.headers
            try:
                delay = float(headers.get("Retry-After", ""))
                if not math.isfinite(delay) or delay <= 0:
                    raise ValueError
                source = "retry_after_header"
            except (TypeError, ValueError):
                delay = fallback
                source = "local_backoff"
            fallback = min(fallback * 2, 900.0)
            logger.warning(
                "startup login_rate_limited attempt=%s retry_after=%.1f "
                "pause_source=%s scope=%s global=%s via=%s cf_ray=%s content_type=%s",
                attempt, delay, source, headers.get("X-RateLimit-Scope"),
                headers.get("X-RateLimit-Global"), headers.get("Via"),
                headers.get("CF-Ray"), headers.get("Content-Type"),
            )
            # static_login creates an aiohttp session on each call. Close
            # the failed session before retrying to avoid leaking connectors.
            await close_http()
            await asyncio.sleep(delay)
