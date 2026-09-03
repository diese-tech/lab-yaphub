import os


DEFAULT_TEMP_CHANNEL_PREFIX = ""
JOIN_TO_CREATE_NAME = "Join to Yap"
DEFAULT_NOTIFICATION_COOLDOWN_SECONDS = 45
NOTIFICATION_DELETE_AFTER_SECONDS = 15
RECONCILE_INTERVAL_MINUTES = 10

# An empty tracked room younger than this is not treated as an orphan.
# create_temp_room records the room before `member.move_to` lands, and the
# member only appears in channel.members once the gateway event arrives; a
# reconcile pass inside that window would delete a room whose creation is
# still in flight. Genuinely abandoned rooms are reaped one interval later.
RECONCILE_MIN_ROOM_AGE_SECONDS = 60

DATA_DIR = os.getenv("YAPHUB_DATA_DIR", "data")
DATABASE_PATH = os.getenv("YAPHUB_DB_PATH", os.path.join(DATA_DIR, "yaphub.sqlite3"))

# Telemetry event-type strings, stored in telemetry_daily_counts.event_type.
# Defined here (not in storage.py or services/telemetry.py) so both can
# reference the same constants without storage.py -- a pure persistence
# layer nothing else in the codebase imports from -- depending on services/.
TELEMETRY_EVENT_ROOM_CREATED = "room_created"
TELEMETRY_EVENT_ROOM_CREATE_FAILED = "room_create_failed"
TELEMETRY_EVENT_DUPLICATE_BLOCKED = "duplicate_blocked"
TELEMETRY_EVENT_ROLLBACK_FAILED_TRACKING_PRESERVED = "rollback_failed_tracking_preserved"
TELEMETRY_EVENT_RECONCILE_CLEANUP_OK = "reconcile_cleanup_ok"
TELEMETRY_EVENT_RECONCILE_CLEANUP_FAILED = "reconcile_cleanup_failed"

# Env var holding the server-side secret used to derive pseudonymous
# user/guild keys (see services/telemetry.py). Never committed, never
# logged, never exposed publicly.
ANALYTICS_SECRET_ENV_VAR = "YAPHUB_ANALYTICS_SECRET"

# The public stats HTTP server (services/stats_server.py) binds 0.0.0.0 so
# Railway's proxy can reach it, on $PORT -- Railway's standard convention
# for "this process wants a public port" -- falling back to 8080 for local
# runs where nothing sets it.
STATS_SERVER_HOST = "0.0.0.0"
STATS_SERVER_PORT = int(os.getenv("PORT", "8080"))

# How often the cached public stats snapshot is refreshed from durable
# telemetry. discord.ext.tasks' `hours=` scheduling (see bot.py) runs this
# once immediately, then every N hours after that.
STATS_REFRESH_INTERVAL_HOURS = 1
