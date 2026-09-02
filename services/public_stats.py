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
    """
    as_of = (now or datetime.now(_STATS_TIMEZONE)).astimezone(_STATS_TIMEZONE)

    return {
        "as_of": as_of.isoformat(),
        "servers_served": telemetry_summary["unique_guilds_served_total"],
        "unique_users_served": telemetry_summary["unique_users_served_total"],
        "rooms_created_total": telemetry_summary["rooms_created_total"],
        "rooms_created_7d": telemetry_summary["rooms_created_7d"],
        "rooms_created_30d": telemetry_summary["rooms_created_30d"],
        "active_profiles": telemetry_summary["active_profiles"],
    }


async def refresh_public_stats_snapshot(bot) -> None:
    """Build a fresh snapshot from durable telemetry and cache it.

    Best-effort: any failure here is logged and leaves the previously
    cached snapshot (if any) untouched, per the issue's explicit
    requirement that a failed refresh must not erase the last known-good
    snapshot. Never raises.
    """
    try:
        summary = await bot.storage.get_telemetry_summary()
        snapshot = build_public_snapshot(summary)
        await bot.storage.save_public_stats_snapshot(
            as_of=snapshot["as_of"],
            payload=json.dumps(snapshot),
        )
        logger.info("public_stats snapshot_refreshed as_of=%s", snapshot["as_of"])
    except Exception:
        logger.exception(
            "public_stats snapshot_refresh_failed; the previous cached snapshot, "
            "if any, remains in place"
        )
