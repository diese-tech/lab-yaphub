create table if not exists guild_configs (
  guild_id text primary key,
  temp_channel_prefix text not null default '',
  notification_cooldown_seconds integer not null default 45,
  mod_log_channel_id text,
  created_at text not null,
  updated_at text not null
);

create table if not exists temp_vc_profiles (
  id text primary key,
  guild_id text not null,
  name text not null,
  join_channel_id text not null unique,
  target_category_id text,
  created_by_user_id text not null,
  default_user_limit integer,
  temp_name_template text,
  created_at text not null,
  updated_at text not null
);

create unique index if not exists idx_temp_vc_profiles_guild_name
  on temp_vc_profiles (guild_id, lower(name));

create index if not exists idx_temp_vc_profiles_guild
  on temp_vc_profiles (guild_id);

create table if not exists active_temp_channels (
  channel_id text primary key,
  guild_id text not null,
  profile_id text not null,
  owner_user_id text not null,
  panel_message_id text,
  created_at text not null,
  last_seen_at text not null
);

create unique index if not exists idx_active_temp_channels_owner
  on active_temp_channels (guild_id, owner_user_id);

create index if not exists idx_active_temp_channels_guild
  on active_temp_channels (guild_id);

create table if not exists temp_channel_permits (
  channel_id text not null,
  user_id text not null,
  created_at text not null,
  primary key (channel_id, user_id)
);

create table if not exists temp_channel_blocks (
  channel_id text not null,
  user_id text not null,
  created_at text not null,
  primary key (channel_id, user_id)
);

-- Durable usage telemetry, deliberately separate from active_temp_channels:
-- operational state answers "what exists right now" and is deleted on
-- cleanup; these tables answer "what has happened over time" and must
-- survive it. Bounded by calendar time (telemetry_daily_counts) or by real
-- entity cardinality (telemetry_known_users/guilds), never by event volume,
-- so neither needs pruning. See services/telemetry.py for the privacy
-- boundary these tables are built around.

create table if not exists telemetry_daily_counts (
  day text not null,
  event_type text not null,
  count integer not null default 0,
  primary key (day, event_type)
);

-- user_key / guild_key are HMAC-SHA256(secret, "<type>:<discord_id>") --
-- never a raw Discord snowflake. Existence in these tables proves "this
-- pseudonymous entity has had a room created for it at least once"; nothing
-- here maps back to a Discord id without the server-side secret.
create table if not exists telemetry_known_users (
  user_key text primary key,
  first_seen_at text not null
);

create table if not exists telemetry_known_guilds (
  guild_key text primary key,
  first_seen_at text not null
);

-- The one cached snapshot served by the public stats endpoint
-- (services/stats_server.py). Singleton row (id is always 1) holding the
-- last successfully generated snapshot; a failed refresh simply does not
-- write here, leaving the previous good snapshot in place. `payload` is
-- the exact JSON already served to the public -- built once by
-- services/public_stats.py through its field allowlist, not re-filtered
-- on every request.
create table if not exists public_stats_snapshot (
  id integer primary key check (id = 1),
  as_of text not null,
  payload text not null
);

-- Tracks which YAPHUB_ANALYTICS_SECRET backfill_known_guilds() last ran
-- under (see services/telemetry.py). A rotated secret produces a different
-- HMAC for the same guild, so blindly re-backfilling under a new secret
-- would insert a second, permanently-unmatchable row per guild into
-- telemetry_known_guilds and silently inflate servers_served. Singleton
-- row (id is always 1); no row at all means the backfill has never run.
-- secret_fingerprint is a plain SHA-256 of the secret used only to detect
-- whether it changed -- never the HMAC keying used for pseudonymization
-- itself (see pseudonymous_guild_key / pseudonymous_user_key).
create table if not exists telemetry_backfill_state (
  id integer primary key check (id = 1),
  secret_fingerprint text not null,
  updated_at text not null
);
