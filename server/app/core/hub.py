"""Session Hub: in-memory registry of connected agents and dashboards, plus
the routing logic that glues the two sides of the server together.

Conceptually:

    * An *Agent connection* is identified by its `agent_id`. Each agent owns
      a set of workspace ids.
    * A *Dashboard connection* is identified by a synthetic `dashboard_id`.
      Dashboards may subscribe to workspaces to receive `event` traffic and
      can issue `req` envelopes targeting agents/workspaces.

The hub is transport-agnostic — it only sees the :class:`Envelope` objects
and raw binary frames. Per-hop `stream_id` remapping is done here so that a
sidecar that only knows its own id space can fan out to N dashboards with
different stream ids.
"""

from __future__ import annotations

import asyncio
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from tmaster.common import Envelope, FrameTag, MsgType, Scope, get_logger
from tmaster.common.envelope import ErrorDetail, Ops
from tmaster.common.frames import BinaryFrame, decode_frame

log = get_logger("hub")

SendEnvelope = Callable[[Envelope], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


@dataclass
class AgentConn:
    agent_id: str
    send_env: SendEnvelope
    send_bytes: SendBytes
    workspaces: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class DashboardConn:
    dashboard_id: str
    user_id: str
    send_env: SendEnvelope
    send_bytes: SendBytes
    # workspace_id subscribed for events
    subscriptions: set[str] = field(default_factory=set)


@dataclass
class StreamBridge:
    """One logical stream spans: dashboard_stream_id ↔ sidecar_stream_id.

    The hub allocates a unique id at the agent hop; `agent_stream_id` is what
    agents & sidecars see. `dashboard_stream_id` is what the specific
    dashboard allocates/sees.
    """
    agent_stream_id: int
    agent_id: str
    workspace_id: str
    dashboard_id: str
    dashboard_stream_id: int


class Hub:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConn] = {}
        self._dashboards: dict[str, DashboardConn] = {}
        # Pending req tracking for two-way routing of responses.
        # key = envelope.id sent on outer hop; value = (origin_kind, origin_id, origin_env_id)
        self._pending: dict[str, tuple[str, str, str]] = {}
        # Stream bridges indexed two ways:
        self._bridge_by_agent: dict[tuple[str, int], StreamBridge] = {}
        self._bridge_by_dashboard: dict[tuple[str, int], StreamBridge] = {}
        # Per-agent allocator
        self._next_stream_id: dict[str, int] = defaultdict(lambda: 1)
        self._lock = asyncio.Lock()

    # ---- registration --------------------------------------------------

    async def register_agent(self, conn: AgentConn) -> None:
        async with self._lock:
            if conn.agent_id in self._agents:
                # Boot the older connection; the new one wins.
                old = self._agents[conn.agent_id]
                log.warning("replacing agent connection", agent_id=conn.agent_id)
                # caller of the old side is responsible for its own cleanup;
                # here we just unregister.
                await self._unregister_agent_locked(old)
            self._agents[conn.agent_id] = conn
        log.info("agent registered", agent_id=conn.agent_id,
                 workspaces=list(conn.workspaces.keys()))
        await self._broadcast_workspaces_to_dashboards()

    async def unregister_agent(self, conn: AgentConn) -> None:
        async with self._lock:
            if self._agents.get(conn.agent_id) is conn:
                await self._unregister_agent_locked(conn)
        await self._broadcast_workspaces_to_dashboards()

    async def _unregister_agent_locked(self, conn: AgentConn) -> None:
        self._agents.pop(conn.agent_id, None)
        # Drop any stream bridges belonging to this agent.
        stale = [k for k in self._bridge_by_agent if k[0] == conn.agent_id]
        for k in stale:
            br = self._bridge_by_agent.pop(k)
            self._bridge_by_dashboard.pop((br.dashboard_id, br.dashboard_stream_id), None)
        log.info("agent unregistered", agent_id=conn.agent_id)

    async def register_dashboard(self, conn: DashboardConn) -> None:
        async with self._lock:
            self._dashboards[conn.dashboard_id] = conn
        log.info("dashboard registered", dashboard_id=conn.dashboard_id,
                 user_id=conn.user_id)
        # Push initial workspace list
        await conn.send_env(
            Envelope.event(
                scope=Scope.SERVER,
                op=Ops.WORKSPACE_LIST,
                payload={"workspaces": await self._snapshot_workspaces()},
            )
        )

    async def unregister_dashboard(self, conn: DashboardConn) -> None:
        async with self._lock:
            self._dashboards.pop(conn.dashboard_id, None)
            stale = [k for k in self._bridge_by_dashboard if k[0] == conn.dashboard_id]
            for k in stale:
                br = self._bridge_by_dashboard.pop(k)
                self._bridge_by_agent.pop((br.agent_id, br.agent_stream_id), None)

    # ---- agent state reporting ------------------------------------------

    async def update_agent_workspaces(
        self, agent_id: str, workspaces: list[dict[str, Any]]
    ) -> None:
        conn = self._agents.get(agent_id)
        if conn is None:
            return
        conn.workspaces = {w["id"]: w for w in workspaces}
        await self._broadcast_workspaces_to_dashboards()

    async def _snapshot_workspaces(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for agent in self._agents.values():
            for w in agent.workspaces.values():
                out.append({**w, "agent_id": agent.agent_id, "agent_online": True})
        return out

    async def _broadcast_workspaces_to_dashboards(self) -> None:
        snap = await self._snapshot_workspaces()
        evt = Envelope.event(
            scope=Scope.SERVER,
            op=Ops.WORKSPACE_LIST,
            payload={"workspaces": snap},
        )
        for d in list(self._dashboards.values()):
            try:
                await d.send_env(evt)
            except Exception:
                log.exception("failed to push workspaces", dashboard_id=d.dashboard_id)

    # ---- routing: dashboard -> agent/workspace --------------------------

    async def route_from_dashboard(self, conn: DashboardConn, env: Envelope) -> None:
        if env.scope == Scope.SERVER:
            await self._handle_server_scope(conn, env)
            return
        if env.scope == Scope.AGENT:
            agent_id = env.target
        elif env.scope == Scope.WORKSPACE:
            agent_id = self._find_agent_for_workspace(env.target or "")
        else:
            await conn.send_env(env.reply_error("bad_request", f"unknown scope {env.scope}"))
            return

        if not agent_id or agent_id not in self._agents:
            if env.type == MsgType.REQ:
                await conn.send_env(env.reply_error("unavailable", "agent offline"))
            return

        agent = self._agents[agent_id]

        # For tmux.open: allocate a stream bridge up front so the response
        # (which echoes stream_id) is remapped correctly.
        if env.scope == Scope.WORKSPACE and env.op == Ops.TMUX_OPEN:
            dash_sid = int(env.payload.get("stream_id") or 0)
            if dash_sid == 0:
                await conn.send_env(env.reply_error("bad_request", "tmux.open needs stream_id"))
                return
            agent_sid = self._alloc_agent_stream(agent_id)
            br = StreamBridge(
                agent_stream_id=agent_sid,
                agent_id=agent_id,
                workspace_id=env.target or "",
                dashboard_id=conn.dashboard_id,
                dashboard_stream_id=dash_sid,
            )
            self._bridge_by_agent[(agent_id, agent_sid)] = br
            self._bridge_by_dashboard[(conn.dashboard_id, dash_sid)] = br
            # Forward with rewritten stream_id
            env = env.model_copy(update={"payload": {**env.payload, "stream_id": agent_sid}})

        if env.type == MsgType.REQ:
            self._pending[env.id] = ("dashboard", conn.dashboard_id, env.id)
        try:
            await agent.send_env(env)
        except Exception as e:
            self._pending.pop(env.id, None)
            if env.type == MsgType.REQ:
                await conn.send_env(env.reply_error("internal", str(e)))

    async def _handle_server_scope(self, conn: DashboardConn, env: Envelope) -> None:
        if env.op == Ops.WORKSPACE_LIST and env.type == MsgType.REQ:
            await conn.send_env(
                env.reply(payload={"workspaces": await self._snapshot_workspaces()})
            )
        elif env.type == MsgType.REQ:
            await conn.send_env(env.reply_error("bad_request", f"unknown op {env.op}"))

    # ---- routing: agent -> dashboard ------------------------------------

    async def route_from_agent(self, conn: AgentConn, env: Envelope) -> None:
        # Agent events about workspaces: mirror into our registry
        if env.scope == Scope.AGENT and env.op == Ops.AGENT_WS_UPDATE and env.type == MsgType.EVENT:
            ws = env.payload.get("workspace")
            if ws:
                conn.workspaces[ws["id"]] = ws
                await self._broadcast_workspaces_to_dashboards()
            return

        # Bulk refresh of an agent's workspace map. Agents emit this on a
        # low-frequency timer to push runtime state (current_command, activity).
        if env.scope == Scope.AGENT and env.op == Ops.AGENT_WS_LIST and env.type == MsgType.EVENT:
            wss = env.payload.get("workspaces") or []
            conn.workspaces = {w["id"]: w for w in wss if "id" in w}
            await self._broadcast_workspaces_to_dashboards()
            return

        if env.type == MsgType.RESP:
            origin = self._pending.pop(env.in_reply_to or "", None)
            if origin is None:
                return
            kind, origin_id, _ = origin
            if kind == "dashboard":
                dash = self._dashboards.get(origin_id)
                if dash:
                    # Rewrite stream_id in tmux.open response (agent_sid -> dashboard_sid)
                    if env.scope == Scope.WORKSPACE and env.op == Ops.TMUX_OPEN and env.ok:
                        agent_sid = int(env.payload.get("stream_id") or 0)
                        br = self._bridge_by_agent.get((conn.agent_id, agent_sid))
                        if br:
                            env = env.model_copy(update={
                                "payload": {**env.payload, "stream_id": br.dashboard_stream_id},
                            })
                    await dash.send_env(env)
            return

        # Event from agent/workspace -> broadcast to subscribed dashboards
        if env.scope == Scope.WORKSPACE and env.target:
            for d in self._dashboards.values():
                if env.target in d.subscriptions:
                    try:
                        await d.send_env(env)
                    except Exception:
                        log.exception("dashboard send failed")

    # ---- binary frame routing -------------------------------------------

    async def route_bytes_from_agent(self, conn: AgentConn, frame_bytes: bytes) -> None:
        try:
            frame = decode_frame(frame_bytes)
        except Exception:
            log.exception("bad binary frame from agent")
            return
        br = self._bridge_by_agent.get((conn.agent_id, frame.stream_id))
        if br is None:
            return
        dash = self._dashboards.get(br.dashboard_id)
        if dash is None:
            return
        # Rewrite stream_id
        out = BinaryFrame(
            tag=frame.tag, stream_id=br.dashboard_stream_id, payload=frame.payload
        ).encode()
        try:
            await dash.send_bytes(out)
        except Exception:
            log.exception("dashboard send_bytes failed")
        if frame.tag == FrameTag.STREAM_CLOSE:
            self._bridge_by_agent.pop((conn.agent_id, frame.stream_id), None)
            self._bridge_by_dashboard.pop((br.dashboard_id, br.dashboard_stream_id), None)

    async def route_bytes_from_dashboard(
        self, conn: DashboardConn, frame_bytes: bytes
    ) -> None:
        try:
            frame = decode_frame(frame_bytes)
        except Exception:
            log.exception("bad binary frame from dashboard")
            return
        br = self._bridge_by_dashboard.get((conn.dashboard_id, frame.stream_id))
        if br is None:
            return
        agent = self._agents.get(br.agent_id)
        if agent is None:
            return
        out = BinaryFrame(
            tag=frame.tag, stream_id=br.agent_stream_id, payload=frame.payload
        ).encode()
        try:
            await agent.send_bytes(out)
        except Exception:
            log.exception("agent send_bytes failed")

    # ---- subscriptions ---------------------------------------------------

    async def subscribe_dashboard(self, conn: DashboardConn, workspace_id: str) -> None:
        conn.subscriptions.add(workspace_id)

    async def unsubscribe_dashboard(self, conn: DashboardConn, workspace_id: str) -> None:
        conn.subscriptions.discard(workspace_id)

    # ---- helpers ---------------------------------------------------------

    def _find_agent_for_workspace(self, workspace_id: str) -> Optional[str]:
        for aid, a in self._agents.items():
            if workspace_id in a.workspaces:
                return aid
        return None

    def _alloc_agent_stream(self, agent_id: str) -> int:
        sid = self._next_stream_id[agent_id]
        self._next_stream_id[agent_id] = sid + 1
        return sid

    def new_dashboard_id(self) -> str:
        return secrets.token_hex(6)


# Module-level singleton accessor used by the FastAPI app state.
def make_hub() -> Hub:
    return Hub()
