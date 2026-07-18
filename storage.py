import asyncio
import os
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from config import DEFAULT_NOTIFICATION_COOLDOWN_SECONDS, DEFAULT_TEMP_CHANNEL_PREFIX


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Storage:
    """SQLite-backed storage.

    Every public method is async and offloads the blocking sqlite3 call to a
    worker thread via asyncio.to_thread, so DB I/O never stalls the bot's
    event loop for other guilds.
    """

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        Path(os.path.dirname(self.database_path) or ".").mkdir(parents=True, exist_ok=True)
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.executescript(schema)

    async def get_or_create_guild_config(self, guild_id: int) -> sqlite3.Row:
        return await asyncio.to_thread(self._get_or_create_guild_config, guild_id)

    def _get_or_create_guild_config(self, guild_id: int) -> sqlite3.Row:
        existing = self._get_guild_config(guild_id)
        if existing is not None:
            return existing

        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                insert into guild_configs (
                    guild_id,
                    temp_channel_prefix,
                    notification_cooldown_seconds,
                    created_at,
                    updated_at
                )
                values (?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    DEFAULT_TEMP_CHANNEL_PREFIX,
                    DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
                    now,
                    now,
                ),
            )
        return self._get_guild_config(guild_id)

    async def get_guild_config(self, guild_id: int) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_guild_config, guild_id)

    def _get_guild_config(self, guild_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "select * from guild_configs where guild_id = ?",
                (str(guild_id),),
            ).fetchone()

    async def reset_guild_configuration(self, guild_id: int) -> None:
        await asyncio.to_thread(self._reset_guild_configuration, guild_id)

    def _reset_guild_configuration(self, guild_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "delete from temp_vc_profiles where guild_id = ?",
                (str(guild_id),),
            )
            connection.execute(
                "delete from guild_configs where guild_id = ?",
                (str(guild_id),),
            )

    async def create_profile(
        self,
        guild_id: int,
        name: str,
        join_channel_id: int,
        target_category_id: int | None,
        created_by_user_id: int,
    ) -> sqlite3.Row:
        return await asyncio.to_thread(
            self._create_profile,
            guild_id,
            name,
            join_channel_id,
            target_category_id,
            created_by_user_id,
        )

    def _create_profile(
        self,
        guild_id: int,
        name: str,
        join_channel_id: int,
        target_category_id: int | None,
        created_by_user_id: int,
    ) -> sqlite3.Row:
        self._get_or_create_guild_config(guild_id)
        now = utc_now_iso()
        profile_id = str(uuid.uuid4())

        with self._connect() as connection:
            connection.execute(
                """
                insert into temp_vc_profiles (
                    id,
                    guild_id,
                    name,
                    join_channel_id,
                    target_category_id,
                    created_by_user_id,
                    created_at,
                    updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    str(guild_id),
                    name,
                    str(join_channel_id),
                    str(target_category_id) if target_category_id else None,
                    str(created_by_user_id),
                    now,
                    now,
                ),
            )

        return self._get_profile(profile_id)

    async def get_profile(self, profile_id: str) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_profile, profile_id)

    def _get_profile(self, profile_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "select * from temp_vc_profiles where id = ?",
                (profile_id,),
            ).fetchone()

    async def get_profile_by_name(self, guild_id: int, name: str) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_profile_by_name, guild_id, name)

    def _get_profile_by_name(self, guild_id: int, name: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                select * from temp_vc_profiles
                where guild_id = ? and lower(name) = lower(?)
                """,
                (str(guild_id), name),
            ).fetchone()

    async def get_profile_by_join_channel(
        self, guild_id: int, join_channel_id: int
    ) -> sqlite3.Row | None:
        return await asyncio.to_thread(
            self._get_profile_by_join_channel, guild_id, join_channel_id
        )

    def _get_profile_by_join_channel(
        self, guild_id: int, join_channel_id: int
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                select * from temp_vc_profiles
                where guild_id = ? and join_channel_id = ?
                """,
                (str(guild_id), str(join_channel_id)),
            ).fetchone()

    async def list_profiles(self, guild_id: int) -> Sequence[sqlite3.Row]:
        return await asyncio.to_thread(self._list_profiles, guild_id)

    def _list_profiles(self, guild_id: int) -> Sequence[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                select * from temp_vc_profiles
                where guild_id = ?
                order by created_at asc
                """,
                (str(guild_id),),
            ).fetchall()

    async def list_all_profiles(self) -> Sequence[sqlite3.Row]:
        return await asyncio.to_thread(self._list_all_profiles)

    def _list_all_profiles(self) -> Sequence[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "select * from temp_vc_profiles order by created_at asc"
            ).fetchall()

    async def delete_profile(self, profile_id: str) -> None:
        await asyncio.to_thread(self._delete_profile, profile_id)

    def _delete_profile(self, profile_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "delete from temp_vc_profiles where id = ?",
                (profile_id,),
            )

    async def create_active_temp_channel(
        self,
        channel_id: int,
        guild_id: int,
        profile_id: str,
        owner_user_id: int,
    ) -> None:
        await asyncio.to_thread(
            self._create_active_temp_channel, channel_id, guild_id, profile_id, owner_user_id
        )

    def _create_active_temp_channel(
        self,
        channel_id: int,
        guild_id: int,
        profile_id: str,
        owner_user_id: int,
    ) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                insert or replace into active_temp_channels (
                    channel_id,
                    guild_id,
                    profile_id,
                    owner_user_id,
                    created_at,
                    last_seen_at
                )
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(channel_id),
                    str(guild_id),
                    profile_id,
                    str(owner_user_id),
                    now,
                    now,
                ),
            )

    async def get_active_temp_channel(self, channel_id: int) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_active_temp_channel, channel_id)

    def _get_active_temp_channel(self, channel_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "select * from active_temp_channels where channel_id = ?",
                (str(channel_id),),
            ).fetchone()

    async def get_active_temp_channel_by_owner(
        self, guild_id: int, owner_user_id: int
    ) -> sqlite3.Row | None:
        return await asyncio.to_thread(
            self._get_active_temp_channel_by_owner, guild_id, owner_user_id
        )

    def _get_active_temp_channel_by_owner(
        self, guild_id: int, owner_user_id: int
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                select * from active_temp_channels
                where guild_id = ? and owner_user_id = ?
                limit 1
                """,
                (str(guild_id), str(owner_user_id)),
            ).fetchone()

    async def list_active_temp_channels(self, guild_id: int | None = None) -> Sequence[sqlite3.Row]:
        return await asyncio.to_thread(self._list_active_temp_channels, guild_id)

    def _list_active_temp_channels(self, guild_id: int | None = None) -> Sequence[sqlite3.Row]:
        with self._connect() as connection:
            if guild_id is None:
                return connection.execute(
                    "select * from active_temp_channels order by created_at asc"
                ).fetchall()

            return connection.execute(
                """
                select * from active_temp_channels
                where guild_id = ?
                order by created_at asc
                """,
                (str(guild_id),),
            ).fetchall()

    async def transfer_active_temp_channel_owner(self, channel_id: int, owner_user_id: int) -> None:
        await asyncio.to_thread(
            self._transfer_active_temp_channel_owner, channel_id, owner_user_id
        )

    def _transfer_active_temp_channel_owner(self, channel_id: int, owner_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                update active_temp_channels
                set owner_user_id = ?, last_seen_at = ?
                where channel_id = ?
                """,
                (str(owner_user_id), utc_now_iso(), str(channel_id)),
            )

    async def delete_active_temp_channel(self, channel_id: int) -> None:
        await asyncio.to_thread(self._delete_active_temp_channel, channel_id)

    def _delete_active_temp_channel(self, channel_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "delete from active_temp_channels where channel_id = ?",
                (str(channel_id),),
            )

    async def touch_active_temp_channel(self, channel_id: int) -> None:
        await asyncio.to_thread(self._touch_active_temp_channel, channel_id)

    def _touch_active_temp_channel(self, channel_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                update active_temp_channels
                set last_seen_at = ?
                where channel_id = ?
                """,
                (utc_now_iso(), str(channel_id)),
            )
