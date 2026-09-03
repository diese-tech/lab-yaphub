# YapHub Architecture and Operations

This document preserves the implementation, reliability, telemetry, deployment, and operational notes that are intentionally too detailed for YapHub's user-facing `README.md`.

## Room Tracking Invariants

The temporary-room lifecycle is built around five rules, pinned by `tests/test_incident_regression.py`:

1. **A managed room that still exists is never forgotten.** If YapHub cannot delete a room it created, the tracking record stays. It blocks a second room for that owner and reconciliation retries cleanup.
2. **Tracking is removed only on proof.** The room was either deleted successfully or Discord answered `404 Not Found`. A `403` or `5xx` is not proof.
3. **Ownership is proved by ID, never by name.** Rooms resolve through persisted `channel_id` and `guild_id`; a similarly named channel is never treated as managed.
4. **Every destructive step is guild-scoped.** A record from one guild cannot resolve to a channel in another.
5. **Repeating an action cannot amplify a failure.** Duplicate voice events, repeated lobby joins, cleanup passes, and reconciliation passes are idempotent.

These rules were formalized after an earlier failure mode could leave orphaned duplicate rooms behind.

## Concurrency and Load Testing

`tests/test_load_stress.py` re-proves the lifecycle invariants under concurrent load, including hundreds of guilds and members executing through `asyncio.gather` against the real per-`(guild, member)` lock and SQLite storage layer. Only Discord network I/O is faked.

The load tests run as part of normal `pytest`, so they execute in CI on every push. Changes touching locking, storage, creation, cleanup, or reconciliation should continue to answer one question: **do the lifecycle invariants still hold at scale?**

The suite is mutation-tested. Removing the per-user creation lock produces the `displaced_active_room` critical log emitted when a tracking record is clobbered and fails `test_duplicate_join_storm_across_every_guild_blocks_every_extra_attempt`.

If the lifecycle gains a new concurrency invariant, such as a new lock, shared cache, or cross-request state, extend this suite rather than creating a separate load-testing process.

All SQLite access runs off the event loop through `asyncio.to_thread`.

## Permission Preflight

Before creating a room, YapHub preflights the permissions required for the operation and skips creation with an actionable log entry if the known requirements are not satisfied.

It checks:

- **Manage Channels** and **Connect** where the room will be created. For a categorized room, this is resolved against the destination category. For a top-level room, guild-wide permissions apply. `Connect` matters independently because `Move Members` does not let the bot place a member into a channel the bot itself cannot join.
- **Move Members** on both the lobby the member is leaving and the destination.

The preflight fails **open** when a permission cannot be read. It is a guard, not a guarantee: Discord remains authoritative and can still reject an operation, so rollback and reconciliation remain the actual safety net.

## Persistence and Reconciliation

YapHub persists guild configuration, profiles, active temporary-channel tracking, permission state, and telemetry in SQLite.

One active owned temporary room is enforced per user per guild. On restart, reconciliation removes stale records and deletes empty orphaned managed rooms. A room record is retained when deletion cannot be proven successful so cleanup can be retried safely.

## Usage Telemetry

YapHub stores durable, privacy-safe usage telemetry separately from `active_temp_channels`. Operational room state answers what Discord resources exist now; telemetry answers what YapHub has done over time. Historical telemetry therefore survives normal room cleanup and reconciliation.

### Storage shape

Defined primarily in `services/telemetry.py` and `storage.py`:

- `telemetry_daily_counts`: one row per `(day, event_type)`, incremented per event. Storage grows with elapsed calendar days and event types rather than usage volume. Rolling 7-day and 30-day windows sum trailing UTC calendar days, including today.
- `telemetry_known_users` and `telemetry_known_guilds`: one row per distinct pseudonymous entity ever observed, enabling exact lifetime-unique counts without storing raw Discord IDs.

### Privacy boundary

Raw Discord user and guild IDs are not stored in telemetry. When `YAPHUB_ANALYTICS_SECRET` is configured, unique entities use keyed pseudonymous identifiers:

```text
user_key  = HMAC-SHA256(YAPHUB_ANALYTICS_SECRET, "user:"  + discord_user_id)
guild_key = HMAC-SHA256(YAPHUB_ANALYTICS_SECRET, "guild:" + discord_guild_id)
```

The prefixes separate the user and guild namespaces. HMAC is keyed so the values cannot be recomputed from public source code without the analytics secret.

Counter-only metrics such as room totals and reliability counters store no user or guild reference. `YAPHUB_ANALYTICS_SECRET` is optional. Without it, counter metrics continue normally while unique-entity recognition pauses and logs a warning. Telemetry is best-effort: telemetry failure must never affect the temporary-room lifecycle.

`storage.get_telemetry_summary()` is the aggregate read path and never returns pseudonymous keys.

## Public Stats Endpoint

The GitHub Pages landing page at `docs/index.html` displays adoption metrics from a small public JSON endpoint served by the bot via `services/stats_server.py`:

```text
GET /stats.json
```

### Why the bot serves the endpoint

The Railway bot service and static GitHub Pages site do not share a filesystem. Having the bot commit a generated `docs/stats.json` would require a repository-write credential in Railway and give the bot process write access to its own source. A read-only public endpoint has a smaller blast radius. `aiohttp` is already available through discord.py.

