# YapHub Roadmap

This is the living, version-controlled source of truth for YapHub's
infrastructure and scaling roadmap. It consolidates the history and decisions
from [Issue #5](https://github.com/diese-tech/lab-yaphub/issues/5) (closed)
and carries the phases that issue deferred forward.

## How to use this document

- This file — not a closed issue's comment thread — is the canonical plan for
  future infrastructure phases (Postgres migration, sharding, dashboard/API).
- Edit it directly via a normal PR whenever a phase starts, completes, or its
  trigger conditions change. Land the update here, don't just discuss it in
  chat or in an issue comment, so the plan never drifts out of sync with
  reality.
- New issues/PRs about a future phase should link back to the relevant
  section here instead of re-deriving scope from scratch.

## Phase 1 — Single worker + SQLite (complete)

Originally scoped in Issue #5, closed 2026-07-19.

Delivered:

- `guild_configs` / `temp_vc_profiles` / `active_temp_channels` persisted in
  SQLite on a Railway Volume, documented in the README.
- Multi-guild, multi-profile support; all state keyed by `guild_id`.
- Restart-safe reconciliation (`reconcile_active_temp_channels` in
  `services/temp_channels.py`): stale/empty room cleanup, control-panel
  message backfill for rooms that predate a schema change.
- `bot.profile_cache` / `bot.active_temp_channel_ids` are rebuildable caches
  only, invalidated on config changes; SQLite is the source of truth
  throughout.
- All storage access goes through `asyncio.to_thread` so SQLite I/O can't
  block the event loop for other guilds under concurrent load.
- Full room lifecycle and moderation surface: persistent control panel,
  lock/unlock, hide/unhide, permit/block lists, kick, transfer, claim,
  admin-override audit logging with an optional mod-log channel.
- Automated test suite (pytest) and CI running on every push/PR.

Not done, and intentionally out of scope for Phase 1 — see below.

## Phase 2 — Postgres migration

### Trigger conditions

Per the original Issue #5 discussion, move off SQLite-on-Railway-Volume once
**any** of the following becomes true:

- More than one worker/process is needed.
- The bot is invited to servers outside a controlled beta.
- Dashboard/API config writes are added.
- Uptime/restart-recovery matters as a real production SLA.
- Guild count grows beyond a controlled beta.

None of these have been hit yet as of this writing — YapHub is still a
single worker in controlled beta.

### Why (carried over from the original discussion)

- Railway Volume state is tied to a single service/worker; horizontal
  scaling gets awkward fast with volume-backed SQLite.
- Multi-worker writes to SQLite are not safe.
- Backups, migrations, and observability are weaker than managed Postgres.
- Dashboard/API writes become painful without a shared DB.
- Sharding wants a shared DB, not isolated local disk state.

### Candidate providers

Supabase Postgres, Neon Postgres, Railway Postgres.

### Status

Not started.

## Phase 3 — Sharding

Discord bots at larger guild counts may need gateway sharding. Phase 1 was
deliberately built stateless-and-DB-backed at the worker level so this
doesn't require an architecture rewrite when it's actually needed.

**Do not shard prematurely** — this stays deferred until real guild-count or
gateway pressure justifies it, not in anticipation of it.

Status: not started, not yet needed.

## Phase 4 — Dashboard / API

If a web dashboard is added later, it should write to the same DB tables the
bot reads from — bot workers stay read-from-DB/cache, not a second config
store.

Status: not started, no dashboard currently planned.

## Smaller open items surfaced during Phase 1

Non-blocking, tracked here so they don't get lost:

- `temp_vc_profiles` never got the `inherit_category_permissions` /
  `enabled` boolean columns proposed in the original Issue #5 schema.
  Nothing depends on them today; needs a decision on whether to add them or
  drop them from the plan.
- The landing page (`docs/index.html`) stats ("servers using YapHub", "temp
  rooms created") are still placeholder text. Wiring them to live data needs
  an architecture decision, since the bot (Railway) and the static site
  (GitHub Pages) don't share a filesystem. Options: (a) the bot periodically
  commits an updated `docs/stats.json` via the GitHub API, or (b) the bot
  exposes a small public JSON endpoint the page fetches directly. Neither
  has been decided.

## History

- **2026-05-09** — Issue #5 opened: "Design multi-server scaling
  architecture for 100+ guilds." Established the phased storage plan
  (in-memory prototype → Railway Volume + SQLite for beta → Postgres before
  public launch) and the target data model.
- **2026-07-19** — Issue #5 closed as completed. All eight acceptance
  criteria met by the Phase 1 implementation: async SQLite storage, the full
  moderation feature set, reconciliation-based restart recovery, and a
  pytest + CI suite. See the issue for the full closing checklist.
- **2026-07-19** — This roadmap created to carry Phases 2–4 forward as a
  version-controlled document instead of re-deriving scope from a closed
  issue's comment thread each time.
