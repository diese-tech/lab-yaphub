"""Tests for services/telemetry.py: the pseudonymization boundary and the
best-effort orchestration around it.

Storage-level derivation (windowing, aggregation) is covered in
tests/test_storage.py; end-to-end wiring at the real call sites is covered
in tests/test_telemetry_wiring.py. This file is about the two things that
are specific to this module: the HMAC keys are actually keyed and actually
namespaced, and nothing here can ever raise into a caller.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

import services.telemetry as telemetry


@pytest.fixture(autouse=True)
def _reset_warn_once_flag(monkeypatch):
    # _warned_missing_secret is a module-level latch so the missing-secret
    # warning logs once per process, not once per room creation. Tests that
    # exercise the missing-secret path need it reset, or only the first
    # such test in the run would ever see the warning.
    monkeypatch.setattr(telemetry, "_warned_missing_secret", False)


def _bot(**storage_overrides):
    defaults = dict(
        record_telemetry_event=AsyncMock(),
        record_known_user=AsyncMock(),
        record_known_guild=AsyncMock(),
        list_all_guild_ids=AsyncMock(return_value=[]),
    )
    defaults.update(storage_overrides)
    return types.SimpleNamespace(storage=types.SimpleNamespace(**defaults))


# --- analytics_secret_configured --------------------------------------------


def test_analytics_secret_configured_is_true_when_the_env_var_is_set(monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")

    assert telemetry.analytics_secret_configured() is True


def test_analytics_secret_configured_is_false_when_unset(monkeypatch):
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)

    assert telemetry.analytics_secret_configured() is False


def test_analytics_secret_configured_has_no_warning_side_effect(monkeypatch, caplog):
    """Unlike _get_secret(), this is meant to be called on every public-stats
    build -- it must never touch the missing-secret warning latch or log."""
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)

    telemetry.analytics_secret_configured()

    assert "YAPHUB_ANALYTICS_SECRET is not set" not in caplog.text


# --- pseudonymous key derivation --------------------------------------------


def test_user_key_is_deterministic(monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")

    assert telemetry.pseudonymous_user_key(42) == telemetry.pseudonymous_user_key(42)


def test_user_and_guild_keys_do_not_collide_on_the_same_numeric_id(monkeypatch):
    """The whole point of namespacing: a user and a guild that happen to
    share a snowflake must never hash to the same pseudonymous key."""
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")

    assert telemetry.pseudonymous_user_key(12345) != telemetry.pseudonymous_guild_key(12345)


def test_different_secrets_produce_different_keys(monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "secret-a")
    key_a = telemetry.pseudonymous_user_key(42)

    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "secret-b")
    key_b = telemetry.pseudonymous_user_key(42)

    assert key_a != key_b


def test_the_key_is_actually_keyed_not_a_plain_hash(monkeypatch):
    """A plain (unkeyed) hash of the Discord id would be trivially
    recomputable by anyone who read this file -- not pseudonymous at all.
    The output must depend on the secret, not just the id."""
    import hashlib

    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")

    key = telemetry.pseudonymous_user_key(777)

    assert key != hashlib.sha256(b"user:777").hexdigest()
    assert key != hashlib.sha256(b"777").hexdigest()


def test_different_users_get_different_keys(monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")

    assert telemetry.pseudonymous_user_key(1) != telemetry.pseudonymous_user_key(2)


def test_missing_secret_returns_none_rather_than_a_weak_default(monkeypatch):
    """No hardcoded fallback secret: that would make every deployment's
    pseudonymous keys derivable by anyone who read this file."""
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)

    assert telemetry.pseudonymous_user_key(1) is None
    assert telemetry.pseudonymous_guild_key(1) is None


def test_missing_secret_warns_exactly_once(monkeypatch, caplog):
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)

    telemetry.pseudonymous_user_key(1)
    telemetry.pseudonymous_user_key(2)
    telemetry.pseudonymous_guild_key(3)

    assert caplog.text.count("YAPHUB_ANALYTICS_SECRET") == 1


# --- best-effort orchestration -----------------------------------------------


async def test_record_event_swallows_a_storage_failure(caplog):
    bot = _bot(record_telemetry_event=AsyncMock(side_effect=RuntimeError("db down")))

    await telemetry.record_event(bot, "room_created")  # must not raise

    assert "Failed to record telemetry event" in caplog.text


async def test_record_room_created_bumps_the_counter_and_both_entity_tables(monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")
    bot = _bot()

    await telemetry.record_room_created(bot, guild_id=100, user_id=200)

    bot.storage.record_telemetry_event.assert_awaited_once_with("room_created")
    bot.storage.record_known_guild.assert_awaited_once_with(
        telemetry.pseudonymous_guild_key(100)
    )
    bot.storage.record_known_user.assert_awaited_once_with(telemetry.pseudonymous_user_key(200))


async def test_record_room_created_still_bumps_the_counter_without_a_secret(monkeypatch):
    """Room-count and reliability metrics must not wait on analytics
    configuration -- only unique-entity recognition needs the secret."""
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)
    bot = _bot()

    await telemetry.record_room_created(bot, guild_id=100, user_id=200)

    bot.storage.record_telemetry_event.assert_awaited_once_with("room_created")
    bot.storage.record_known_guild.assert_not_called()
    bot.storage.record_known_user.assert_not_called()


async def test_record_room_created_records_the_user_even_if_the_guild_write_fails(monkeypatch):
    """Each step is independently best-effort -- one failing must not skip
    the others."""
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")
    bot = _bot(record_known_guild=AsyncMock(side_effect=RuntimeError("db down")))

    await telemetry.record_room_created(bot, guild_id=100, user_id=200)  # must not raise

    bot.storage.record_telemetry_event.assert_awaited_once_with("room_created")
    bot.storage.record_known_user.assert_awaited_once()


@pytest.mark.parametrize(
    "record_fn,expected_event_type",
    [
        (telemetry.record_room_create_failed, "room_create_failed"),
        (telemetry.record_duplicate_blocked, "duplicate_blocked"),
        (
            telemetry.record_rollback_failed_tracking_preserved,
            "rollback_failed_tracking_preserved",
        ),
        (telemetry.record_reconcile_cleanup_ok, "reconcile_cleanup_ok"),
        (telemetry.record_reconcile_cleanup_failed, "reconcile_cleanup_failed"),
    ],
)
async def test_each_reliability_recorder_bumps_its_own_event_type(record_fn, expected_event_type):
    bot = _bot()

    await record_fn(bot)

    bot.storage.record_telemetry_event.assert_awaited_once_with(expected_event_type)


# --- backfill_known_guilds ----------------------------------------------


async def test_backfill_folds_every_configured_guild_when_secret_is_set(monkeypatch):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")
    bot = _bot(list_all_guild_ids=AsyncMock(return_value=[100, 200]))

    await telemetry.backfill_known_guilds(bot)

    bot.storage.record_known_guild.assert_any_await(telemetry.pseudonymous_guild_key(100))
    bot.storage.record_known_guild.assert_any_await(telemetry.pseudonymous_guild_key(200))
    assert bot.storage.record_known_guild.await_count == 2


async def test_backfill_is_a_noop_without_the_secret(monkeypatch):
    """Without the secret there's nothing safe to derive -- and calling
    storage at all would be wasted work on every startup."""
    monkeypatch.delenv("YAPHUB_ANALYTICS_SECRET", raising=False)
    bot = _bot(list_all_guild_ids=AsyncMock(return_value=[100, 200]))

    await telemetry.backfill_known_guilds(bot)

    bot.storage.list_all_guild_ids.assert_not_called()
    bot.storage.record_known_guild.assert_not_called()


async def test_backfill_never_raises_when_listing_guild_ids_fails(monkeypatch, caplog):
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")
    bot = _bot(list_all_guild_ids=AsyncMock(side_effect=RuntimeError("db down")))

    await telemetry.backfill_known_guilds(bot)  # must not raise

    bot.storage.record_known_guild.assert_not_called()
    assert "Failed to list guild ids for telemetry backfill" in caplog.text


async def test_backfill_continues_past_a_single_guild_write_failure(monkeypatch, caplog):
    """One guild's write failing must not skip the rest -- each is
    independently best-effort, matching record_room_created's pattern."""
    monkeypatch.setenv("YAPHUB_ANALYTICS_SECRET", "s3cret")
    bot = _bot(
        list_all_guild_ids=AsyncMock(return_value=[100, 200]),
        record_known_guild=AsyncMock(side_effect=[RuntimeError("db down"), None]),
    )

    await telemetry.backfill_known_guilds(bot)  # must not raise

    assert bot.storage.record_known_guild.await_count == 2
    assert "Failed to backfill known guild for telemetry" in caplog.text
