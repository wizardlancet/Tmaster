"""TMaster sidecar entry point.

Invoked by the agent as:

    tmaster-sidecar \
        --workspace-id <id> \
        --socket <path> \
        --tmux-session <name> \
        --cwd <path>

The sidecar listens on the UDS socket for a single agent client, bridges
a ``tmux -CC`` stream for the PTY plane, and handles fs.* requests on the
control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path
from typing import Any, Optional

from tmaster.common import (
    Envelope,
    FrameTag,
    MsgType,
    Scope,
    configure_logging,
    get_logger,
)
from tmaster.common.envelope import Ops
from tmaster.common.frames import BinaryFrame
from tmaster.common.uds import read_records, write_record

from tmaster.sidecar.fs import FsSandbox, PathDenied
from tmaster.sidecar.tmux_bridge import TmuxControl

log = get_logger("sidecar")


class Sidecar:
    def __init__(
        self,
        *,
        workspace_id: str,
        socket: Path,
        tmux_session: str,
        cwd: Path,
        tmux_bin: str = "tmux",
    ) -> None:
        self.workspace_id = workspace_id
        self.socket = socket
        self.tmux_session = tmux_session
        self.cwd = cwd
        self.tmux_bin = tmux_bin
        self.fs = FsSandbox(root=cwd)

        self._server: Optional[asyncio.base_events.Server] = None
        self._peer_writer: Optional[asyncio.StreamWriter] = None
        self._send_lock = asyncio.Lock()
        self._tmux: Optional[TmuxControl] = None
        # Set of agent_stream_ids subscribed to the shared tmux stream.
        # Allows N dashboards to attach to the same tmux session simultaneously.
        self._tmux_streams: set[int] = set()

    async def run(self) -> None:
        self.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.socket.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle_client, str(self.socket))
        os.chmod(self.socket, 0o600)
        log.info("sidecar listening", socket=str(self.socket), workspace_id=self.workspace_id)
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Verify the peer is the same uid (we don't have auth beyond that).
        sock = writer.get_extra_info("socket")
        try:
            import struct
            SO_PEERCRED = 17  # Linux
            creds = sock.getsockopt(1, SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", creds)
            if uid != os.getuid():
                log.warning("rejecting peer with wrong uid", uid=uid, peer_pid=pid)
                writer.close()
                return
        except OSError:
            # Non-Linux: skip peer verification — the 0600 socket perms are
            # our primary defence.
            pass

        if self._peer_writer is not None:
            log.warning("second client connected; closing old peer")
            try:
                self._peer_writer.close()
            except Exception:
                pass
        self._peer_writer = writer

        try:
            async for record in read_records(reader):
                if isinstance(record, Envelope):
                    await self._handle_envelope(record)
                else:
                    await self._handle_frame(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sidecar client loop crashed")
        finally:
            if self._peer_writer is writer:
                self._peer_writer = None
            try:
                writer.close()
            except Exception:
                pass

    # ---- envelope dispatch ---------------------------------------------

    async def _handle_envelope(self, env: Envelope) -> None:
        if env.scope != Scope.WORKSPACE:
            if env.type == MsgType.REQ:
                await self._send(env.reply_error("bad_request", "workspace scope required"))
            return

        op = env.op
        try:
            if op == Ops.TMUX_OPEN:
                await self._op_tmux_open(env)
            elif op == Ops.TMUX_CLOSE:
                await self._op_tmux_close(env)
            elif op == Ops.TMUX_RESIZE:
                await self._op_tmux_resize(env)
            elif op == Ops.FS_LIST:
                res = self.fs.list_dir(env.payload["path"])
                await self._send(env.reply(payload={"entries": res}))
            elif op == Ops.FS_STAT:
                res = self.fs.stat(env.payload["path"])
                await self._send(env.reply(payload={"stat": res}))
            elif op == Ops.FS_READ:
                data = self.fs.read(
                    env.payload["path"], max_bytes=env.payload.get("max_bytes")
                )
                # Simple path: return data inline (base64) for small files;
                # the stream-based path is added in the upload-download todo.
                import base64
                await self._send(
                    env.reply(
                        payload={
                            "content_b64": base64.b64encode(data).decode("ascii"),
                            "size": len(data),
                        }
                    )
                )
            elif op == Ops.FS_WRITE:
                import base64
                raw = base64.b64decode(env.payload["content_b64"])
                mtime = self.fs.write(
                    env.payload["path"],
                    raw,
                    expected_mtime=env.payload.get("expected_mtime"),
                    mode=env.payload.get("mode"),
                )
                await self._send(env.reply(payload={"mtime": mtime}))
            elif op == Ops.FS_MKDIR:
                self.fs.mkdir(env.payload["path"], parents=env.payload.get("parents", True))
                await self._send(env.reply())
            elif op == Ops.FS_DELETE:
                self.fs.delete(
                    env.payload["path"], recursive=env.payload.get("recursive", False)
                )
                await self._send(env.reply())
            elif op == Ops.FS_RENAME:
                self.fs.rename(env.payload["from"], env.payload["to"])
                await self._send(env.reply())
            elif op == Ops.STATUS_GET:
                await self._send(env.reply(payload={"probes": {}}))  # TODO: probes
            else:
                if env.type == MsgType.REQ:
                    await self._send(env.reply_error("bad_request", f"unknown op {op}"))
        except PathDenied as e:
            await self._send(env.reply_error("path_denied", str(e)))
        except FileNotFoundError as e:
            await self._send(env.reply_error("not_found", str(e)))
        except FileExistsError as e:
            await self._send(env.reply_error("conflict", str(e)))
        except Exception as e:
            log.exception("op failed", op=op)
            await self._send(env.reply_error("internal", str(e)))

    async def _op_tmux_open(self, env: Envelope) -> None:
        sid = int(env.payload.get("stream_id") or 0)
        if not sid:
            await self._send(env.reply_error("bad_request", "stream_id required"))
            return
        cols = int(env.payload.get("cols") or 120)
        rows = int(env.payload.get("rows") or 40)

        # Start the shared tmux -CC on the first subscriber; subsequent opens
        # just register a new stream id.
        if self._tmux is None:
            async def _on_output(data: bytes) -> None:
                for s in list(self._tmux_streams):
                    await self._send_frame(
                        BinaryFrame(tag=FrameTag.PTY_OUT, stream_id=s, payload=data)
                    )

            async def _on_event(kind: str, args: list[str]) -> None:
                if kind == "exit":
                    for s in list(self._tmux_streams):
                        await self._send_frame(
                            BinaryFrame(
                                tag=FrameTag.STREAM_CLOSE, stream_id=s, payload=bytes([0])
                            )
                        )
                    self._tmux_streams.clear()
                    return
                log.debug("tmux event", kind=kind, args=args)

            self._tmux = TmuxControl(
                session=self.tmux_session,
                cols=cols,
                rows=rows,
                tmux_bin=self.tmux_bin,
                on_output=_on_output,
                on_event=_on_event,
            )
            await self._tmux.start()
        else:
            # Resize the existing tmux to the newcomer's viewport. The smallest
            # viewport among attached clients ends up winning in tmux, which is
            # what you want when multiple viewers share a session.
            try:
                await self._tmux.resize(cols, rows)
            except Exception:
                log.exception("resize on additional attach failed")

        self._tmux_streams.add(sid)
        await self._send(env.reply(payload={"stream_id": sid}))
        await self._send_frame(
            BinaryFrame(tag=FrameTag.STREAM_OPEN_ACK, stream_id=sid, payload=b"")
        )

        # Fast-attach: push an immediate capture-pane snapshot so the viewer
        # sees recent scrollback + current screen instantly, without waiting
        # for tmux -CC to replay via %output. The live stream will continue
        # to deliver deltas after this; any duplicated bytes are harmless
        # because xterm just re-renders the same content.
        try:
            snap = await self._capture_pane_snapshot()
            if snap:
                await self._send_frame(
                    BinaryFrame(tag=FrameTag.PTY_OUT, stream_id=sid, payload=snap)
                )
        except Exception:
            log.exception("capture-pane snapshot failed")

    async def _capture_pane_snapshot(self) -> bytes:
        """Return the current pane contents (scrollback + visible) as bytes
        with escape sequences preserved (``-e``)."""
        args = [
            self.tmux_bin,
            "capture-pane",
            "-p",
            "-e",
            "-J",
            "-S", "-200",
            "-t", self.tmux_session,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return b""
        # tmux prints with trailing newlines per row; normalise to CRLF so xterm
        # renders rows correctly (the live -CC stream already uses CRLF).
        return out.replace(b"\n", b"\r\n")

    async def _op_tmux_close(self, env: Envelope) -> None:
        sid = int(env.payload.get("stream_id") or 0)
        if sid:
            self._tmux_streams.discard(sid)
        else:
            # Legacy: close all
            self._tmux_streams.clear()
        if not self._tmux_streams and self._tmux is not None:
            await self._tmux.stop()
            self._tmux = None
        await self._send(env.reply())

    async def _op_tmux_resize(self, env: Envelope) -> None:
        if self._tmux is None:
            await self._send(env.reply_error("not_found", "no open tmux stream"))
            return
        await self._tmux.resize(int(env.payload["cols"]), int(env.payload["rows"]))
        await self._send(env.reply())

    # ---- frame dispatch -------------------------------------------------

    async def _handle_frame(self, frame: BinaryFrame) -> None:
        if frame.tag == FrameTag.PTY_IN and self._tmux is not None:
            # Any subscribed stream can send input into the shared tmux.
            if frame.stream_id in self._tmux_streams:
                await self._tmux.send_input(frame.payload)
        elif frame.tag == FrameTag.PTY_RESIZE and self._tmux is not None:
            from tmaster.common.frames import decode_resize
            cols, rows = decode_resize(frame)
            await self._tmux.resize(cols, rows)
        # File-chunk frames handled in a later todo.

    # ---- send helpers ---------------------------------------------------

    async def _send(self, env: Envelope) -> None:
        w = self._peer_writer
        if w is None:
            return
        async with self._send_lock:
            try:
                await write_record(w, env)
            except Exception:
                log.exception("sidecar send failed")

    async def _send_frame(self, frame: BinaryFrame) -> None:
        w = self._peer_writer
        if w is None:
            return
        async with self._send_lock:
            try:
                await write_record(w, frame)
            except Exception:
                log.exception("sidecar send_frame failed")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tmaster-sidecar")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--tmux-session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--tmux-bin", default="tmux")
    args = parser.parse_args()

    configure_logging("sidecar")
    sc = Sidecar(
        workspace_id=args.workspace_id,
        socket=Path(args.socket),
        tmux_session=args.tmux_session,
        cwd=Path(args.cwd),
        tmux_bin=args.tmux_bin,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_evt = asyncio.Event()

    def _sig(*_a):
        stop_evt.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _sig)

    async def _runner():
        task = asyncio.create_task(sc.run())
        await stop_evt.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        loop.run_until_complete(_runner())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
