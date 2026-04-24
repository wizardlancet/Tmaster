"""Dashboard-facing WebSocket.

Clients must authenticate with a JWT access token (via `?token=`). One WS
connection multiplexes control-plane envelopes and binary frames.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from tmaster.common import Envelope, PROTOCOL_VERSION, Scope, get_logger
from tmaster.common.envelope import MsgType
from app.core.auth import decode_token
from app.core.hub import DashboardConn, Hub

log = get_logger("ws.dashboard")


def build_router(settings, hub: Hub) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/dashboard")
    async def dashboard_ws(
        websocket: WebSocket,
        token: str = Query(...),
    ) -> None:
        try:
            user_id = decode_token(settings, token, expected_type="access")
        except Exception:
            await websocket.close(code=4401, reason="unauthenticated")
            return

        await websocket.accept(subprotocol="tmaster.dashboard.v1")
        try:
            hello_raw = await websocket.receive_text()
            hello = json.loads(hello_raw)
            if hello.get("type") != "hello" or hello.get("proto") != PROTOCOL_VERSION:
                await websocket.close(code=4001, reason="proto_mismatch")
                return
        except Exception:
            await websocket.close(code=4001, reason="bad handshake")
            return

        dashboard_id = hub.new_dashboard_id()
        await websocket.send_text(
            json.dumps({
                "type": "hello_ack",
                "proto": PROTOCOL_VERSION,
                "dashboard_id": dashboard_id,
                "user_id": user_id,
            })
        )

        async def send_env(env: Envelope) -> None:
            await websocket.send_text(env.model_dump_json(exclude_none=True))

        async def send_bytes(data: bytes) -> None:
            await websocket.send_bytes(data)

        conn = DashboardConn(
            dashboard_id=dashboard_id,
            user_id=user_id,
            send_env=send_env,
            send_bytes=send_bytes,
        )
        await hub.register_dashboard(conn)

        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    try:
                        env = Envelope.model_validate_json(msg["text"])
                    except Exception:
                        log.exception("bad envelope from dashboard", dashboard_id=dashboard_id)
                        continue
                    # Subscription management is server-local.
                    if env.scope == Scope.SERVER and env.op == "subscribe":
                        wid = env.payload.get("workspace_id")
                        if wid:
                            await hub.subscribe_dashboard(conn, wid)
                        if env.type == MsgType.REQ:
                            await conn.send_env(env.reply())
                        continue
                    if env.scope == Scope.SERVER and env.op == "unsubscribe":
                        wid = env.payload.get("workspace_id")
                        if wid:
                            await hub.unsubscribe_dashboard(conn, wid)
                        if env.type == MsgType.REQ:
                            await conn.send_env(env.reply())
                        continue
                    await hub.route_from_dashboard(conn, env)
                elif "bytes" in msg and msg["bytes"] is not None:
                    await hub.route_bytes_from_dashboard(conn, msg["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("dashboard ws loop crashed", dashboard_id=dashboard_id)
        finally:
            await hub.unregister_dashboard(conn)

    return router
