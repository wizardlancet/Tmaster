"""Envelope models for the TMaster control plane.

See docs/protocol.md §2 for the canonical specification. This module is the
normative Python binding.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Scope(str, Enum):
    AGENT = "agent"
    WORKSPACE = "workspace"
    SERVER = "server"


class MsgType(str, Enum):
    REQ = "req"
    RESP = "resp"
    EVENT = "event"


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None


def _new_id() -> str:
    # ULID-shaped ids would require an extra dep; uuid4 is sufficient for
    # uniqueness on a single connection and keeps the core dep-free.
    return uuid.uuid4().hex


def _now_ms() -> int:
    return int(time.time() * 1000)


class Envelope(BaseModel):
    """A single control-plane frame.

    Invariants enforced on parse:
    - `resp` messages must carry `in_reply_to` and `ok`.
    - `event` messages must not carry `in_reply_to`.
    - `scope == workspace` messages must have a non-null `target`.
    """

    id: str = Field(default_factory=_new_id)
    type: MsgType
    scope: Scope
    target: Optional[str] = None
    op: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: int = Field(default_factory=_now_ms)

    in_reply_to: Optional[str] = None
    ok: Optional[bool] = None
    error: Optional[ErrorDetail] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "Envelope":
        if self.type == MsgType.RESP:
            if self.in_reply_to is None:
                raise ValueError("resp envelope must set in_reply_to")
            if self.ok is None:
                raise ValueError("resp envelope must set ok")
            if self.ok is False and self.error is None:
                raise ValueError("resp envelope with ok=false must set error")
        else:
            if self.in_reply_to is not None:
                raise ValueError(f"{self.type.value} envelope must not set in_reply_to")
        if self.scope == Scope.WORKSPACE and not self.target:
            raise ValueError("workspace-scoped envelope requires target")
        return self

    # ---- convenience constructors --------------------------------------

    @classmethod
    def req(
        cls,
        *,
        scope: Scope,
        op: str,
        payload: Optional[dict[str, Any]] = None,
        target: Optional[str] = None,
    ) -> "Envelope":
        return cls(
            type=MsgType.REQ,
            scope=scope,
            op=op,
            target=target,
            payload=payload or {},
        )

    @classmethod
    def event(
        cls,
        *,
        scope: Scope,
        op: str,
        payload: Optional[dict[str, Any]] = None,
        target: Optional[str] = None,
    ) -> "Envelope":
        return cls(
            type=MsgType.EVENT,
            scope=scope,
            op=op,
            target=target,
            payload=payload or {},
        )

    def reply(
        self,
        *,
        ok: bool = True,
        payload: Optional[dict[str, Any]] = None,
        error: Optional[ErrorDetail] = None,
    ) -> "Envelope":
        return Envelope(
            type=MsgType.RESP,
            scope=self.scope,
            target=self.target,
            op=self.op,
            payload=payload or {},
            in_reply_to=self.id,
            ok=ok,
            error=error,
        )

    def reply_error(self, code: str, message: str, **details: Any) -> "Envelope":
        return self.reply(
            ok=False,
            error=ErrorDetail(code=code, message=message, details=details or None),
        )


# Some op names are used by more than one component, so we centralise the
# string constants here to catch typos at import time.
class Ops:
    # agent scope
    AGENT_PING = "agent.ping"
    AGENT_WS_LIST = "agent.workspace.list"
    AGENT_WS_CREATE = "agent.workspace.create"
    AGENT_WS_KILL = "agent.workspace.kill"
    AGENT_WS_UPDATE = "agent.workspace.update"  # event only

    # workspace scope — tmux
    TMUX_OPEN = "tmux.open"
    TMUX_CLOSE = "tmux.close"
    TMUX_RESIZE = "tmux.resize"
    TMUX_STATE = "tmux.state"  # event only

    # workspace scope — filesystem
    FS_LIST = "fs.list"
    FS_STAT = "fs.stat"
    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    FS_MKDIR = "fs.mkdir"
    FS_DELETE = "fs.delete"
    FS_RENAME = "fs.rename"
    FS_UPLOAD = "fs.upload"
    FS_DOWNLOAD = "fs.download"

    # workspace scope — status probes
    STATUS_GET = "status.get"
    STATUS_UPDATE = "status.update"  # event only

    # server scope
    AUTH_REFRESH = "auth.refresh"
    WORKSPACE_LIST = "workspace.list"
    AUDIT_QUERY = "audit.query"


# A typed alias mostly useful for IDE help.
Handshake = Literal["hello", "hello_ack"]
