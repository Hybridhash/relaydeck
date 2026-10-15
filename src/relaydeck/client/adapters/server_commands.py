"""Owner-private Mac storage for payload-free hosted command identities."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

from relaydeck.client.adapters.ports import CredentialStore


class ServerCommandStore(CredentialStore):
    def __init__(self, path: Path) -> None:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError("Pending command file must be owner-private.")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pending (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.commit()

    async def available(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM pending WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    async def set(self, key: str, value: str) -> None:
        if len(value.encode()) > 131072 or len(key) != 72 or not key.startswith("pending_"):
            raise ValueError("Invalid pending command record")
        if (
            await self.get(key) is None
            and self.db.execute("SELECT count(*) FROM pending").fetchone()[0] >= 1024
        ):
            raise ValueError("Pending command scope capacity reached")
        self.db.execute("INSERT OR REPLACE INTO pending VALUES (?,?)", (key, value))
        self.db.commit()

    async def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM pending WHERE key=?", (key,))
        self.db.commit()

    async def clear_account(self) -> None:
        raise RuntimeError("Hosted cleanup must use the authenticated scope")

    def close(self) -> None:
        self.db.close()
