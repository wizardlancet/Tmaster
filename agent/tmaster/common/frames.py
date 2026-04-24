"""Binary frame codec for PTY and file streams.

See docs/protocol.md §3. Layout:

    +--------+--------+--------+--------+--------+----------------+
    |  tag   |          stream_id (uint32, BE)    |   payload ... |
    +--------+--------+--------+--------+--------+----------------+
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

_HDR = struct.Struct(">BI")
HEADER_SIZE = _HDR.size  # 5


class FrameTag(IntEnum):
    PTY_OUT = 0x01
    PTY_IN = 0x02
    PTY_RESIZE = 0x03
    FILE_CHUNK = 0x10
    FILE_EOF = 0x11
    FILE_ABORT = 0x12
    STREAM_OPEN_ACK = 0xFE
    STREAM_CLOSE = 0xFF


@dataclass(slots=True)
class BinaryFrame:
    tag: FrameTag
    stream_id: int
    payload: bytes

    def encode(self) -> bytes:
        return encode_frame(self.tag, self.stream_id, self.payload)


class FrameError(ValueError):
    pass


def encode_frame(tag: FrameTag | int, stream_id: int, payload: bytes = b"") -> bytes:
    if stream_id < 0 or stream_id > 0xFFFFFFFF:
        raise FrameError(f"stream_id out of range: {stream_id}")
    return _HDR.pack(int(tag), stream_id) + payload


def decode_frame(buf: bytes) -> BinaryFrame:
    if len(buf) < HEADER_SIZE:
        raise FrameError(f"frame too short: {len(buf)} bytes")
    tag_i, stream_id = _HDR.unpack_from(buf, 0)
    try:
        tag = FrameTag(tag_i)
    except ValueError as e:
        raise FrameError(f"unknown frame tag 0x{tag_i:02x}") from e
    return BinaryFrame(tag=tag, stream_id=stream_id, payload=bytes(buf[HEADER_SIZE:]))


# ---- helpers for specific tags -----------------------------------------


def encode_resize(stream_id: int, cols: int, rows: int) -> bytes:
    return encode_frame(FrameTag.PTY_RESIZE, stream_id, struct.pack(">HH", cols, rows))


def decode_resize(frame: BinaryFrame) -> tuple[int, int]:
    if frame.tag != FrameTag.PTY_RESIZE:
        raise FrameError(f"expected PTY_RESIZE, got {frame.tag!r}")
    if len(frame.payload) != 4:
        raise FrameError("PTY_RESIZE payload must be 4 bytes")
    cols, rows = struct.unpack(">HH", frame.payload)
    return cols, rows


def encode_file_chunk(stream_id: int, offset: int, data: bytes) -> bytes:
    return encode_frame(FrameTag.FILE_CHUNK, stream_id, struct.pack(">Q", offset) + data)


def decode_file_chunk(frame: BinaryFrame) -> tuple[int, bytes]:
    if frame.tag != FrameTag.FILE_CHUNK:
        raise FrameError(f"expected FILE_CHUNK, got {frame.tag!r}")
    if len(frame.payload) < 8:
        raise FrameError("FILE_CHUNK payload must include uint64 offset")
    (offset,) = struct.unpack_from(">Q", frame.payload, 0)
    return offset, bytes(frame.payload[8:])
