"""Tests for services/public_stats.py: the field allowlist that decides
what YapHub is allowed to say about itself in public, and the best-effort
refresh orchestration around it.
"""

from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from services.public_stats import build_public_snapshot, refresh_public_stats_snapshot

FULL_TELEMETRY_SUMMARY = {
    "rooms_created_total": 18273,
    "rooms_created_7d": 614,
    "rooms_created_30d": 2164,
    "unique_users_served_total": 3912,
    "unique_guilds_served_total": 42,
    "active_profiles": 58,
    # Internal-only reliability counters -- must never reach the public
    # payload. Present here specifically so the allowlist test can prove
    # they are dropped, not merely absent from the fixture.
    "room_create_failed_total": 91,
    "duplicate_blocked_total": 40,
    "rollback_failed_tracking_preserved_total": 3,
    "reconcile_cleanup_ok_total": 5000,
    "reconcile_cleanup_failed_total": 12,
}


def test_the_public_snapshot_contains_exactly_the_documented_fields():
    snapshot = build_public_snapshot(FULL_TELEMETRY_SUMMARY)

    assert set(snapshot.keys()) == {
        "as_of",
        "servers_served",
        "unique_users_served",
        "rooms_created_total",
        "rooms_created_7d",
        "rooms_created_30d",
        "active_profiles",
    }


@pytest.mark.parametrize(
    "internal_field",
    [
        "room_create_failed_total",
        "duplicate_blocked_total",
        "rollback_failed_tracking_preserved_total",
        "reconcile_cleanup_ok_total",
        "reconcile_cleanup_failed_total",
    ],
)
def test_internal_reliability_counters_never_reach_the_public_payload(internal_field):
    """The literal 'privacy-safe field allowlisting' acceptance criterion:
    each internal counter, and its distinctive value, must not appear
    anywhere in the public output."""
    snapshot = build_public_snapshot(FULL_TELEMETRY_SUMMARY)

    assert internal_field not in snapshot
    distinctive_value = FULL_TELEMETRY_SUMMARY[internal_field]
    assert distinctive_value not in snapshot.values()


def test_public_field_values_map_from_the_correct_telemetry_field():
    snapshot = build_public_snapshot(FULL_TELEMETRY_SUMMARY)

    assert snapshot["servers_served"] == 42
    assert snapshot["unique_users_served"] == 3912
    assert snapshot["rooms_created_total"] == 18273
    assert snapshot["rooms_created_7d"] == 614
    assert snapshot["rooms_created_30d"] == 2164
    assert snapshot["active_profiles"] == 58


def test_as_of_is_a_timezone_aware_iso8601_string_in_eastern_time():
    fixed_now = datetime(2026, 9, 2, 14, 30, tzinfo=ZoneInfo("UTC"))  # 10:30 ET

    snapshot = build_public_snapshot(FULL_TELEMETRY_SUMMARY, now=fixed_now)

    parsed = datetime.fromisoformat(snapshot["as_of"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == fixed_now.astimezone(ZoneInfo("America/New_York")).utcoffset()
    assert parsed.hour == 10
    assert parsed.minute == 30


def test_as_of_reflects_generation_time_not_a_fixed_string():
    early = build_public_snapshot(
        FULL_TELEMETRY_SUMMARY, now=datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
    )
    later = build_public_snapshot(
        FULL_TELEMETRY_SUMMARY, now=datetime(2026, 6, 1, tzinfo=ZoneInfo("UTC"))
    )

    assert early["as_of"] != later["as_of"]


def test_zero_state_produces_zeroed_not_missing_fields():
    """A brand-new deployment with no activity yet must still publish a
    complete, well-formed snapshot -- zeros, not partial/missing keys."""
    empty_summary = {
        "rooms_created_total": 0,
        "rooms_created_7d": 0,
        "rooms_created_30d": 0,
        "unique_users_served_total": 0,
        "unique_guilds_served_total": 0,
        "active_profiles": 0,
    }

    snapshot = build_public_snapshot(empty_summary)

    assert snapshot["rooms_created_total"] == 0
    assert snapshot["servers_served"] == 0
    assert "as_of" in snapshot


# --- refresh orchestration ---------------------------------------------


def _bot(**storage_overrides):
    defaults = dict(
        get_telemetry_summary=AsyncMock(return_value=dict(FULL_TELEMETRY_SUMMARY)),
        save_public_stats_snapshot=AsyncMock(),
    )
    defaults.update(storage_overrides)
    return types.SimpleNamespace(storage=types.SimpleNamespace(**defaults))


async def test_refresh_saves_a_built_snapshot():
    bot = _bot()

    await refresh_public_stats_snapshot(bot)

    bot.storage.save_public_stats_snapshot.assert_awaited_once()
    kwargs = bot.storage.save_public_stats_snapshot.call_args.kwargs
    assert kwargs["as_of"]
    assert '"servers_served": 42' in kwargs["payload"]
    assert "room_create_failed_total" not in kwargs["payload"]


async def test_refresh_never_raises_when_telemetry_read_fails(caplog):
    bot = _bot(get_telemetry_summary=AsyncMock(side_effect=RuntimeError("db down")))

    await refresh_public_stats_snapshot(bot)  # must not raise

    bot.storage.save_public_stats_snapshot.assert_not_called()
    assert "snapshot_refresh_failed" in caplog.text


async def test_refresh_never_raises_when_the_save_fails(caplog):
    bot = _bot(save_public_stats_snapshot=AsyncMock(side_effect=RuntimeError("disk full")))

    await refresh_public_stats_snapshot(bot)  # must not raise

    assert "snapshot_refresh_failed" in caplog.text


async def test_a_failed_refresh_does_not_overwrite_a_real_prior_snapshot(caplog):
    """The literal failure-behavior requirement: refresh_public_stats_snapshot
    never issues a save call on failure, so whatever storage already holds
    -- the last known-good snapshot -- is left completely untouched."""
    bot = _bot(get_telemetry_summary=AsyncMock(side_effect=RuntimeError("db down")))

    await refresh_public_stats_snapshot(bot)

    bot.storage.save_public_stats_snapshot.assert_not_called()
