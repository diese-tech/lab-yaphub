"""Create and verify a WAL-consistent backup before a recovery rollout."""

import argparse
import asyncio
import hashlib
import sqlite3
from pathlib import Path

from storage import Storage


async def backup(database, output):
    if not await asyncio.to_thread(Path(database).is_file):
        raise FileNotFoundError(database)
    path = await Storage(database).backup(output)
    def verify():
        with sqlite3.connect(path) as connection:
            tables = connection.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
            if tables == 0:
                raise RuntimeError("Backup contains no tables")
        return tables, hashlib.sha256(Path(path).read_bytes()).hexdigest()
    tables, checksum = await asyncio.to_thread(verify)
    print(f"backup_verified path={path} sha256={checksum} tables={tables}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    asyncio.run(backup(args.database, args.output))
