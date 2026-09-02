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
"""

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger("yaphub")

BOT_KEY = web.AppKey("bot", Any)


async def _handle_stats(request: web.Request) -> web.Response:
    bot = request.app[BOT_KEY]
    row = await bot.storage.get_public_stats_snapshot()

    if row is None:
        # No refresh has ever succeeded yet (a very new deployment). 503
        # rather than an empty/zeroed payload: publishing a snapshot that
        # looks real but isn't would violate "do not publish malformed,
        # partially generated, or obviously inconsistent data."
        return web.json_response(
            {"error": "stats_not_yet_available"},
            status=503,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # payload is stored pre-serialized JSON text (see
    # Storage.save_public_stats_snapshot); served verbatim rather than
    # decoded and re-encoded so what was built by the allowlist in
    # services/public_stats.py is exactly what goes out, byte for byte.
    return web.Response(
        text=row["payload"],
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
