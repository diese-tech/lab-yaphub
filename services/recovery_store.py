"""Durable lifecycle work; every state transition is a SQLite transaction."""

import asyncio
import time
import uuid
from contextlib import closing

from config import TELEMETRY_EVENT_ROOM_CREATED
from storage import utc_now_iso


class RecoveryStore:
    def __init__(self, storage):
        self.storage = storage

    async def transaction(self, operation):
        def run():
            with closing(self.storage._connect()) as connection, connection:
                connection.execute("begin immediate")
                return operation(connection)
        return await asyncio.to_thread(run)

    async def rows(self, table):
        if table not in {"pending_joins", "pending_cleanup", "creation_intents"}:
            raise ValueError(table)
        return await self.transaction(lambda c: [dict(r) for r in c.execute(f"select * from {table}")])

    async def enqueue(self, guild, user, lobby, profile):
        def write(c):
            previous = c.execute("select * from pending_joins where guild_id=? and user_id=?",
                                 (str(guild), str(user))).fetchone()
            if previous and previous["lobby_id"] == str(lobby) and previous["profile_id"] == profile:
                return True
            if previous is None:
                total = c.execute("select count(*) from pending_joins").fetchone()[0]
                local = c.execute("select count(*) from pending_joins where guild_id=?", (str(guild),)).fetchone()[0]
                if total >= 500 or local >= 50:
                    return False
            c.execute("insert into pending_joins values(?,?,?,?,?,?,?) on conflict(guild_id,user_id) "
                      "do update set lobby_id=excluded.lobby_id,profile_id=excluded.profile_id,"
                      "revision=excluded.revision,due=0",
                      (str(guild), str(user), str(lobby), profile, str(uuid.uuid4()), time.time(), 0))
            return True
        return await self.transaction(write)

    async def cancel(self, guild, user, revision=None):
        return await self.transaction(lambda c: c.execute(
            "delete from pending_joins where guild_id=? and user_id=? and (? is null or revision=?)",
            (str(guild), str(user), revision, revision)).rowcount)

    async def defer(self, row, delay):
        await self.transaction(lambda c: c.execute(
            "update pending_joins set due=? where guild_id=? and user_id=? and revision=?",
            (time.time() + max(1, delay), row["guild_id"], row["user_id"], row["revision"])))

    async def current(self, row):
        return await self.transaction(lambda c: c.execute(
            "select 1 from pending_joins where guild_id=? and user_id=? and revision=?",
            (row["guild_id"], row["user_id"], row["revision"])).fetchone() is not None)

    async def intent(self, row):
        def get(c):
            result = c.execute("select * from creation_intents where guild_id=? and user_id=? "
                               "and phase not in ('completed','cancelled')",
                               (row["guild_id"], row["user_id"])).fetchone()
            if result:
                return dict(result)
            identifier = str(uuid.uuid4())
            c.execute("insert into creation_intents (id,guild_id,user_id,lobby_id,profile_id,phase,channel_id,created) values(?,?,?,?,?,?,?,?)",
                      (identifier, row["guild_id"], row["user_id"], row["lobby_id"],
                       row["profile_id"], "prepared", None, time.time()))
            return dict(c.execute("select * from creation_intents where id=?", (identifier,)).fetchone())
        return await self.transaction(get)

    async def phase(self, identifier, phase):
        await self.transaction(lambda c: c.execute("update creation_intents set phase=? where id=?",
                                                   (phase, identifier)))

    async def attach(self, intent, channel_id):
        def write(c):
            # Plain INSERT refuses to displace an existing owner's room.
            now = utc_now_iso()
            existing = c.execute("select * from active_temp_channels where channel_id=?", (str(channel_id),)).fetchone()
            if existing is None:
                c.execute("insert into active_temp_channels "
                          "(channel_id,guild_id,profile_id,owner_user_id,created_at,last_seen_at) values(?,?,?,?,?,?)",
                          (str(channel_id), intent["guild_id"], intent["profile_id"], intent["user_id"], now, now))
            elif (existing["guild_id"], existing["owner_user_id"]) != (intent["guild_id"], intent["user_id"]):
                raise ValueError("Channel already belongs to another owner")
            c.execute("update creation_intents set channel_id=?,phase='created' where id=?",
                      (str(channel_id), intent["id"]))
        await self.transaction(write)

    async def complete(self, intent, guild_key, user_key):
        def write(c):
            changed = c.execute("update creation_intents set phase='completed',completed=? where id=? and phase='moved'",
                                (time.time(), intent["id"])).rowcount
            if changed:
                now = utc_now_iso()
                c.execute("insert into telemetry_daily_counts values(?,?,1) on conflict(day,event_type) "
                          "do update set count=count+1", (now[:10], TELEMETRY_EVENT_ROOM_CREATED))
                if guild_key:
                    c.execute("insert or ignore into telemetry_known_guilds values(?,?)", (guild_key, now))
                if user_key:
                    c.execute("insert or ignore into telemetry_known_users values(?,?)", (user_key, now))
            return bool(changed)
        return await self.transaction(write)

    async def cleanup(self, guild, channel, leaver=0):
        await self.transaction(lambda c: c.execute(
            "insert into pending_cleanup values(?,?,?,0) on conflict(channel_id,leaver_id) do update set due=0",
            (str(channel), str(guild), str(leaver))))

    async def cleanup_result(self, row, delay=None):
        if delay is None:
            await self.transaction(lambda c: c.execute(
                "delete from pending_cleanup where channel_id=? and leaver_id=?", (row["channel_id"], row["leaver_id"])))
        else:
            await self.transaction(lambda c: c.execute(
                "update pending_cleanup set due=? where channel_id=? and leaver_id=?",
                (time.time() + max(1, delay), row["channel_id"], row["leaver_id"])))

    async def restore(self):
        # A crash after transmission and before recording the reply is ambiguous.
        await self.transaction(lambda c: c.execute("update creation_intents set phase='unresolved' where phase='sent'"))

    async def protects(self, channel):
        return await self.transaction(lambda c: c.execute(
            "select 1 from creation_intents where channel_id=? and phase in ('created','moved','unresolved')",
            (str(channel),)).fetchone() is not None)

    async def deleted(self, channel):
        await self.transaction(lambda c: c.execute(
            "update creation_intents set phase='cancelled' where channel_id=? and phase!='completed'", (str(channel),)))
