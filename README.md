# TMaster

Manage multiple remote workspaces (tmux sessions) through a lightweight web dashboard
while retaining native `tmux` access on every machine.

## Architecture

Three components:

- **Server Core** — Python/FastAPI process reachable by dashboard users and by agents.
  Performs auth, message routing, and hosts the dashboard static bundle.
- **Agent** — One per managed machine. Opens an outbound WSS connection to the server,
  owns the lifecycle of TMaster workspaces on that host, and supervises sidecars.
- **Sidecar** — One per workspace, spawned by the agent together with the tmux session.
  Bridges the tmux control-mode stream, serves the workspace file system, and runs
  status probes (e.g. "is my coding agent idle?").

The local user stays on tmux: `tmux attach -t <label>` (or `tmaster workspace open <label>`)
joins exactly the same session that the dashboard sees.

See [`docs/protocol.md`](docs/protocol.md) for the wire protocol and
[`docs/deployment.md`](docs/deployment.md) for deployment topology.

## Repository layout

```
server/      FastAPI server + dashboard static hosting
agent/       Python package with two entry points: tmaster-agent, tmaster-sidecar
dashboard/   React + TypeScript + Vite single-page app
docs/        Protocol & deployment documentation
```

## Development status

Early scaffolding. See `plan.md` in the session state for the phased roadmap.
