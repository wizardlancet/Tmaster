"""UDS duplex codec (agent ↔ sidecar).

Framing rule from docs/protocol.md §6:

- Record boundary is a single marker byte:
  - `0x00` → a binary frame follows: `uint32 len` then `len` bytes (see frames.py).
  - anything else → the marker byte is the first byte of a UTF-8 JSON envelope
    that runs until the next `\\n`.

This keeps both planes on one socket and trivially interleavable.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncIterator
from typing import Union

from tmaster.common.envelope import Envelope
from tmaster.common.frames import BinaryFrame, decode_frame

MARKER_BINARY = 0x00

Record = Union[Envelope, BinaryFrame]


class UdsCodecError(Exception):
    pass


async def read_records(reader: asyncio.StreamReader) -> AsyncIterator[Record]:
    """Yield envelopes and binary frames from a UDS peer until EOF."""
    while True:
        first = await reader.read(1)
        if not first:
            return
        if first[0] == MARKER_BINARY:
            length_bytes = await reader.readexactly(4)
            (length,) = struct.unpack(">I", length_bytes)
            if length > 16 * 1024 * 1024:
                raise UdsCodecError(f"binary frame too large: {length}")
            body = await reader.readexactly(length)
            yield decode_frame(body)
        else:
            # re-inject the first byte then read until newline
            rest = await reader.readuntil(b"\n")
            line = first + rest
            try:
                obj = json.loads(line.decode("utf-8").rstrip("\n"))
            except json.JSONDecodeError as e:
                raise UdsCodecError(f"invalid json line: {e}") from e
            yield Envelope.model_validate(obj)


def encode_envelope(env: Envelope) -> bytes:
    data = env.model_dump_json(exclude_none=True).encode("utf-8")
    # The marker is implicitly "any non-zero byte"; JSON starts with '{' (0x7B).
    return data + b"\n"


def encode_binary(frame: BinaryFrame) -> bytes:
    body = frame.encode()
    return bytes([MARKER_BINARY]) + struct.pack(">I", len(body)) + body


async def write_record(writer: asyncio.StreamWriter, record: Record) -> None:
    if isinstance(record, Envelope):
        writer.write(encode_envelope(record))
    else:
        writer.write(encode_binary(record))
    await writer.drain()
