create table if not exists guild_configs (
  guild_id text primary key,
  temp_channel_prefix text not null default '',
  notification_cooldown_seconds integer not null default 45,
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