### Cached, not live

`services/public_stats.py` builds a snapshot from `get_telemetry_summary()` hourly using `STATS_REFRESH_INTERVAL_HOURS` from `config.py`. The snapshot is persisted to the single-row `public_stats_snapshot` table and copied to `bot.public_stats_cache`.

The HTTP handler reads only the in-process cache and never queries storage per request. This prevents unauthenticated HTTP traffic from competing with Discord operations for the shared bounded thread executor used for SQLite work.

During startup, `setup_hook` warms the cache from the durable snapshot so a restart does not need to wait for the next scheduled refresh. A failed refresh leaves the previous durable and in-memory snapshot unchanged. The landing page exposes the snapshot's `as_of` value so cached metrics are not presented as real-time data.

### Allowlisted payload

`build_public_snapshot()` explicitly lists public fields rather than returning an internal object and removing sensitive fields. Current public fields are:

- `rooms_created_total`
- `rooms_created_7d`
- `rooms_created_30d`
- `active_profiles`
- `as_of`
- `servers_served`, when unique-entity telemetry is available
- `unique_users_served`, when unique-entity telemetry is available

The endpoint never exposes raw or pseudonymous Discord identifiers, guild or user lists, or live `active_temp_channels` state.

When `YAPHUB_ANALYTICS_SECRET` is absent, `servers_served` and `unique_users_served` are omitted rather than falsely reported as zero. The landing page renders a missing value as an em dash. Other counters do not depend on the secret, so zero remains a valid value for them.

### Existing-guild backfill

When the analytics secret is first enabled, `backfill_known_guilds()` folds every guild with at least one configured profile into `telemetry_known_guilds`. This avoids reporting zero servers until an already-established guild happens to create another room.

The backfill is guarded against secret rotation. `telemetry_backfill_state` stores a SHA-256 fingerprint of the configured secret. Reusing the same secret is idempotent; detecting a different fingerprint skips the backfill and logs a warning rather than silently double-counting guilds hashed under the previous secret.

### Deployment

Enable public networking for the Railway service. The server binds to Railway's `$PORT`. Set the generated service URL as the `STATS_ENDPOINT` constant in `docs/index.html`.

If the stats server cannot bind, it logs the failure and the Discord bot continues normally. Public stats are supplementary and must never block core bot functionality.

## Railway Volume Setup

For the current SQLite deployment model, mount a persistent Railway Volume and place the database on that volume.

Recommended configuration:

1. Add a Railway Volume to the bot service.
2. Mount it at a stable path such as `/data`.
3. Configure either:

```env
YAPHUB_DATA_DIR=/data
```

or:

```env
YAPHUB_DB_PATH=/data/yaphub.sqlite3
```

4. Deploy the bot worker.

This preserves guild configuration, profiles, telemetry, and active temporary-channel tracking across restarts and deploys.

## Environment Variables

```env
DISCORD_TOKEN=your_discord_bot_token_here
YAPHUB_DATA_DIR=./data
# Optional explicit override; wins over YAPHUB_DATA_DIR:
# YAPHUB_DB_PATH=./data/yaphub.sqlite3
# Optional; enables pseudonymous unique-user/guild telemetry:
# YAPHUB_ANALYTICS_SECRET=
```

Never commit the Discord token or analytics secret to source control.

## Testing Checklist

- Bot starts without schema errors.
- Slash commands sync successfully.
- `/yap setup` creates a default lobby.
- `/yap profile create` creates an additional category-scoped section.
- Joining a lobby creates a temporary voice channel in the correct category.
- Leaving the last member deletes the temporary room.
- Restarting preserves occupied rooms and cleans empty orphaned rooms.
- A user with an existing occupied room is blocked from creating a second room.

Run the automated suite with:

```bash
pytest
```

## Startup Log Warnings

Three voice/configuration warnings have been investigated:

| Warning | Verdict |
| --- | --- |
| `PyNaCl is not installed, voice will NOT be supported` | **Benign for YapHub.** YapHub manages voice channels through Discord REST operations and does not open audio voice connections. Installing PyNaCl only to silence this warning would add an unnecessary native dependency. |
| `davey is not installed, voice will NOT be supported` | **Benign for YapHub** for the same reason. |
| `Privileged message content intent is missing` | **Resolved.** YapHub has no prefix-command surface that requires message content. `command_prefix` uses `commands.when_mentioned`, `help_command` is `None`, and the privileged message-content intent remains disabled. |

`tests/test_discord_configuration.py` pins these expectations, including a control test that verifies the message-content warning remains detectable.

## Known Constraints

- SQLite is the only persistence target in the current phase.
- Voice-state events cannot send true ephemeral notices.
- **Single worker only.** Locks are in-process `asyncio.Lock` objects and SQLite lives on one Railway Volume. Multiple replicas could independently create rooms for the same owner. Horizontal scaling requires the Postgres and distributed-locking work tracked in `ROADMAP.md`.
- Fallback duplicate-room notices depend on channel messaging availability and permissions.
- Earlier in-memory MVP owner-control behavior that is not represented in the current canonical implementation should not be treated as supported merely because it existed historically.
