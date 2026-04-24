"""Tmux control mode (``tmux -CC``) bridge.

This is the engine behind TMUX_OUT / TMUX_IN binary frames. We spawn
``tmux -C attach-session -t <name>`` as a child process and parse the
event stream it emits on stdout.

Control-mode syntax (minimal summary; see tmux(1) §CONTROL MODE):

* Every line starts with ``%`` and a notification keyword.
* ``%output %PANE_ID DATA`` — pane output, where DATA is octal-escaped.
* ``%window-add``, ``%window-close``, ``%window-renamed``, ``%session-changed``,
  ``%layout-change`` — metadata events.
* ``%begin`` ... ``%end``/``%error`` — reply blocks to commands we issue.
* ``%exit`` — tmux is about to exit.

We write tmux commands to stdin (one per line); tmux echoes them inside a
begin/end block.

For the MVP we only need: spawn, stream output, accept input, resize.
Richer state (window list, layout) is added in later phases and plumbed
through ``tmux.state`` events.
"""

from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, Optional

from tmaster.common import get_logger

log = get_logger("tmux_bridge")

_OCTAL_ESCAPE = re.compile(rb"\\([0-7]{3})")


def _unescape_output(data: bytes) -> bytes:
    """Convert tmux control-mode octal escapes back to raw bytes."""
    return _OCTAL_ESCAPE.sub(lambda m: bytes([int(m.group(1), 8)]), data)


OnOutput = Callable[[bytes], Awaitable[None]]
OnEvent = Callable[[str, list[str]], Awaitable[None]]


class TmuxControl:
    def __init__(
        self,
        *,
        session: str,
        cols: int = 120,
        rows: int = 40,
        tmux_bin: str = "tmux",
        on_output: Optional[OnOutput] = None,
        on_event: Optional[OnEvent] = None,
    ) -> None:
        self.session = session
        self.cols = cols
        self.rows = rows
        self.tmux_bin = tmux_bin
        self._on_output = on_output or (lambda _b: _noop())
        self._on_event = on_event or (lambda _k, _a: _noop())
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._read_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._in_reply_block = False
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        # -C = control mode. attach-session doesn't take -x/-y (those belong to
        # new-session); size is inherited from the (pseudo)terminal or set via
        # refresh-client -C afterwards.
        args = [
            self.tmux_bin,
            "-C",
            "attach-session",
            "-t",
            self.session,
        ]
        log.info("starting tmux control client", session=self.session, argv=args)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(self._read_loop(), name="tmux-ctl-read")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="tmux-ctl-err")
        # Apply initial size.
        try:
            await self.resize(self.cols, self.rows)
        except Exception:
            log.exception("initial resize failed")

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                await self._handle_line(line.rstrip(b"\r\n"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tmux control read loop crashed")
        finally:
            self._stopped.set()

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                log.warning("tmux stderr", line=line.decode(errors="replace").rstrip())
        except Exception:
            pass

    async def _handle_line(self, line: bytes) -> None:
        if not line.startswith(b"%"):
            # Inside a %begin/%end reply block we can receive any content;
            # for now we just drop it — we never query state that needs
            # parsing reply bodies.
            return

        # %output %PANE DATA
        if line.startswith(b"%output "):
            rest = line[len(b"%output ") :]
            # rest = "%<pane-id> <octal-escaped-data>"
            sp = rest.find(b" ")
            if sp < 0:
                return
            data = rest[sp + 1 :]
            raw = _unescape_output(data)
            await self._on_output(raw)
            return

        # %begin / %end / %error reply bracketing — we ignore bodies.
        if line.startswith(b"%begin"):
            self._in_reply_block = True
            return
        if line.startswith(b"%end") or line.startswith(b"%error"):
            self._in_reply_block = False
            return

        if line.startswith(b"%exit"):
            await self._on_event("exit", [])
            return

        # Other notifications: %window-add, %window-close, %window-renamed,
        # %session-changed, %layout-change, %unlinked-window-add, ...
        parts = line.split(b" ")
        kind = parts[0][1:].decode("ascii", errors="replace")
        args = [p.decode(errors="replace") for p in parts[1:]]
        await self._on_event(kind, args)

    # ---- sending --------------------------------------------------------

    async def send_input(self, data: bytes) -> None:
        """Forward keystrokes to tmux via send-keys -l."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        # send-keys -l sends a literal string; we base64 to avoid quoting
        # issues. tmux also supports hex via `send-keys -H`.
        hex_str = data.hex()
        if not hex_str:
            return
        # send-keys -H expects pairs of hex digits separated by spaces.
        pairs = " ".join(hex_str[i : i + 2] for i in range(0, len(hex_str), 2))
        cmd = f"send-keys -t {self.session} -H {pairs}\n"
        try:
            self._proc.stdin.write(cmd.encode("ascii"))
            await self._proc.stdin.drain()
        except Exception:
            log.exception("failed to send input")

    async def resize(self, cols: int, rows: int) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        self.cols, self.rows = cols, rows
        cmd = f"refresh-client -C {cols}x{rows}\n"
        try:
            self._proc.stdin.write(cmd.encode("ascii"))
            await self._proc.stdin.drain()
        except Exception:
            log.exception("failed to resize")

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._proc.kill()
                except Exception:
                    pass
                await self._proc.wait()
        if self._read_task:
            self._read_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()


async def _noop() -> None:
    return None
