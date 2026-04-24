"""`tmaster` local CLI.

Minimal first version: talks directly to tmux because the primary use case is
``tmaster workspace open <label>`` which is just a wrapper around
``tmux attach -t <real-session-name>``. Listing uses the agent's registry.

A future version will optionally connect to the agent over UDS to do
create/kill round-trips (right now those go through the server so we depend on
it being reachable).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from tmaster.agent.config import load as load_settings


@click.group()
def main() -> None:
    """TMaster local CLI."""


@main.group()
def workspace() -> None:
    """Workspace operations."""


@workspace.command("ls")
def workspace_ls() -> None:
    """List workspaces registered on this machine."""
    import asyncio
    from tmaster.agent.registry import Registry

    async def _go():
        s = load_settings()
        r = Registry(s.state_dir / "agent.db")
        await r.connect()
        try:
            recs = await r.list_all()
        finally:
            await r.close()
        for w in recs:
            click.echo(
                f"{w.id[:8]}\t{w.status:10s}\t{w.label:20s}\t{w.tmux_session_name}\t{w.cwd or ''}"
            )

    asyncio.run(_go())


@workspace.command("open")
@click.argument("label_or_id")
def workspace_open(label_or_id: str) -> None:
    """Attach to a workspace locally via tmux."""
    import asyncio
    from tmaster.agent.registry import Registry

    async def _resolve() -> str | None:
        s = load_settings()
        r = Registry(s.state_dir / "agent.db")
        await r.connect()
        try:
            recs = await r.list_all()
        finally:
            await r.close()
        # Exact id, then id prefix, then label
        for w in recs:
            if w.id == label_or_id or w.tmux_session_name == label_or_id or w.label == label_or_id:
                return w.tmux_session_name
        for w in recs:
            if w.id.startswith(label_or_id):
                return w.tmux_session_name
        return None

    target = asyncio.run(_resolve())
    if target is None:
        click.echo(f"no workspace matching {label_or_id!r}", err=True)
        sys.exit(1)
    s = load_settings()
    os.execvp(s.tmux_bin, [s.tmux_bin, "attach", "-t", target])


@main.command("enroll")
@click.option("--server", required=True, help="Base HTTP URL of the server, e.g. https://tm.example.com")
@click.option("--enrollment-token", required=True)
@click.option("--name", required=True, help="Machine name to register as")
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path.home() / ".config" / "tmaster" / "agent.env",
    show_default=True,
)
def enroll(server: str, enrollment_token: str, name: str, env_file: Path) -> None:
    """Exchange a one-time enrollment token for a long-lived agent token."""
    import urllib.error
    import urllib.request
    import json

    body = json.dumps({"enrollment_token": enrollment_token, "agent_name": name}).encode()
    req = urllib.request.Request(
        server.rstrip("/") + "/api/agents/enroll",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        click.echo(f"enrollment failed: {e.code} {e.reason}", err=True)
        sys.exit(1)

    env_file.parent.mkdir(parents=True, exist_ok=True)
    ws_url = server.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    env_file.write_text(
        "\n".join([
            f"TMASTER_AGENT_SERVER_URL={ws_url}",
            f"TMASTER_AGENT_AGENT_ID={data['agent_id']}",
            f"TMASTER_AGENT_AGENT_TOKEN={data['agent_token']}",
            "",
        ])
    )
    env_file.chmod(0o600)
    click.echo(f"wrote {env_file}")
    click.echo(f"agent_id={data['agent_id']}")


if __name__ == "__main__":
    main()
