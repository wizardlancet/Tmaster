"""Hub routing tests — especially the multi-viewer stream_id remapping."""

from __future__ import annotations

import asyncio

import pytest

from tmaster.common import Envelope, FrameTag, MsgType, Scope
from tmaster.common.envelope import Ops
from tmaster.common.frames import BinaryFrame, decode_frame

from app.core.hub import AgentConn, DashboardConn, Hub


class Collector:
    def __init__(self) -> None:
        self.envs: list[Envelope] = []
        self.bins: list[bytes] = []

    async def send_env(self, env: Envelope) -> None:
        self.envs.append(env)

    async def send_bytes(self, data: bytes) -> None:
        self.bins.append(data)


@pytest.mark.asyncio
async def test_multi_viewer_stream_remap() -> None:
    hub = Hub()

    # Fake agent
    agent_out = Collector()
    agent = AgentConn(
        agent_id="a1", send_env=agent_out.send_env, send_bytes=agent_out.send_bytes
    )
    agent.workspaces = {"w1": {"id": "w1", "label": "ws", "status": "running"}}
    await hub.register_agent(agent)

    # Two dashboards subscribe and each open the same workspace tmux
    d1_out = Collector()
    d1 = DashboardConn("d1", "u1", d1_out.send_env, d1_out.send_bytes)
    d2_out = Collector()
    d2 = DashboardConn("d2", "u1", d2_out.send_env, d2_out.send_bytes)
    await hub.register_dashboard(d1)
    await hub.register_dashboard(d2)

    # Each dashboard picks its own stream_id space (they may collide).
    open1 = Envelope.req(
        scope=Scope.WORKSPACE, target="w1", op=Ops.TMUX_OPEN,
        payload={"stream_id": 42, "cols": 80, "rows": 24},
    )
    open2 = Envelope.req(
        scope=Scope.WORKSPACE, target="w1", op=Ops.TMUX_OPEN,
        payload={"stream_id": 42, "cols": 80, "rows": 24},
    )
    await hub.route_from_dashboard(d1, open1)
    await hub.route_from_dashboard(d2, open2)

    # Both should have been forwarded to the agent with *different* remapped
    # stream ids.
    forwarded = [e for e in agent_out.envs if e.op == Ops.TMUX_OPEN]
    assert len(forwarded) == 2
    sid_a = int(forwarded[0].payload["stream_id"])
    sid_b = int(forwarded[1].payload["stream_id"])
    assert sid_a != sid_b
    assert sid_a != 42 and sid_b != 42  # hub-allocated namespace is distinct

    # Agent replies with the remapped stream_id echoed back.
    resp_a = forwarded[0].reply(payload={"stream_id": sid_a})
    resp_b = forwarded[1].reply(payload={"stream_id": sid_b})
    await hub.route_from_agent(agent, resp_a)
    await hub.route_from_agent(agent, resp_b)

    # Each dashboard should have gotten its *original* stream_id back.
    assert any(
        e.type == MsgType.RESP and int(e.payload.get("stream_id", 0)) == 42
        for e in d1_out.envs
    )
    assert any(
        e.type == MsgType.RESP and int(e.payload.get("stream_id", 0)) == 42
        for e in d2_out.envs
    )

    # Now the agent emits PTY_OUT on sid_a — only d1 should receive it,
    # with its dashboard_stream_id (42).
    frame_a = BinaryFrame(tag=FrameTag.PTY_OUT, stream_id=sid_a, payload=b"hello-1")
    await hub.route_bytes_from_agent(agent, frame_a.encode())
    assert len(d1_out.bins) == 1
    assert len(d2_out.bins) == 0
    decoded = decode_frame(d1_out.bins[0])
    assert decoded.stream_id == 42
    assert decoded.payload == b"hello-1"

    # And sid_b fans out only to d2.
    frame_b = BinaryFrame(tag=FrameTag.PTY_OUT, stream_id=sid_b, payload=b"hi-2")
    await hub.route_bytes_from_agent(agent, frame_b.encode())
    assert len(d2_out.bins) == 1
    decoded2 = decode_frame(d2_out.bins[0])
    assert decoded2.stream_id == 42
    assert decoded2.payload == b"hi-2"

    # d1 typing keystrokes maps back to sid_a on the agent side.
    typed = BinaryFrame(tag=FrameTag.PTY_IN, stream_id=42, payload=b"x")
    await hub.route_bytes_from_dashboard(d1, typed.encode())
    assert len(agent_out.bins) >= 1
    agent_side = decode_frame(agent_out.bins[-1])
    assert agent_side.stream_id == sid_a
    assert agent_side.payload == b"x"
