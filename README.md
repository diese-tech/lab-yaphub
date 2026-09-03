# YapHub

YapHub is a focused Discord bot for VoiceMaster-style temporary voice channels. It creates temporary rooms from one or more Join to Yap lobbies, tracks them in SQLite, and cleans them up when they are empty.

## Invite YapHub

Use this invite link to add YapHub to a Discord server:

```text
https://discord.com/oauth2/authorize?client_id=1503456577666154506&permissions=277313817680&integration_type=0&scope=bot+applications.commands
```

If YapHub was invited with an older link, re-invite it with this one (or add
Manage Roles to its role): editing channel permission overwrites requires
Manage Roles, and lock, unlock, hide, unhide, permit and block are all built
on channel permission overwrites.

The invite URL only adds the bot to a server. The running bot service still needs `DISCORD_TOKEN` configured in its environment.

Never commit the bot token to GitHub.

## Current MVP Scope

- `/yap setup` creates a Join to Yap lobby in the selected category
- `/yap config` shows the stored guild configuration
- `/yap reset` clears configured profiles for a guild (confirm/cancel buttons)
- `/yap profile create` adds additional category-scoped Join to Yap sections
- `/yap profile list` lists configured profiles
- `/yap profile delete` removes a profile (autocomplete + confirm/cancel buttons)
- Every temp room gets an in-channel control panel (lock, unlock, hide, unhide,
  rename, limit, transfer, claim, permit, kick) so members don't need to remember slash commands
- `/yap permit` / `/yap unpermit` manage a per-room allow list: permitted members
  keep view/connect access while the room is hidden or locked and after leaving;
  the list persists across bot restarts and clears when the room closes
- `/yap room` shows a read-only info embed for the room you are in
- `/yap profile create` supports an optional default user limit and a
  `{user}` name template per lobby
- Room commands have light anti-spam cooldowns, and renames respect Discord's
  2-per-10-minutes channel rename limit with an honest error message
- Temporary rooms are persisted in SQLite
- One active owned temp room is enforced per user per guild
- Restart reconciliation removes stale records and deletes empty orphan temp rooms
- All SQLite access runs off the event loop via `asyncio.to_thread`
- Slash command errors are caught centrally and reported to the user instead of failing silently

## Room Tracking Invariants

These are the rules the temp-room lifecycle is built on. They exist because
breaking the first one is what once left a server full of orphaned duplicate
rooms, and they are pinned by `tests/test_incident_regression.py`.

1. **A managed room that still exists is never forgotten.** If YapHub cannot
   delete a room it created, the tracking record stays. It blocks a second
   room for that owner and reconciliation retries the cleanup.
2. **Tracking is removed only on proof.** Either the room was just deleted
   successfully, or Discord answered `404 Not Found`. A `403` or a `5xx` is
   not proof — a hidden room and an API outage both look like that.
3. **Ownership is proved by ID, never by name.** Rooms are resolved through
   the persisted `channel_id`/`guild_id`, so a channel that merely *looks*
   like `<name>'s Yap` is never touched.
4. **Every destructive step is guild-scoped.** A record from one guild can
   never resolve to a channel in another.
5. **Repeating an action cannot amplify a failure.** Duplicate voice events,
   repeated lobby joins and repeated cleanup or reconciliation passes are all
   idempotent.

## Load Testing

`tests/test_load_stress.py` re-proves the invariants above under real
concurrency pressure — hundreds of guilds and members firing through
`asyncio.gather` at once against the real per-`(guild, member)` lock and the
real SQLite storage layer (only Discord's network I/O is faked). It is not a
separate step: it runs as part of the normal `pytest` invocation, so it
already executes in CI on every push.

Treat it as the standing question to answer whenever a change touches
locking, storage, or the create/cleanup/reconcile paths: **does this still
hold at scale?** If a change makes one of these tests fail, that change made
YapHub less safe under load, whatever else it does — that failure is not a
flake to retry past. It's mutation-tested: removing the per-user creation
lock produces the exact `displaced_active_room` critical log the storage
layer emits for a clobbered tracking record, and fails
`test_duplicate_join_storm_across_every_guild_blocks_every_extra_attempt`.

If a future change to the lifecycle needs a *new* concurrency invariant
covered (a new lock, a new shared cache, a new cross-request state), add it
here rather than starting a separate load-testing process — one file, one
place this question gets answered, always run.

Before creating a room, YapHub preflights the permissions the operation
actually needs and skips creation — with an actionable log line — rather than
creating a room it cannot move anyone into:

- **Manage Channels** and **Connect** where the room will land. The room does
  not exist yet, so this is resolved against the destination category, or
  against YapHub's guild-wide permissions for a top-level room (which inherits
  nothing from the lobby next to it). Connect matters on its own: Move Members
  does not let the bot place someone in a channel it could not join itself.
