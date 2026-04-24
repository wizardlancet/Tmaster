"""SQLite-backed storage layer for the server.

Only the MVP surface is here. More tables (audit events, workspace metadata
history) are added in later phases without schema migrations needing to touch
this module — see :func:`_SCHEMA`.
"""

from __future__ import annotations

import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_HASHER = PasswordHasher()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL,
    last_seen_at  INTEGER,
    created_at    INTEGER NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_by    TEXT
);

CREATE TABLE IF NOT EXISTS workspaces (
    id                 TEXT PRIMARY KEY,
    agent_id           TEXT NOT NULL,
    tmux_session_name  TEXT NOT NULL,
    label              TEXT NOT NULL,
    cwd                TEXT,
    config_json        TEXT,
    status             TEXT NOT NULL,
    health_json        TEXT,
    created_at         INTEGER NOT NULL,
    last_seen_at       INTEGER NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    agent_id     TEXT,
    workspace_id TEXT,
    user_id      TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Store.connect() must be called first"
        return self._conn

    # -------- users & auth -----------------------------------------------

    async def create_user(self, username: str, password: str) -> str:
        uid = secrets.token_hex(8)
        hashed = _HASHER.hash(password)
        await self.conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (uid, username, hashed, int(time.time())),
        )
        await self.conn.commit()
        return uid

    async def count_users(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def verify_user(self, username: str, password: str) -> Optional[str]:
        async with self.conn.execute(
            "SELECT id, password_hash FROM users WHERE username=?", (username,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        uid, phash = row
        try:
            _HASHER.verify(phash, password)
            return str(uid)
        except VerifyMismatchError:
            return None

    # -------- enrollment & agent tokens ----------------------------------

    async def create_enrollment_token(self, ttl_seconds: int = 3600) -> str:
        token = secrets.token_urlsafe(24)
        th = _token_hash(token)
        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO enrollment_tokens (token_hash, created_at, expires_at) VALUES (?,?,?)",
            (th, now, now + ttl_seconds),
        )
        await self.conn.commit()
        return token

    async def consume_enrollment_token(self, token: str, *, agent_name: str) -> Optional[tuple[str, str]]:
        """Consume an enrollment token, return (agent_id, long_lived_token) on success."""
        th = _token_hash(token)
        now = int(time.time())
        async with self.conn.execute(
            "SELECT expires_at, used_by FROM enrollment_tokens WHERE token_hash=?", (th,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        expires_at, used_by = row
        if used_by is not None or expires_at < now:
            return None
        agent_id = secrets.token_hex(8)
        long_token = secrets.token_urlsafe(32)
        long_hash = _token_hash(long_token)
        await self.conn.execute(
            "INSERT INTO agents (id, name, token_hash, created_at) VALUES (?,?,?,?)",
            (agent_id, agent_name, long_hash, now),
        )
        await self.conn.execute(
            "UPDATE enrollment_tokens SET used_by=? WHERE token_hash=?", (agent_id, th)
        )
        await self.conn.commit()
        return agent_id, long_token

    async def verify_agent_token(self, agent_id: str, token: str) -> bool:
        th = _token_hash(token)
        async with self.conn.execute(
            "SELECT 1 FROM agents WHERE id=? AND token_hash=?", (agent_id, th)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def touch_agent(self, agent_id: str) -> None:
        await self.conn.execute(
            "UPDATE agents SET last_seen_at=? WHERE id=?", (int(time.time()), agent_id)
        )
        await self.conn.commit()

    async def list_agents(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT id, name, last_seen_at, created_at FROM agents ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "last_seen_at": r[2], "created_at": r[3]}
            for r in rows
        ]

    # -------- workspaces --------------------------------------------------

    async def upsert_workspace(self, **fields: Any) -> None:
        cols = list(fields.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        await self.conn.execute(
            f"INSERT INTO workspaces ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            tuple(fields[c] for c in cols),
        )
        await self.conn.commit()

    async def delete_workspace(self, workspace_id: str) -> None:
        await self.conn.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
        await self.conn.commit()

    async def list_workspaces(self, *, agent_id: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT id, agent_id, tmux_session_name, label, cwd, config_json, status, health_json, created_at, last_seen_at FROM workspaces"
        args: tuple[Any, ...] = ()
        if agent_id:
            q += " WHERE agent_id=?"
            args = (agent_id,)
        q += " ORDER BY created_at"
        async with self.conn.execute(q, args) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "agent_id": r[1],
                    "tmux_session_name": r[2],
                    "label": r[3],
                    "cwd": r[4],
                    "config": json.loads(r[5]) if r[5] else None,
                    "status": r[6],
                    "health": json.loads(r[7]) if r[7] else None,
                    "created_at": r[8],
                    "last_seen_at": r[9],
                }
            )
        return out

    # -------- events ------------------------------------------------------

    async def record_event(
        self,
        kind: str,
        *,
        agent_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        user_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO events (ts, kind, agent_id, workspace_id, user_id, payload_json) VALUES (?,?,?,?,?,?)",
            (
                int(time.time() * 1000),
                kind,
                agent_id,
                workspace_id,
                user_id,
                json.dumps(payload) if payload else None,
            ),
        )
        await self.conn.commit()


def _token_hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@asynccontextmanager
async def store_lifespan(path: Path) -> AsyncIterator[Store]:
    s = Store(path)
    await s.connect()
    try:
        yield s
    finally:
        await s.close()
