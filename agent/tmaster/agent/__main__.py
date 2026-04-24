"""The TMaster agent daemon.

Responsibilities:
    * Establish and maintain an outbound WSS connection to the server.
    * Own a local SQLite registry of managed workspaces.
    * Fulfil server requests: agent.ping, agent.workspace.create / kill / list.
    * Spawn and supervise one sidecar per workspace; route server traffic to
      the right sidecar, and sidecar traffic back to the server.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from tmaster.common import (
    PROTOCOL_VERSION,
    Envelope,
    MsgType,
    Scope,
    configure_logging,
    get_logger,
)
from tmaster.common.envelope import Ops
from tmaster.common.frames import BinaryFrame

from tmaster.agent.config import AgentSettings, load as load_settings
from tmaster.agent.registry import Registry, WorkspaceRecord
from tmaster.agent.supervisor import Supervisor
from tmaster.agent.tmux import Tmux

log = get_logger("agent")


class Agent:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.registry = Registry(settings.state_dir / "agent.db")
        self.tmux = Tmux(settings.tmux_bin)
        self.supervisor = Supervisor(self._on_sidecar_record)
        self._ws: Optional[WebSocketClientProtocol] = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        # stream_id -> workspace_id (used to route binary frames in both directions)
        self._stream_to_workspace: dict[int, str] = {}

    # ---- lifecycle ------------------------------------------------------

    async def run(self) -> None:
        await self.registry.connect()
        await self._reconcile_on_start()

        backoff = self.settings.reconnect_initial_backoff_s
        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                backoff = self.settings.reconnect_initial_backoff_s
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("server connection lost", error=str(e))
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.settings.reconnect_max_backoff_s)

        await self.supervisor.stop_all()
        await self.registry.close()

    def stop(self) -> None:
        self._stop.set()

    async def _reconcile_on_start(self) -> None:
        """Inspect registry entries and clean up anything that's dead."""
        recs = await self.registry.list_all()
        for r in recs:
            alive = await self.tmux.has_session(r.tmux_session_name)
            if not alive:
                log.info("removing stale workspace", workspace_id=r.id)
                await self.registry.delete(r.id)
                continue
            # We do not auto-restart sidecars across agent restarts; sidecar
            # will be spawned on demand the next time the server or a
            # dashboard requests something for this workspace.
            r.sidecar_pid = None
            r.sidecar_sock = None
            r.status = "idle"
            r.updated_at = self.registry.now()
            await self.registry.upsert(r)

    # ---- connection -----------------------------------------------------

    async def _connect_and_serve(self) -> None:
        if not self.settings.agent_id or not self.settings.agent_token:
            raise RuntimeError(
                "Agent not enrolled. Set TMASTER_AGENT_AGENT_ID and TMASTER_AGENT_AGENT_TOKEN "
                "after POST /api/agents/enroll."
            )
        url = (
            f"{self.settings.server_url.rstrip('/')}/ws/agent"
            f"?agent_id={self.settings.agent_id}&token={self.settings.agent_token}"
        )
        ssl_ctx: Any = None
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if self.settings.tls_insecure:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        log.info("connecting to server", url=url.split("?")[0])
        async with websockets.connect(
            url,
            subprotocols=["tmaster.agent.v1"],
            ssl=ssl_ctx,
            ping_interval=self.settings.heartbeat_interval_s,
            ping_timeout=self.settings.heartbeat_interval_s * 2,
            max_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            try:
                await self._handshake(ws)
                await self._serve(ws)
            finally:
                self._ws = None

    async def _handshake(self, ws: WebSocketClientProtocol) -> None:
        recs = await self.registry.list_all()
        hello = {
            "type": "hello",
            "proto": PROTOCOL_VERSION,
            "client": "agent",
            "version": "0.0.1",
            "agent_id": self.settings.agent_id,
            "machine": {
                "hostname": self.settings.machine_name or socket.gethostname(),
                "tmux_version": await self.tmux.server_version(),
            },
            "workspaces": [r.to_wire() for r in recs],
        }
        await ws.send(json.dumps(hello))
        ack_raw = await ws.recv()
        ack = json.loads(ack_raw)
        if ack.get("type") != "hello_ack" or ack.get("proto") != PROTOCOL_VERSION:
            raise RuntimeError(f"bad hello_ack: {ack}")
        log.info("handshake complete", agent_id=self.settings.agent_id)

    async def _serve(self, ws: WebSocketClientProtocol) -> None:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    env = Envelope.model_validate_json(msg)
                except Exception:
                    log.exception("bad envelope from server")
                    continue
                await self._handle_server_envelope(env)
            else:
                await self._handle_server_binary(msg)

    # ---- from server ----------------------------------------------------

    async def _handle_server_envelope(self, env: Envelope) -> None:
        if env.scope == Scope.AGENT:
            await self._handle_agent_scope(env)
            return
        if env.scope == Scope.WORKSPACE:
            # Route to sidecar; spawn on demand if needed.
            wid = env.target or ""
            h = self.supervisor.get(wid)
            if h is None:
                rec = await self.registry.get(wid)
                if rec is None:
                    if env.type == MsgType.REQ:
                        await self._send(env.reply_error("not_found", "unknown workspace"))
                    return
                try:
                    h = await self._spawn_sidecar_for(rec)
                except Exception as e:
                    log.exception("failed to spawn sidecar", workspace_id=wid)
                    if env.type == MsgType.REQ:
                        await self._send(env.reply_error("unavailable", str(e)))
                    return
            # Track stream_id -> workspace for frame routing.
            if env.op in (Ops.TMUX_OPEN, Ops.FS_READ, Ops.FS_WRITE, Ops.FS_DOWNLOAD, Ops.FS_UPLOAD):
                sid = env.payload.get("stream_id")
                if isinstance(sid, int):
                    self._stream_to_workspace[sid] = wid
            if env.op == Ops.TMUX_CLOSE:
                sid = env.payload.get("stream_id")
                if isinstance(sid, int):
                    self._stream_to_workspace.pop(sid, None)
            await h.send(env)

    async def _handle_server_binary(self, data: bytes) -> None:
        from tmaster.common.frames import FrameTag, decode_frame
        try:
            frame = decode_frame(data)
        except Exception:
            log.exception("bad binary frame from server")
            return
        wid = self._stream_to_workspace.get(frame.stream_id)
        if wid is None:
            log.warning("no sidecar for stream", stream_id=frame.stream_id)
            return
        h = self.supervisor.get(wid)
        if h is None:
            return
        await h.send(frame)
        if frame.tag == FrameTag.STREAM_CLOSE:
            self._stream_to_workspace.pop(frame.stream_id, None)

    async def _handle_agent_scope(self, env: Envelope) -> None:
        if env.op == Ops.AGENT_PING and env.type == MsgType.REQ:
            await self._send(env.reply(payload={"uptime_s": int(time.time())}))
            return
        if env.op == Ops.AGENT_WS_LIST and env.type == MsgType.REQ:
            recs = await self.registry.list_all()
            await self._send(env.reply(payload={"workspaces": [r.to_wire() for r in recs]}))
            return
        if env.op == Ops.AGENT_WS_CREATE and env.type == MsgType.REQ:
            try:
                rec = await self._create_workspace(
                    label=env.payload.get("label") or "workspace",
                    cwd=env.payload.get("cwd") or str(self.settings.default_workspace_cwd),
                    config=env.payload.get("config"),
                )
                await self._send(env.reply(payload={"workspace": rec.to_wire()}))
            except Exception as e:
                log.exception("workspace create failed")
                await self._send(env.reply_error("internal", str(e)))
            return
        if env.op == Ops.AGENT_WS_KILL and env.type == MsgType.REQ:
            try:
                await self._kill_workspace(env.payload["workspace_id"])
                await self._send(env.reply(payload={}))
            except Exception as e:
                await self._send(env.reply_error("internal", str(e)))
            return
        if env.type == MsgType.REQ:
            await self._send(env.reply_error("bad_request", f"unknown op {env.op}"))

    # ---- workspace lifecycle --------------------------------------------

    async def _create_workspace(
        self, *, label: str, cwd: str, config: Optional[dict[str, Any]]
    ) -> WorkspaceRecord:
        ws_id = uuid.uuid4().hex[:12]
        tmux_name = f"{self.settings.session_prefix}{ws_id[:8]}"
        await self.tmux.new_session(tmux_name, cwd=cwd)
        now = self.registry.now()
        rec = WorkspaceRecord(
            id=ws_id,
            tmux_session_name=tmux_name,
            label=label,
            cwd=cwd,
            config=config,
            sidecar_pid=None,
            sidecar_sock=None,
            status="idle",
            created_at=now,
            updated_at=now,
        )
        await self.registry.upsert(rec)
        await self._emit_workspace_update(rec)
        return rec

    async def _kill_workspace(self, ws_id: str) -> None:
        await self.supervisor.stop(ws_id)
        rec = await self.registry.get(ws_id)
        if rec:
            await self.tmux.kill_session(rec.tmux_session_name)
        await self.registry.delete(ws_id)
        # Fire deletion event
        evt = Envelope.event(
            scope=Scope.AGENT,
            op=Ops.AGENT_WS_UPDATE,
            payload={"workspace": {"id": ws_id, "status": "deleted"}},
        )
        await self._send(evt)

    async def _emit_workspace_update(self, rec: WorkspaceRecord) -> None:
        evt = Envelope.event(
            scope=Scope.AGENT,
            op=Ops.AGENT_WS_UPDATE,
            payload={"workspace": rec.to_wire()},
        )
        await self._send(evt)

    async def _spawn_sidecar_for(self, rec: WorkspaceRecord):
        sock_path = self.settings.runtime_dir / f"ws-{rec.id}.sock"
        h = await self.supervisor.start(
            workspace_id=rec.id,
            socket_path=sock_path,
            binary=self.settings.resolve_sidecar_bin(),
            tmux_session=rec.tmux_session_name,
            cwd=rec.cwd or str(self.settings.default_workspace_cwd),
        )
        rec.sidecar_pid = h.pid
        rec.sidecar_sock = str(sock_path)
        rec.status = "running"
        rec.updated_at = self.registry.now()
        await self.registry.upsert(rec)
        await self._emit_workspace_update(rec)
        return h

    # ---- from sidecar ---------------------------------------------------

    async def _on_sidecar_record(self, workspace_id: str, record) -> None:
        # Envelopes and binary frames from sidecar flow straight out to the
        # server. Binary frames keep their stream_id; control-plane
        # envelopes retain their scope/target as set by the sidecar.
        if isinstance(record, Envelope):
            await self._send(record)
        else:
            await self._send_bytes(record.encode())

    # ---- outbound to server ---------------------------------------------

    async def _send(self, env: Envelope) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            try:
                await ws.send(env.model_dump_json(exclude_none=True))
            except Exception:
                log.exception("send envelope failed")

    async def _send_bytes(self, data: bytes) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            try:
                await ws.send(data)
            except Exception:
                log.exception("send bytes failed")


def main() -> None:
    import signal
    configure_logging("agent")
    settings = load_settings()
    agent = Agent(settings)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig(*_a):
        agent.stop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _sig)

    try:
        loop.run_until_complete(agent.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