- **Move Members** on the lobby the member is leaving *and* on the destination.

The preflight fails **open** — an unreadable permission never blocks creation,
because silently refusing to make rooms in a working server would be worse than
the failure it prevents. It is a guard, not a guarantee: Discord resolves
permissions server-side and can still refuse, so the rollback paths above
remain the real safety net.

## Product Direction

YapHub is intentionally narrow:

- Focused temporary voice channel bot
- VoiceMaster-style replacement target
- Multi-server capable
- Category-scoped
- Persistence-backed

YapHub is not trying to be a general-purpose mega-bot.

## How It Works

Example setup:

```text
GENERAL VOICE
  Join to Yap

LOWER DIVISION
  Lower Div Join to Yap

HIGHER DIVISION
  Higher Div Join to Yap
```

Flow:

```text
User joins a configured lobby
-> YapHub resolves the matching profile
-> YapHub creates a temp VC in the configured category
-> YapHub stores the active room in SQLite
-> YapHub moves the user into that temp VC
-> YapHub deletes the temp VC when it becomes empty
```

If a user already owns an active room in the same guild, YapHub does not create a second one. It attempts to DM them first, then falls back to a short-lived channel notice if DM delivery fails.

## Discord Permissions

Recommended bot permissions:

- Manage Channels
- Manage Roles (required to edit channel permission overwrites, which is what
  lock/unlock, hide/unhide, permit and block are made of)
- Move Members
- View Channels
- Connect
- Speak
- Send Messages
- Read Message History

YapHub does not need Administrator, so a channel-level `@everyone` deny applies
to the bot as well. Hiding or locking a room therefore always writes YapHub its
own allow overwrite first — without it the bot would lose sight of the room it
is managing, and the room would survive its last member.

OAuth scopes:

- `bot`
- `applications.commands`

## Local Setup

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable the required bot permissions.
3. Copy `.env.example` to `.env`.
4. Set `DISCORD_TOKEN`.
5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Run the bot:

```bash
python bot.py
```

7. In Discord, run:

```text
/yap setup category:#GENERAL VOICE
```

8. Add additional sections by running setup again with another category:

```text
/yap setup category:#SMITE LOWER DIV
```

For advanced/manual setup, use:

```text
/yap profile create name:"Lower Div Yaps" category:#SMITE-LOWER-DIV lobby_name:"Lower Div Join to Yap"
```

## Environment Variables

```env
DISCORD_TOKEN=your_discord_bot_token_here
YAPHUB_DATA_DIR=./data
# Optional explicit override:
# YAPHUB_DB_PATH=./data/yaphub.sqlite3
# Optional, see Usage Telemetry below:
# YAPHUB_ANALYTICS_SECRET=
```

`YAPHUB_DB_PATH` wins if both are set.

## Usage Telemetry

YapHub persists durable, privacy-safe usage telemetry — answering "what has
YapHub done over time" (rooms created, by whom, how often creation fails) as
a deliberately separate concern from `active_temp_channels`, which answers
"what Discord resources currently exist right now." Telemetry rows are never
touched by room creation, cleanup, or reconciliation, so historical counts
survive normal room lifecycle activity that deletes the operational record.

**Storage shape** (`services/telemetry.py`, `storage.py`):

- `telemetry_daily_counts` — one row per `(day, event_type)`, incremented on
  every event. Bounded by calendar days elapsed, not by usage volume — a
  decade of daily rows across all event types is a few tens of thousands,
  never needs pruning. Rolling 7d/30d windows sum trailing calendar days in
  UTC, inclusive of today.
- `telemetry_known_users` / `telemetry_known_guilds` — one row per distinct
  pseudonymous entity ever seen, giving an exact lifetime-unique count as a
  plain row count. Bounded by real entity cardinality, same as
  `guild_configs`.

**Privacy boundary.** Nothing here ever stores a raw Discord user or guild
ID. Exact unique counts use a pseudonymous key instead:

```text
user_key  = HMAC-SHA256(YAPHUB_ANALYTICS_SECRET, "user:"  + discord_user_id)
guild_key = HMAC-SHA256(YAPHUB_ANALYTICS_SECRET, "guild:" + discord_guild_id)
```

