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
