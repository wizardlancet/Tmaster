# TMaster — Architecture & Implementation

This document describes the concrete architecture and implementation of TMaster.
For the normative wire format see [`docs/protocol.md`](docs/protocol.md); for
install/run steps see [`get_start.md`](get_start.md).

## 1. Goals & high-level shape

TMaster lets a single operator manage multiple **workspaces** spread across
several remote machines. A *workspace* is a real `tmux` session created and
tracked by TMaster. Because it is a real tmux session, any user on that host
can still `tmux attach -t <name>` natively. Remote users reach the same session
through a web dashboard.

```
┌──────────────┐  WSS   ┌────────────────┐  WSS   ┌─────────────┐
│ Dashboard    │◀──────▶│ Server Core    │◀──────▶│ Agent       │
│ (React SPA)  │        │ (FastAPI)      │        │ (one/host)  │
└──────────────┘        └────────────────┘        └────┬────────┘
                                                       │ UDS (Unix socket)
                                                       ▼
                                              ┌─────────────────┐
                                              │ Sidecar         │
                                              │ (one/workspace) │
                                              └────┬────────────┘
                                                   │ tmux -CC
                                                   ▼
                                              ┌─────────────────┐
                                              │ tmux session    │ ← user may attach
                                              └─────────────────┘
```

Four processes, three transports. Everything above the UDS hop speaks the
same **envelope + binary-frame** protocol documented in `docs/protocol.md`.

## 2. Components

### 2.1 Server Core — `server/`
- FastAPI + uvicorn, single Python process.
- SQLite (`aiosqlite` + argon2) for users, agents, enrollment tokens,
  workspace snapshots, audit events.
- JWT-based dashboard auth, long-lived agent tokens issued via **one-time
  enrollment tokens**.
- Serves the compiled dashboard SPA from `server/app/static/` at `/`.
- Exposes:
  - `GET  /healthz`
  - `POST /api/auth/login`, `POST /api/auth/refresh`
  - `POST /api/agents/enrollment-token`, `POST /api/agents/enroll`
  - `GET  /api/workspaces`, `GET /api/agents`
  - `WS   /ws/agent` — outbound agent connections
  - `WS   /ws/dashboard?token=<JWT>` — dashboard multiplex
- Central routing lives in `app/core/hub.py` (`Hub`), which:
  - Keeps an in-memory registry of `AgentConn` and `DashboardConn`.
  - Allocates a per-agent `stream_id` namespace.
  - Maintains `StreamBridge` records so a single sidecar stream can fan out
    to N dashboards, each with its own `stream_id`.
  - Remaps `stream_id` on `tmux.open` req/resp and on every binary frame
    (`PTY_OUT`, `PTY_IN`, …).

### 2.2 Agent — `agent/tmaster/agent/`
- Entry point: `tmaster-agent` (or `python -m tmaster.agent`).
- Outbound WebSocket client with exponential reconnect and JSON heartbeats.
- Handshake: `{"type":"hello","proto":1,"agent_id":…,"agent_token":…}`.
- Local SQLite **registry** at `$XDG_STATE_HOME/tmaster/registry.sqlite`
  stores every workspace it has ever created (tmux name, cwd, config,
  sidecar socket path, status).
- On start: reconciles registry against live `tmux` — prunes sessions the
  user already killed, but does **not** auto-respawn sidecars.
- Handles server requests:
  - `agent.ping`, `agent.workspace.list`
  - `agent.workspace.create` → `tmux new-session -d … -s tm_<uuid8>`
  - `agent.workspace.kill` → stop sidecar + `tmux kill-session`
- Sidecar supervisor (`supervisor.py`):
  - Spawns `tmaster-sidecar` on demand when the server routes a
    workspace-scoped request for that id.
  - Waits up to 10 s for the UDS to appear, then connects and starts a
    read loop.
  - On stop: closes the UDS, SIGTERM → SIGKILL the child, unlinks socket.
- Scope routing inside the agent:
  - `scope=agent` → handled locally.
  - `scope=workspace` → spawn sidecar if needed, forward the envelope
    over UDS. `stream_id` → workspace id map maintained for later binary
    traffic.

### 2.3 Sidecar — `agent/tmaster/sidecar/`
- Entry point: `tmaster-sidecar`.
- One process per workspace; lifetime bounded by that workspace.
- UDS server at `$XDG_RUNTIME_DIR/tmaster/<ws>.sock`, mode 0600, parent dir
  0700, plus **SO_PEERCRED uid check** (Linux) so only the agent user can
  talk to it.