The `user:`/`guild:` prefix means a user and a guild that happen to share a
numeric snowflake never collide. The HMAC is *keyed* specifically so nobody
who has read this source can recompute a key without the secret — a plain
hash of the ID would not be pseudonymous at all. Counter-only metrics (room
totals, reliability counters) never touch identity: they are a daily count
bump with no user or guild reference stored anywhere. `YAPHUB_ANALYTICS_SECRET`
is optional — without it, room-count and reliability metrics work normally;
only unique-entity recognition pauses until it is configured, logging one
warning rather than failing startup over an optional feature. Every
telemetry call is best-effort: a telemetry failure can never affect the
temp-room lifecycle it measures.

`storage.get_telemetry_summary()` is the one read path, returning aggregate
counts only (never a pseudonymous key). The only thing built on top of it is
the cached, privacy-filtered public snapshot described next — nothing else
in the codebase reads telemetry directly.

## Public Stats Endpoint

The landing page (`docs/index.html`, served via GitHub Pages) shows adoption
numbers — servers using YapHub, rooms created — pulled from a small public
JSON endpoint the bot itself serves (`services/stats_server.py`):

```
GET /stats.json
```

**Why an endpoint, not a committed file.** The bot (Railway) and the static
site (GitHub Pages) don't share a filesystem. The alternative — the bot
pushing an updated `docs/stats.json` to the repo via a GitHub API token —
was considered and rejected: it would mean storing a repo-write credential
in Railway and giving the bot process write access to its own source, a
bigger blast-radius consideration than one additional public, unauthenticated,
read-only route with nothing sensitive to leak. `aiohttp` is already a
discord.py dependency, so this adds no new library, and Railway's public
networking is a first-class, well-supported path for exactly this.

**It is a cache, not a live view — and the HTTP route never touches
storage.** `services/public_stats.py` builds a snapshot from
`get_telemetry_summary()` about once a day (`~10:00 AM ET`, DST-safe via
`zoneinfo`; see `stats_refresh_loop` in `bot.py`), persists it to SQLite
(`public_stats_snapshot` — one row, always the latest), and writes it into
`bot.public_stats_cache`, a plain in-process attribute. The HTTP handler in
`services/stats_server.py` reads only that attribute — it never calls
`bot.storage`. This is deliberate: every real bot operation (room creation,
cleanup, reconciliation) offloads its SQLite calls through the same shared,
bounded `asyncio.to_thread` executor, and a public, unauthenticated,
unrate-limited route reading the database per request could let a request
flood queue enough work on that shared pool to delay real Discord
operations. Reading a plain attribute instead makes that impossible by
construction. `bot.py`'s `setup_hook` warms `public_stats_cache` from the
durable snapshot once at startup, so a restart doesn't serve `503` while
waiting for the next daily refresh. A failed refresh — a storage error,
whatever — leaves the previous cached snapshot (both the SQLite row and the
in-memory cache) exactly as it was; nothing here ever clears the cache, only
replaces it on a *successful* refresh. The landing page shows the cached
snapshot's `as_of` timestamp as a "Stats as of …" label specifically so this
is never mistaken for real-time data.

**Payload is allowlisted, not filtered.** `build_public_snapshot()` lists the
public fields explicitly (`rooms_created_total`, `rooms_created_7d`,
`rooms_created_30d`, `active_profiles`, `as_of`, plus `servers_served` and
`unique_users_served` — see below) rather than returning everything and
subtracting what's sensitive — so a new internal reliability counter added
to `get_telemetry_summary()` later cannot silently leak into the public
payload just by existing. Nothing here is ever a raw or pseudonymous Discord
identifier, a guild/user list, or live `active_temp_channels` state.

**`servers_served` / `unique_users_served` are omitted, not zeroed, without
`YAPHUB_ANALYTICS_SECRET`.** Those two fields depend on the pseudonymous
unique-entity tables in `services/telemetry.py`, which only get written to
when the analytics secret is configured (see "Usage Telemetry" above).
Without it, the underlying counts sit at a structural zero regardless of
real usage — publishing that as `servers_served: 0` would tell an
established, busy deployment it has no servers. `build_public_snapshot()`
omits both keys entirely in that case rather than publish a fake exact
value; the landing page's JS renders `—` when the key is absent. The other
fields never depend on the secret (plain counters / existing config, no
identity involved), so a `0` there is always a real, honest zero.

