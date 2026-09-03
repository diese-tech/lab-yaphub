"""Builds and refreshes the cached public stats snapshot served by
services/stats_server.py.

This is the one place the field allowlist for public exposure is
enforced. storage.get_telemetry_summary() returns internal reliability
counters (room_create_failed_total, duplicate_blocked_total, and so on)
alongside the adoption metrics -- useful for incident analysis, never for
the public. build_public_snapshot() picks exactly the fields the public
contract in issue #24 names and nothing else, by construction: it lists
the public keys explicitly rather than returning everything and trying to
subtract the sensitive ones, so a new internal counter added to the
telemetry summary later cannot silently leak into the public payload just
by existing in the summary dict.

The snapshot is a cache, not a live view: refresh_public_stats_snapshot
is meant to run about once a day (see bot.py's stats_refresh_loop), and a
failed refresh must leave the previous good snapshot in place -- this
module never deletes or clears a snapshot, only replaces it on success.
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from services.telemetry import analytics_secret_configured

logger = logging.getLogger("yaphub")

# The timezone the public "Stats as of ..." freshness label is expressed
# in, matching the issue's example ("2026-09-02T10:00:00-04:00") and the
# ~10am ET refresh schedule. zoneinfo resolves EST/EDT automatically, so
# this needs no manual DST handling.
_STATS_TIMEZONE = ZoneInfo("America/New_York")


def build_public_snapshot(telemetry_summary: dict, *, now: datetime | None = None) -> dict:
    """Map a full telemetry summary onto the public, privacy-safe payload.

    `now` is injectable for tests; production callers omit it and get the
    real current time.

    servers_served / unique_users_served are OMITTED, not zeroed, when
    YAPHUB_ANALYTICS_SECRET isn't configured. Without the secret,
    record_room_created() never writes to telemetry_known_guilds/
    telemetry_known_users (see services/telemetry.py), so those counts sit
    at a structural zero regardless of real usage -- publishing that as
    servers_served: 0 would tell an established, busy deployment it has no
    servers. The issue's own contract anticipates exactly this: "may omit
    metrics that are not yet trustworthy" rather than publish a fake exact
    value. rooms_created_* and active_profiles never depend on the secret
    (they are plain counters/existing config, no identity involved), so
    they are always present and a real 0 there is a real, honest zero.
    """
    as_of = (now or datetime.now(_STATS_TIMEZONE)).astimezone(_STATS_TIMEZONE)

    snapshot = {
        "as_of": as_of.isoformat(),
        "rooms_created_total": telemetry_summary["rooms_created_total"],
        "rooms_created_7d": telemetry_summary["rooms_created_7d"],
        "rooms_created_30d": telemetry_summary["rooms_created_30d"],
        "active_profiles": telemetry_summary["active_profiles"],
    }

    if analytics_secret_configured():
        snapshot["servers_served"] = telemetry_summary["unique_guilds_served_total"]
        snapshot["unique_users_served"] = telemetry_summary["unique_users_served_total"]

    return snapshot


async def refresh_public_stats_snapshot(bot) -> None:
    """Build a fresh snapshot from durable telemetry, persist it, and
    update the in-memory cache the HTTP route actually serves from.

    Best-effort: any failure here is logged and leaves the previously
    cached snapshot (if any) untouched, per the issue's explicit
    requirement that a failed refresh must not erase the last known-good
    snapshot. Never raises.

    services/stats_server.py reads bot.public_stats_cache directly, with
    no storage call in the request path -- deliberately, so a request
    flood on that public, unauthenticated route cannot queue work onto the
    same asyncio.to_thread executor every real Discord operation (room
    creation, cleanup, reconciliation) shares. This function is the only
    writer of that attribute.
    """
    try:
        summary = await bot.storage.get_telemetry_summary()
        snapshot = build_public_snapshot(summary)
        payload = json.dumps(snapshot)
        await bot.storage.save_public_stats_snapshot(as_of=snapshot["as_of"], payload=payload)
        bot.public_stats_cache = payload
        logger.info("public_stats snapshot_refreshed as_of=%s", snapshot["as_of"])
    except Exception:
        logger.exception(
            "public_stats snapshot_refresh_failed; the previous cached snapshot, "
            "if any, remains in place"
        )
