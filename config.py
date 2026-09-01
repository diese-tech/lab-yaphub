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