**`servers_served` backfills from existing profile config the first time the
secret goes live.** `telemetry_known_guilds` (what `servers_served` counts)
only grows from `record_room_created()` — so turning
`YAPHUB_ANALYTICS_SECRET` on for a deployment that's already had guilds set
up and using it would otherwise show `servers_served: 0` until each of
those guilds happens to create a *new* room. On every startup,
`backfill_known_guilds()` (`services/telemetry.py`) folds every guild with
at least one configured profile (`temp_vc_profiles` — i.e. a guild that's
actually run `/yap setup` or `/yap profile create`, not merely one that
peeked at `/yap config`) into `telemetry_known_guilds`, hashed with the
currently configured secret. It's a no-op without the secret.

**The backfill is guarded against secret rotation.** A rotated
`YAPHUB_ANALYTICS_SECRET` hashes the same guild differently, and
`record_known_guild`'s `insert or ignore` only dedupes identical hashes —
so blindly re-running the backfill after a rotation would insert a second,
permanently-unmatchable row per guild and silently inflate
`servers_served`. `backfill_known_guilds()` fingerprints the configured
secret (`telemetry_backfill_state`, a single-row table — the fingerprint
itself is a plain SHA-256, never the HMAC keying used for pseudonymization)
and compares it against the fingerprint from the last successful backfill.
Same secret: backfill proceeds normally (idempotent, safe on every boot).
Different secret: the backfill is skipped with a warning logged rather than
guessing how to reconcile the stale entries — that's a deliberate choice
left to whoever rotates the secret, not something the bot decides for you.

**Deploying it.** Enable public networking for the Railway service
(Settings → Networking → Generate Domain — the server binds `$PORT`, which
Railway sets automatically) and paste the resulting URL into the
`STATS_ENDPOINT` constant near the bottom of `docs/index.html`. If the
server fails to bind (a port conflict, a sandbox with no public networking),
it logs and the bot continues normally — the stats endpoint is supplementary
and can never block Discord functionality.

## Railway Volume Setup

For this MVP, Railway should mount a persistent volume and the bot should write SQLite into that mounted path.

Recommended setup:

1. Add a Railway Volume to the service.
2. Mount it at a stable path such as `/data`.
3. Set:

```env
YAPHUB_DATA_DIR=/data
```

or:

```env
YAPHUB_DB_PATH=/data/yaphub.sqlite3
```

4. Deploy the bot worker.

Result:

- SQLite survives restarts and deploys
- Guild config persists
- Profile config persists
- Active temp-channel tracking survives restart reconciliation

## Testing Checklist

- Bot starts without schema errors
- Slash commands sync successfully
- `/yap setup` creates a default lobby
- `/yap profile create` creates an additional section in a category
- Joining a lobby creates a temp VC in the correct category
- Leaving the last member in a temp VC deletes it
- Restarting the bot preserves occupied rooms and cleans empty orphan rooms
- A user with an existing occupied room is blocked from creating a second room

## Startup Log Warnings

Three warnings appear in production startup logs. Two are benign and one has
been resolved; none of them relate to room management.

| Warning | Verdict |
| --- | --- |
| `PyNaCl is not installed, voice will NOT be supported` | **Benign.** Logged unconditionally by `discord.Client.__init__`, regardless of what the bot does. It only matters for opening a voice *connection* (`VoiceChannel.connect`), which YapHub never does — it creates, edits, moves members between and deletes voice channels, all plain REST calls that need no audio codec. Installing PyNaCl purely to silence it would add a native dependency for nothing. |
| `davey is not installed, voice will NOT be supported` | **Benign,** same source and same reasoning. |
| `Privileged message content intent is missing` | **Resolved.** discord.py logs this when the message-content intent is off *and* a prefix-command surface exists. YapHub has no prefix commands, so the surface was the problem, not the intent: `command_prefix` is now `commands.when_mentioned` and `help_command` is `None`. The privileged intent stays **off** — YapHub reads no message content anywhere. |

`tests/test_discord_configuration.py` pins all three, including a control
test that proves the message-content warning is still detectable.

## Known Constraints

- SQLite is the only persistence target in this phase
- Voice-state events cannot send true ephemeral notices
- **Single worker only.** Locks are `asyncio.Lock` objects held in one
  process and SQLite lives on one Railway Volume. Running two replicas would
  give each its own lock table and its own database, so two lobby joins could
  create two rooms for the same owner. Scaling out requires the Postgres +
  distributed-locking work tracked in `ROADMAP.md`, not just a replica count
  change.
- Fallback duplicate-room notices depend on channel messaging availability and permissions
- Owner control commands from the earlier in-memory MVP are not part of this canonical Issue #8 pass
