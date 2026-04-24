from __future__ import annotations

import asyncio

import pytest

from tmaster.common.envelope import Envelope, Ops, Scope
from tmaster.common.frames import FrameTag, encode_frame
from tmaster.common.uds import (
    encode_binary,
    encode_envelope,
    read_records,
    write_record,
)
from tmaster.common.frames import BinaryFrame


class _MemoryReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._i = 0

    async def read(self, n: int) -> bytes:
        if self._i >= len(self._data):
            return b""
        chunk = self._data[self._i : self._i + n]
        self._i += len(chunk)
        return chunk

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._i : self._i + n]
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(partial=chunk, expected=n)
        self._i += n
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._data.find(sep, self._i)
        if idx < 0:
            raise asyncio.IncompleteReadError(partial=self._data[self._i :], expected=None)
        chunk = self._data[self._i : idx + len(sep)]
        self._i = idx + len(sep)
        return chunk


async def test_uds_codec_mixed_stream() -> None:
    env1 = Envelope.req(scope=Scope.AGENT, op=Ops.AGENT_PING)
    frame = BinaryFrame(tag=FrameTag.PTY_OUT, stream_id=1, payload=b"hi")
    env2 = Envelope.event(scope=Scope.WORKSPACE, target="w", op=Ops.TMUX_STATE,
                          payload={"windows": []})

    buf = encode_envelope(env1) + encode_binary(frame) + encode_envelope(env2)
    reader = _MemoryReader(buf)

    records = []
    async for rec in read_records(reader):  # type: ignore[arg-type]
        records.append(rec)

    assert len(records) == 3
    assert isinstance(records[0], Envelope) and records[0].op == Ops.AGENT_PING
    assert isinstance(records[1], BinaryFrame) and records[1].payload == b"hi"
    assert isinstance(records[2], Envelope) and records[2].op == Ops.TMUX_STATE


@pytest.mark.asyncio
async def test_uds_write_drains() -> None:
    class _W:
        def __init__(self) -> None:
            self.buf = bytearray()
        def write(self, data: bytes) -> None:
            self.buf.extend(data)
        async def drain(self) -> None:
            return

    w = _W()
    env = Envelope.req(scope=Scope.AGENT, op=Ops.AGENT_PING)
    await write_record(w, env)  # type: ignore[arg-type]
    assert w.buf.endswith(b"\n")
    assert b'"op":"agent.ping"' in bytes(w.buf)
