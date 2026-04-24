"""Agent-local SQLite registry of managed workspaces."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id                TEXT PRIMARY KEY,
    tmux_session_name TEXT NOT NULL,
    label             TEXT NOT NULL,
    cwd               TEXT,
    config_json       TEXT,
    sidecar_pid       INTEGER,
    sidecar_sock      TEXT,
    status            TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
"""


@dataclass
class WorkspaceRecord:
    id: str
    tmux_session_name: str
    label: str
    cwd: Optional[str]
    config: Optional[dict[str, Any]]
    sidecar_pid: Optional[int]
    sidecar_sock: Optional[str]
    status: str
    created_at: int
    updated_at: int

    def to_wire(self, runtime: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "tmux_session_name": self.tmux_session_name,
            "label": self.label,
            "cwd": self.cwd,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if runtime:
            # Optional per-list runtime metadata; never persisted.
            for k in (
                "current_command",
                "current_pid",
                "activity",
                "last_activity_at",
            ):
                if k in runtime:
                    d[k] = runtime[k]
        return d


class Registry:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn

    async def upsert(self, rec: WorkspaceRecord) -> None:
        await self.conn.execute(
            """
            INSERT INTO workspaces (id, tmux_session_name, label, cwd, config_json,
                sidecar_pid, sidecar_sock, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                tmux_session_name=excluded.tmux_session_name,
                label=excluded.label,
                cwd=excluded.cwd,
                config_json=excluded.config_json,
                sidecar_pid=excluded.sidecar_pid,
                sidecar_sock=excluded.sidecar_sock,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                rec.id,
                rec.tmux_session_name,
                rec.label,
                rec.cwd,
                json.dumps(rec.config) if rec.config else None,
                rec.sidecar_pid,
                rec.sidecar_sock,
                rec.status,
                rec.created_at,
                rec.updated_at,
            ),
        )
        await self.conn.commit()

    async def delete(self, ws_id: str) -> None:
        await self.conn.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        await self.conn.commit()

    async def list_all(self) -> list[WorkspaceRecord]:
        async with self.conn.execute(
            "SELECT id, tmux_session_name, label, cwd, config_json, sidecar_pid, "
            "sidecar_sock, status, created_at, updated_at FROM workspaces"
        ) as cur:
            rows = await cur.fetchall()
        return [
            WorkspaceRecord(
                id=r[0],
                tmux_session_name=r[1],
                label=r[2],
                cwd=r[3],
                config=json.loads(r[4]) if r[4] else None,
                sidecar_pid=r[5],
                sidecar_sock=r[6],
                status=r[7],
                created_at=r[8],
                updated_at=r[9],
            )
            for r in rows
        ]

    async def get(self, ws_id: str) -> Optional[WorkspaceRecord]:
        async with self.conn.execute(
            "SELECT id, tmux_session_name, label, cwd, config_json, sidecar_pid, "
            "sidecar_sock, status, created_at, updated_at FROM workspaces WHERE id=?",
            (ws_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return WorkspaceRecord(
            id=row[0],
            tmux_session_name=row[1],
            label=row[2],
            cwd=row[3],
            config=json.loads(row[4]) if row[4] else None,
            sidecar_pid=row[5],
            sidecar_sock=row[6],
            status=row[7],
            created_at=row[8],
            updated_at=row[9],
        )

    def now(self) -> int:
        return int(time.time())