- Operations:
  - `fs.list|stat|read|write|mkdir|delete|rename` — all go through
    `FsSandbox`, which resolves paths and enforces they stay under the
    workspace cwd.
  - `tmux.open|close|resize` — open a `tmux -C attach-session -t <name>`
    subprocess and pipe its stdout through the control-mode parser.
  - Reads stream PTY bytes out as binary frames tagged `PTY_OUT`.
  - Accepts `PTY_IN` frames and injects them with `send-keys -t <s> -H <hex>`
    (so every byte is faithfully preserved).
  - `refresh-client -C <W>x<H>` for resize.

### 2.4 Dashboard — `dashboard/`
- React 18 + Vite + Tailwind + React Router + TanStack Query + Zustand
  (persist) + xterm.js + Monaco.
- `lib/ws.ts` mirrors the Python protocol exactly: JSON envelopes, binary
  frames `[1B tag][4B stream_id BE][payload]`, request/response correlation
  by `in_reply_to`.
- `lib/auth.ts` persists `{accessToken, refreshToken, userId}`.
- Pages:
  - **Login** (`/login`) — username + password → `/api/auth/login`.
  - **Workspaces** (`/`) — lists agents & workspaces (auto-refresh), mints
    enrollment tokens, creates/kills workspaces over the dashboard WS.
  - **Terminal** (`/terminal/:id`) — xterm.js bound to a dashboard-allocated
    `stream_id`; uses `TMUX_OPEN`, relays `PTY_IN`/`PTY_OUT`, `TMUX_RESIZE`
    on `ResizeObserver`.
  - **Files** (`/files/:id`) — directory tree + Monaco editor; `FS_LIST` /
    `FS_READ` / `FS_WRITE` over base64 JSON payloads.
- Built into `server/app/static/` so the server serves the SPA at `/`.

### 2.5 Local CLI — `tmaster` (shipped with the agent package)
- `tmaster enroll` — interactive: given server URL + enrollment token,
  calls `/api/agents/enroll` and writes `agent_id` + `agent_token` into the
  agent config file.
- `tmaster workspace ls|open` — list workspaces from the local registry;
  `open` runs `tmux attach -t <tmux_session_name>` on the real tmux session.

## 3. Protocol layers at a glance

| Layer                    | Transport           | Payload                                    |
|--------------------------|---------------------|--------------------------------------------|
| Dashboard ↔ Server       | WebSocket over TLS  | JSON envelopes **and** binary frames       |
| Agent ↔ Server           | WebSocket over TLS  | same                                       |
| Agent ↔ Sidecar          | Unix domain socket  | length-prefixed JSON lines + binary frames |
| Sidecar ↔ tmux           | `tmux -CC` pipes    | control-mode text protocol                 |

### Envelope (see `docs/protocol.md §2` + `tmaster.common.envelope`)
```json
{
  "id": "uuid4hex",
  "type": "req" | "resp" | "event",
  "scope": "agent" | "workspace" | "server",
  "target": "<agent_id or workspace_id>",
  "op": "tmux.open",
  "payload": {...},
  "ts": 1712345678901,
  "in_reply_to": "…",   // resp only
  "ok": true,           // resp only
  "error": {"code":"...","message":"..."}
}
```

### Binary frame (all hops, including the browser)
```
┌──────┬───────────────────┬───────────────────────┐
│ tag  │ stream_id (u32 BE)│ payload               │
│ 1 B  │ 4 B               │ variable              │
└──────┴───────────────────┴───────────────────────┘
```
Tags: `PTY_OUT=0x01`, `PTY_IN=0x02`, `PTY_RESIZE=0x03`, `FILE_CHUNK=0x10`,
`FILE_EOF=0x11`, `FILE_ABORT=0x12`, `STREAM_OPEN_ACK=0xFE`,
`STREAM_CLOSE=0xFF`.

### UDS framing between agent and sidecar
First byte disambiguates:
- `0x00` → followed by `u32` length and a binary frame.
- anything else → start of a JSON envelope line terminated by `\n`
  (JSON always starts with `{`, so there is no ambiguity).

## 4. Data model

### Server SQLite (`<data_dir>/tmaster.sqlite`)
- `users(id, username, password_hash_argon2, created_at)`
- `agents(id, name, token_sha256, created_at, last_seen_at)`
- `enrollment_tokens(token_sha256, created_at, expires_at, consumed_at)`
- `workspaces(id, agent_id, label, tmux_session_name, cwd, status, created_at, last_seen_at)`
- `events(id, ts, kind, subject, detail_json)`

