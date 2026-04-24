"""Shared utilities for server, agent, and sidecar.

Everything here is transport-layer: envelope schema, binary framing, UDS codec,
config helpers, structured logging. No business logic.
"""

from tmaster.common.envelope import Envelope, ErrorDetail, MsgType, Scope
from tmaster.common.frames import FrameTag, decode_frame, encode_frame
from tmaster.common.logging import configure_logging, get_logger

__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "ErrorDetail",
    "MsgType",
    "Scope",
    "FrameTag",
    "encode_frame",
    "decode_frame",
    "configure_logging",
    "get_logger",
]

PROTOCOL_VERSION = 1
