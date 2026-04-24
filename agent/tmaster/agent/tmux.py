"""Tmux lifecycle helpers used by the agent.

We intentionally use subprocess instead of libtmux for the bits we care about
(create/kill + existence probe) so we have explicit control over exit codes
and don't pull libtmux into the hot path. libtmux is still a dependency for
future richer session introspection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional


class TmuxError(RuntimeError):
    pass


class Tmux:
    def __init__(self, binary: str = "tmux") -> None:
        self.binary = binary

    async def _run(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode or 0
        if check and rc != 0:
            raise TmuxError(f"tmux {' '.join(args)} failed (rc={rc}): {err.decode().strip()}")
        return rc, out.decode(), err.decode()

    async def has_session(self, name: str) -> bool:
        rc, _, _ = await self._run("has-session", "-t", name, check=False)
        return rc == 0

    async def new_session(
        self,
        name: str,
        *,
        cwd: Optional[str | Path] = None,
        command: Optional[str] = None,
        width: int = 120,
        height: int = 40,
    ) -> None:
        args = ["new-session", "-d", "-s", name, "-x", str(width), "-y", str(height)]
        if cwd:
            args += ["-c", str(cwd)]
        if command:
            args.append(command)
        await self._run(*args)

    async def kill_session(self, name: str) -> None:
        if await self.has_session(name):
            await self._run("kill-session", "-t", name, check=False)

    async def server_version(self) -> str:
        rc, out, _ = await self._run("-V", check=False)
        return out.strip() if rc == 0 else "unknown"

    async def probe_session(self, name: str) -> Optional[dict[str, str]]:
        """Return runtime info for a session, or None if the session is gone.

        Uses ``display-message -p`` against the first pane of the active window
        to capture the foreground command + pid. ``activity`` is derived from
        whether the command looks like an interactive shell.
        """
        rc, out, _ = await self._run(
            "display-message",
            "-p",
            "-t",
            name,
            "-F",
            "#{pane_current_command}\t#{pane_pid}\t#{session_activity}",
            check=False,
        )
        if rc != 0:
            return None
        line = out.strip().splitlines()[0] if out.strip() else ""
        parts = line.split("\t")
        cmd = parts[0] if len(parts) > 0 else ""
        pid = parts[1] if len(parts) > 1 else ""
        activity_ms = parts[2] if len(parts) > 2 else ""
        shells = {"bash", "zsh", "sh", "fish", "ash", "dash", "ksh", "tcsh", "csh"}
        activity = "idle" if cmd in shells else "busy"
        info: dict[str, str] = {
            "current_command": cmd,
            "activity": activity,
        }
        if pid.isdigit():
            info["current_pid"] = pid
        if activity_ms.isdigit():
            # tmux reports ms; we emit seconds for symmetry with other ts
            info["last_activity_at"] = str(int(int(activity_ms) / 1000))
        return info
