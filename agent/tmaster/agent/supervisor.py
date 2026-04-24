"""Sidecar supervisor.

Owns the lifecycle of each sidecar child process and maintains the UDS
connection used to exchange envelopes and binary frames. Bridges the sidecar
to the agent's routing logic via callbacks set from the agent main loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from tmaster.common import Envelope, get_logger
from tmaster.common.frames import BinaryFrame
from tmaster.common.uds import read_records, write_record

log = get_logger("supervisor")


OnRecord = Callable[[str, "Envelope | BinaryFrame"], Awaitable[None]]


class SidecarHandle:
    def __init__(
        self,
        *,
        workspace_id: str,
        socket_path: Path,
        on_record: OnRecord,
    ) -> None:
        self.workspace_id = workspace_id
        self.socket_path = socket_path
        self._on_record = on_record
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task[None]] = None
        self._send_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self.consecutive_failures = 0

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process else None

    async def start(
        self,
        *,
        binary: str,
        tmux_session: str,
        cwd: str,
        extra_env: Optional[dict[str, str]] = None,
    ) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Clean leftover socket if any
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)

        argv = [
            binary,
            "--workspace-id", self.workspace_id,
            "--socket", str(self.socket_path),
            "--tmux-session", tmux_session,
            "--cwd", cwd,
        ]
        log.info("spawning sidecar", workspace_id=self.workspace_id, argv=argv)
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Wait for the socket to appear (sidecar creates it on startup).
        await self._wait_for_socket(timeout_s=10.0)

        # Connect
        self._reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path))
        self._read_task = asyncio.create_task(self._read_loop(), name=f"sidecar-{self.workspace_id}")
        self._ready.set()

    async def _wait_for_socket(self, timeout_s: float) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self.socket_path.exists():
                return
            if self._process is not None and self._process.returncode is not None:
                err = b""
                if self._process.stderr:
                    err = await self._process.stderr.read()
                raise RuntimeError(
                    f"sidecar exited before socket ready (rc={self._process.returncode}): "
                    f"{err.decode(errors='replace')}"
                )
            await asyncio.sleep(0.05)
        raise TimeoutError(f"sidecar did not create {self.socket_path} in {timeout_s}s")

    async def _read_loop(self) -> None:
        try:
            assert self._reader is not None
            async for rec in read_records(self._reader):
                await self._on_record(self.workspace_id, rec)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sidecar read loop crashed", workspace_id=self.workspace_id)
        finally:
            log.info("sidecar read loop ended", workspace_id=self.workspace_id)

    async def send(self, record: Envelope | BinaryFrame) -> None:
        await self._ready.wait()
        assert self._writer is not None
        async with self._send_lock:
            await write_record(self._writer, record)

    async def stop(self, *, timeout_s: float = 5.0) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                try:
                    await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            finally:
                self._writer = None
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            self._process = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


class Supervisor:
    """Registry of running sidecars, keyed by workspace_id."""

    def __init__(self, on_record: OnRecord) -> None:
        self._on_record = on_record
        self._sidecars: dict[str, SidecarHandle] = {}

    def get(self, workspace_id: str) -> Optional[SidecarHandle]:
        return self._sidecars.get(workspace_id)

    def list_ids(self) -> list[str]:
        return list(self._sidecars.keys())

    async def start(
        self,
        *,
        workspace_id: str,
        socket_path: Path,
        binary: str,
        tmux_session: str,
        cwd: str,
    ) -> SidecarHandle:
        if workspace_id in self._sidecars:
            raise RuntimeError(f"sidecar already running for {workspace_id}")
        h = SidecarHandle(
            workspace_id=workspace_id, socket_path=socket_path, on_record=self._on_record
        )
        await h.start(binary=binary, tmux_session=tmux_session, cwd=cwd)
        self._sidecars[workspace_id] = h
        return h

    async def stop(self, workspace_id: str) -> None:
        h = self._sidecars.pop(workspace_id, None)
        if h is not None:
            await h.stop()

    async def stop_all(self) -> None:
        for wid in list(self._sidecars.keys()):
            await self.stop(wid)