### Agent SQLite (`~/.local/state/tmaster/registry.sqlite`)
- `workspaces(id, tmux_session_name, label, cwd, config_json, sidecar_pid,
  sidecar_sock, status, created_at, updated_at)`

## 5. Stream ID namespaces

Each hop has its own `stream_id` space:
- Dashboard picks any positive u32 (e.g. `crypto.getRandomValues`-based).
- Hub allocates a hop-unique `agent_stream_id` per `(agent_id)` on
  `tmux.open` and records a `StreamBridge(dash_sid ↔ agent_sid)`.
- Hub rewrites `stream_id` both in the `tmux.open` response and in every
  subsequent binary frame — in both directions.
- Result: multiple dashboards can open the same tmux session concurrently
  and receive isolated streams. This is covered by
  `server/tests/test_hub.py::test_multi_viewer_stream_remap`.

## 6. Security model

- Dashboard: argon2 password → JWT access (2 h) + refresh (14 d).
- Agent enrollment: operator is logged into the dashboard, mints a
  one-time **enrollment token** (1 h TTL), runs `tmaster enroll` on the
  target machine which redeems it for a long-lived `(agent_id, agent_token)`.
- Agent ↔ server always uses WSS in production (plaintext `ws://` only
  for localhost dev).
- Sidecar UDS: `chmod 0600`, parent dir `0700`, and a Linux `SO_PEERCRED`
  check that the peer uid matches the agent uid.
- `FsSandbox` resolves every path and enforces `path.resolve().is_relative_to(root)`
  before any filesystem op — prevents `..` escapes and symlink traversal
  outside the workspace root.
- `tmux` session naming uses a `tm_` prefix on a UUID-derived 8-hex
  suffix so TMaster never touches sessions the user created themselves.

## 7. Observability

- Structured logs via `structlog` on all Python components.
- `/healthz` on the server.
- Agent emits `agent.workspace.update` events on lifecycle transitions
  which the hub fans out to all subscribed dashboards.

## 8. Testing

| Suite                               | What it covers                                  |
|-------------------------------------|-------------------------------------------------|
| `agent/tests/test_common.py`        | envelope model, frame codec, config loader      |
| `agent/tests/test_uds.py`           | UDS line + binary-frame duplex codec            |
| `agent/tests/test_integration.py`   | **real tmux** + **real sidecar**, full PTY RT   |
| `server/tests/test_server.py`       | healthz, login, enrollment flow                 |
| `server/tests/test_hub.py`          | multi-viewer stream_id remap in both directions |

Run everything with:
```bash
(cd agent  && . .venv/bin/activate && python -m pytest -q)
(cd server && . .venv/bin/activate && python -m pytest -q)
```

## 9. Repository layout

```
TMaster/
├─ server/              FastAPI app (Python)
│  ├─ app/
│  │  ├─ core/          config, store, auth, hub
│  │  ├─ api/           rest, agent_ws, dashboard_ws
│  │  └─ main.py
│  └─ tests/
├─ agent/               Python package (agent + sidecar + CLI)
│  └─ tmaster/
│     ├─ common/        envelope, frames, uds, config, logging
│     ├─ agent/         daemon, registry, supervisor, tmux helpers, CLI
│     ├─ sidecar/       UDS server, tmux_bridge, fs sandbox
│     └─ tests/
├─ dashboard/           React SPA (built into server/app/static/)
│  └─ src/{lib,pages,components}
├─ docs/                protocol.md (+ future deployment.md)
├─ wiki.md              this file
├─ get_start.md         install & run guide
├─ design.md            original design brief
└─ docker-compose.yml   reference deployment (server + Caddy for TLS)
```

## 10. Known gaps / roadmap

Still pending (see session plan for full list):

- `upload-download` — chunked binary file transfer (current `fs.read/write`
  go through base64 JSON; not suitable for big files).
- `status-probes` — framework for workspace status badges
  (e.g. "coding agent idle").
- `sidecar-supervision` — crash/backoff + unhealthy reporting.
- `auth-hardening` — login rate-limit, JWT rotation hardening.
- `tls-deploy` — Caddy reverse proxy reference setup, systemd units.
- `audit-events` — surfaced in dashboard.
- `observability` — optional Prometheus metrics.
- `recording` — optional per-workspace PTY ring buffer.
- `e2e-tests` — Playwright covering login → create → attach → edit.
- `docs` — deployment and probe authoring guide.
