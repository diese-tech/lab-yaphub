"""The public stats HTTP endpoint: a single read-only route serving the
cached snapshot services/public_stats.py produces.

This exists so the GitHub-Pages-hosted landing page (a different origin
from wherever this bot is deployed) can fetch adoption numbers without
either the bot holding repo-write credentials or the site needing any
form of database access. See the "Public Stats Endpoint" section of
README.md for the full design rationale and the two architectures this
was weighed against.

Deliberately minimal: one GET route, no auth (the payload is public
aggregate data by construction -- see services/public_stats.py's
allowlist), no other verbs, no request body handling. aiohttp is already
a discord.py dependency, so this adds no new library.

The handler is also deliberately storage-free: it reads bot.public_stats_cache,
a plain in-process attribute, and awaits nothing. It never calls
bot.storage. Every real bot operation (room creation, cleanup,
reconciliation) offloads its SQLite calls through the same shared
asyncio.to_thread executor; if this handler read the database per request
too, an unauthenticated, unrate-limited flood on this route -- trivial to
produce against any public port, with no ill intent required -- could
queue enough work on that shared, bounded pool to delay real Discord
operations. Reading a plain attribute instead makes that impossible by
construction rather than by hoping traffic stays low. services/
public_stats.py's refresh_public_stats_snapshot is the only writer of
bot.public_stats_cache; bot.py warms it from the durable snapshot once at
startup so a restart doesn't 503 while waiting for the first daily
refresh.
"""

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger("yaphub")

BOT_KEY = web.AppKey("bot", Any)


async def _handle_stats(request: web.Request) -> web.Response:
    bot = request.app[BOT_KEY]
    payload = getattr(bot, "public_stats_cache", None)

    if payload is None:
        # No refresh has ever succeeded and no prior snapshot existed to
        # warm from (a brand-new deployment). 503 rather than an empty/
        # zeroed payload: publishing a snapshot that looks real but isn't
        # would violate "do not publish malformed, partially generated, or
        # obviously inconsistent data."
        return web.json_response(
            {"error": "stats_not_yet_available"},
            status=503,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # payload is pre-serialized JSON text (see refresh_public_stats_snapshot
    # and Storage.save_public_stats_snapshot); served verbatim rather than
    # decoded and re-encoded so what was built by the allowlist in
    # services/public_stats.py is exactly what goes out, byte for byte.
    return web.Response(
        text=payload,
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def build_stats_app(bot) -> web.Application:
    app = web.Application()
    app[BOT_KEY] = bot
    app.router.add_get("/stats.json", _handle_stats)
    return app


async def start_stats_server(bot, host: str, port: int) -> web.AppRunner:
    """Start the stats server and return its runner so the caller can
    clean it up on shutdown (runner.cleanup())."""
    app = build_stats_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("public_stats server_listening host=%s port=%s", host, port)
    return runner
