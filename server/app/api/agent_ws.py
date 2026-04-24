"""Agent-facing WebSocket endpoint.

Protocol handshake (see docs/protocol.md §1):

1. Client connects with `?agent_id=...&token=...`.
2. Server verifies token, sends {"type":"hello_ack","proto":1}.
3. Agent sends its `hello` envelope (with workspaces snapshot).
4. Steady-state: JSON envelopes (control plane) and binary frames share one
   WebSocket connection — standard WS message type selects which.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from tmaster.common import Envelope, PROTOCOL_VERSION, get_logger
from app.core.hub import AgentConn, Hub

log = get_logger("ws.agent")


def build_router(settings, store, hub: Hub) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/agent")
    async def agent_ws(
        websocket: WebSocket,
        agent_id: str = Query(...),
        token: str = Query(...),
    ) -> None:
        if not await store.verify_agent_token(agent_id, token):
            await websocket.close(code=4401, reason="unauthenticated")
            return
        await websocket.accept(subprotocol="tmaster.agent.v1")

        # Expect hello frame
        try:
            hello_raw = await websocket.receive_text()
            hello = json.loads(hello_raw)
            if hello.get("type") != "hello" or hello.get("proto") != PROTOCOL_VERSION:
                await websocket.close(code=4001, reason="proto_mismatch")
                return
        except Exception:
            await websocket.close(code=4001, reason="bad handshake")
            return

        await websocket.send_text(
            json.dumps({"type": "hello_ack", "proto": PROTOCOL_VERSION, "agent_id": agent_id})
        )
        await store.touch_agent(agent_id)

        # Build conn
        async def send_env(env: Envelope) -> None:
            await websocket.send_text(env.model_dump_json(exclude_none=True))

        async def send_bytes(data: bytes) -> None:
            await websocket.send_bytes(data)

        workspaces = {w["id"]: w for w in hello.get("workspaces", [])}
        conn = AgentConn(
            agent_id=agent_id, send_env=send_env, send_bytes=send_bytes, workspaces=workspaces
        )
        await hub.register_agent(conn)

        # Persist workspaces in DB
        import time
        for ws in workspaces.values():
            await store.upsert_workspace(
                id=ws["id"],
                agent_id=agent_id,
                tmux_session_name=ws.get("tmux_session_name", ws["id"]),
                label=ws.get("label", ws["id"]),
                cwd=ws.get("cwd"),
                config_json=None,
                status=ws.get("status", "unknown"),
                health_json=None,
                created_at=int(ws.get("created_at", time.time())),
                last_seen_at=int(time.time()),
            )

        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    try:
                        env = Envelope.model_validate_json(msg["text"])
                    except Exception:
                        log.exception("bad envelope from agent", agent_id=agent_id)
                        continue
                    await hub.route_from_agent(conn, env)
                elif "bytes" in msg and msg["bytes"] is not None:
                    await hub.route_bytes_from_agent(conn, msg["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("agent ws loop crashed", agent_id=agent_id)
        finally:
            await hub.unregister_agent(conn)
            log.info("agent disconnected", agent_id=agent_id)

    return router
