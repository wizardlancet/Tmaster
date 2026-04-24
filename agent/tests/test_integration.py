"""End-to-end integration tests that exercise real tmux.

Skipped automatically if tmux isn't installed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tmaster.common import Envelope, FrameTag, Scope
from tmaster.common.envelope import Ops
from tmaster.common.frames import BinaryFrame
from tmaster.common.uds import read_records, write_record
from tmaster.agent.tmux import Tmux

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.mark.asyncio
async def test_tmux_helper_create_and_kill(tmp_path: Path) -> None:
    t = Tmux("tmux")
    name = f"tm_test_{uuid.uuid4().hex[:6]}"
    # Isolate the tmux server by using a per-test socket to avoid interfering
    # with a user's real tmux.
    # (libtmux/Tmux here doesn't support that directly; we use TMUX_TMPDIR env var
    # through a monkeypatch approach.)
    os.environ["TMUX_TMPDIR"] = str(tmp_path)
    try:
        assert not await t.has_session(name)
        await t.new_session(name, cwd=str(tmp_path))
        assert await t.has_session(name)
        await t.kill_session(name)
        assert not await t.has_session(name)
    finally:
        os.environ.pop("TMUX_TMPDIR", None)


@pytest.mark.asyncio
async def test_sidecar_full_flow(tmp_path: Path) -> None:
    """Spawn a real sidecar that talks to a real tmux, exchange a PTY stream."""

    # Create a scratch tmux session on an isolated server socket.
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    env = os.environ.copy()
    env["TMUX_TMPDIR"] = str(tmux_dir)

    session = f"tm_it_{uuid.uuid4().hex[:8]}"
    cwd = tmp_path / "ws"
    cwd.mkdir()

    # Start tmux session (bash with a simple PS1 so output is deterministic).
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "120", "-y", "40", "-c", str(cwd),
         "bash", "--noprofile", "--norc"],
        check=True, env=env,
    )
    try:
        # Spawn the sidecar binary. Unix socket paths are limited to ~108
        # chars so pytest's tmp_path can be too long; use /tmp directly.
        sock = Path(f"/tmp/tm-it-{uuid.uuid4().hex[:8]}.sock")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tmaster.sidecar",
            "--workspace-id", "w1",
            "--socket", str(sock),
            "--tmux-session", session,
            "--cwd", str(cwd),
            "--tmux-bin", "tmux",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait for the socket to appear.
        for _ in range(100):
            if sock.exists():
                break
            if proc.returncode is not None:
                err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                pytest.fail(f"sidecar exited early: {err}")
            await asyncio.sleep(0.05)
        else:
            pytest.fail("sidecar never created the socket")

        reader, writer = await asyncio.open_unix_connection(str(sock))
        try:
            # FS ops
            req = Envelope.req(
                scope=Scope.WORKSPACE, target="w1", op=Ops.FS_LIST,
                payload={"path": "."},
            )
            await write_record(writer, req)

            # Open tmux stream
            open_req = Envelope.req(
                scope=Scope.WORKSPACE, target="w1", op=Ops.TMUX_OPEN,
                payload={"stream_id": 7, "cols": 80, "rows": 24},
            )
            await write_record(writer, open_req)

            # Type "echo hi\n" as PTY input
            async def _send_keys(ks: bytes) -> None:
                await write_record(
                    writer,
                    BinaryFrame(tag=FrameTag.PTY_IN, stream_id=7, payload=ks),
                )

            # Collect records until we see "hi" in PTY output or timeout.
            saw_fs_resp = False
            saw_tmux_open_resp = False
            saw_open_ack = False
            collected_output = bytearray()

            async def _drain_with_timeout():
                nonlocal saw_fs_resp, saw_tmux_open_resp, saw_open_ack
                async def _read():
                    nonlocal saw_fs_resp, saw_tmux_open_resp, saw_open_ack
                    async for rec in read_records(reader):
                        if isinstance(rec, Envelope):
                            if rec.op == Ops.FS_LIST and rec.ok:
                                saw_fs_resp = True
                            if rec.op == Ops.TMUX_OPEN and rec.ok:
                                saw_tmux_open_resp = True
                                await _send_keys(b"echo hi-tmaster\n")
                        else:
                            if rec.tag == FrameTag.STREAM_OPEN_ACK:
                                saw_open_ack = True
                            elif rec.tag == FrameTag.PTY_OUT:
                                collected_output.extend(rec.payload)
                                if b"hi-tmaster" in collected_output:
                                    return
                await asyncio.wait_for(_read(), timeout=10.0)

            await _drain_with_timeout()

            assert saw_fs_resp
            assert saw_tmux_open_resp
            assert saw_open_ack
            assert b"hi-tmaster" in collected_output
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            env=env, check=False,
        )
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
