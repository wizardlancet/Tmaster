from __future__ import annotations

import pytest

from tmaster.common import (
    Envelope,
    FrameTag,
    MsgType,
    Scope,
    decode_frame,
    encode_frame,
)
from tmaster.common.envelope import Ops
from tmaster.common.frames import (
    decode_file_chunk,
    decode_resize,
    encode_file_chunk,
    encode_resize,
)


def test_envelope_req_roundtrip() -> None:
    env = Envelope.req(
        scope=Scope.WORKSPACE,
        target="ws-1",
        op=Ops.TMUX_OPEN,
        payload={"cols": 120, "rows": 40},
    )
    raw = env.model_dump_json()
    parsed = Envelope.model_validate_json(raw)
    assert parsed.type is MsgType.REQ
    assert parsed.op == Ops.TMUX_OPEN
    assert parsed.target == "ws-1"
    assert parsed.payload["cols"] == 120


def test_envelope_reply_ok() -> None:
    req = Envelope.req(scope=Scope.AGENT, op=Ops.AGENT_PING)
    resp = req.reply(payload={"uptime_s": 42})
    assert resp.type is MsgType.RESP
    assert resp.in_reply_to == req.id
    assert resp.ok is True
    assert resp.payload == {"uptime_s": 42}


def test_envelope_reply_error() -> None:
    req = Envelope.req(scope=Scope.WORKSPACE, target="x", op=Ops.FS_READ)
    err = req.reply_error("not_found", "nope", path="/etc/shadow")
    assert err.ok is False
    assert err.error is not None
    assert err.error.code == "not_found"
    assert err.error.details == {"path": "/etc/shadow"}


def test_envelope_workspace_requires_target() -> None:
    with pytest.raises(ValueError):
        Envelope(
            type=MsgType.REQ,
            scope=Scope.WORKSPACE,
            op=Ops.FS_LIST,
            payload={"path": "/"},
        )


def test_envelope_resp_requires_ok() -> None:
    with pytest.raises(ValueError):
        Envelope(
            type=MsgType.RESP,
            scope=Scope.AGENT,
            op=Ops.AGENT_PING,
            in_reply_to="abc",
        )


def test_frame_roundtrip() -> None:
    raw = encode_frame(FrameTag.PTY_OUT, 7, b"hello world")
    f = decode_frame(raw)
    assert f.tag is FrameTag.PTY_OUT
    assert f.stream_id == 7
    assert f.payload == b"hello world"


def test_frame_resize_helpers() -> None:
    raw = encode_resize(3, 132, 50)
    cols, rows = decode_resize(decode_frame(raw))
    assert (cols, rows) == (132, 50)


def test_frame_file_chunk_helpers() -> None:
    raw = encode_file_chunk(9, 4096, b"abcdef")
    offset, data = decode_file_chunk(decode_frame(raw))
    assert offset == 4096
    assert data == b"abcdef"
