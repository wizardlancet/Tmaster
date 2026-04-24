# Getting Started

This guide walks through installing and running TMaster end-to-end. There are
two sides:

1. **Server side** (one host, reachable by dashboard users and by agents).
2. **Client side** = one **Agent** per machine whose workspaces you want to
   manage.

The browser/web dashboard is served by the server — nothing to install for
end users beyond a modern browser.

> Prerequisites on every host: **Python ≥ 3.10** and **tmux ≥ 3.0**. The
> server and agent use their own virtualenvs. The dashboard requires
> **Node.js ≥ 18** only if you want to rebuild it yourself.

---

## 1. Server side

### 1.1 Clone & install

```bash
git clone <repo-url> TMaster
cd TMaster

# Server venv
python3 -m venv server/.venv
source server/.venv/bin/activate
pip install -e ./agent            # shared 'tmaster.common' lives here
pip install -e './server[dev]'    # the FastAPI app
deactivate
```

### 1.2 Build the dashboard SPA (one-time)

The server ships the compiled dashboard from `server/app/static/`. The repo
may already contain a build; to regenerate:

```bash
cd dashboard
npm install
npm run build      # output → ../server/app/static/
cd ..
```

### 1.3 Configure

The server reads environment variables (prefix `TMASTER_`). Minimum for a
local run:

| Variable                     | Example                              | Purpose                                |
|------------------------------|--------------------------------------|----------------------------------------|
| `TMASTER_DATA_DIR`           | `/var/lib/tmaster`                   | SQLite DB + derived state              |
| `TMASTER_LISTEN_HOST`        | `0.0.0.0` (`127.0.0.1` default)      | bind address                           |
| `TMASTER_LISTEN_PORT`        | `8000`                               | bind port                              |
| `TMASTER_JWT_SECRET`         | 32+ random bytes (hex)               | signing secret                         |
| `TMASTER_JWT_SECRET_FILE`    | path to a file holding the secret    | alternative to inline secret           |
| `TMASTER_BOOTSTRAP_USER`     | `admin`                              | initial user (default `admin`)         |
| `TMASTER_BOOTSTRAP_PASSWORD` | a strong password                    | used on first boot only                |

Generate a secret once:

```bash
mkdir -p /var/lib/tmaster
python -c "import secrets; print(secrets.token_hex(32))" > /var/lib/tmaster/jwt.secret
chmod 600 /var/lib/tmaster/jwt.secret
```

### 1.4 First boot — create the admin user

On the very first run, if no users exist, TMaster creates one from
`TMASTER_BOOTSTRAP_USER` / `TMASTER_BOOTSTRAP_PASSWORD`:

```bash
source server/.venv/bin/activate
export TMASTER_DATA_DIR=/var/lib/tmaster
export TMASTER_JWT_SECRET_FILE=/var/lib/tmaster/jwt.secret
export TMASTER_BOOTSTRAP_USER=admin
export TMASTER_BOOTSTRAP_PASSWORD='<strong password>'
export TMASTER_LISTEN_HOST=0.0.0.0
export TMASTER_LISTEN_PORT=8000
python -m app.main
```

You should see `uvicorn running on http://0.0.0.0:8000`. After the first
successful start you can drop `TMASTER_BOOTSTRAP_PASSWORD` from the
environment.

### 1.5 Run as a background service

For a quick tmux-style foreground run use the command above. For a real
deployment, run it as a systemd unit:

```ini
# /etc/systemd/system/tmaster-server.service
[Unit]
Description=TMaster server
After=network.target

[Service]
User=tmaster
WorkingDirectory=/opt/TMaster/server
Environment=TMASTER_DATA_DIR=/var/lib/tmaster
Environment=TMASTER_JWT_SECRET_FILE=/var/lib/tmaster/jwt.secret
Environment=TMASTER_LISTEN_HOST=127.0.0.1
Environment=TMASTER_LISTEN_PORT=8000
ExecStart=/opt/TMaster/server/.venv/bin/python -m app.main
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Put a reverse proxy (Caddy / Nginx) in front of it for TLS — do **not**
expose plain `ws://` on the public internet. A reference Caddyfile:

```caddy
tmaster.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

(`docker-compose.yml` in the repo sketches a Caddy + server stack.)

### 1.6 Log in

Browse to `https://tmaster.example.com/` (or `http://127.0.0.1:8000/` for
local). Log in with the bootstrap credentials. You should land on an empty
**Workspaces** page.

### 1.7 Mint an enrollment token

On the dashboard, click **New enrollment token**. Copy the token — it is
single-use and expires in 1 hour. You will need it when running
`tmaster enroll` on each client machine.

---

## 2. Client side (one per managed machine)

### 2.1 Prereqs

```bash
# Debian/Ubuntu example
sudo apt install -y python3-venv tmux
```

### 2.2 Install

Clone the repo (or copy just the `agent/` directory) onto the target host:

```bash
git clone <repo-url> TMaster
cd TMaster

python3 -m venv agent/.venv
source agent/.venv/bin/activate
pip install -e ./agent
deactivate
```

The `agent/` package installs three console scripts:

- `tmaster-agent`   — the long-running daemon.
- `tmaster-sidecar` — launched by the agent itself, usually not run manually.
- `tmaster`         — local CLI (`tmaster workspace ls|open`, `tmaster enroll`).

### 2.3 Enroll the agent with the server

On the agent host, using the enrollment token minted in step 1.7:

```bash
source agent/.venv/bin/activate
tmaster enroll \
    --server-url wss://tmaster.example.com \
    --token <enrollment-token> \
    --agent-name "$(hostname)"
```

This call redeems the one-time token, receives a long-lived
`(agent_id, agent_token)` pair, and writes them to the agent config file
(default `~/.config/tmaster/agent.toml`). You can also set them via
environment variables (`TMASTER_AGENT_AGENT_ID`, `TMASTER_AGENT_AGENT_TOKEN`)
if you prefer to keep credentials out of on-disk config.

### 2.4 Run the agent

Foreground (for testing):

```bash
source agent/.venv/bin/activate
export TMASTER_AGENT_SERVER_URL=wss://tmaster.example.com
tmaster-agent
```

You should see `agent registered` messages both in the agent log and on
the server. The dashboard's **Agents** card updates within a few seconds.

Common environment variables (all prefixed `TMASTER_AGENT_`):

| Variable                                  | Default                                  | Purpose                              |
|-------------------------------------------|------------------------------------------|--------------------------------------|
| `TMASTER_AGENT_SERVER_URL`                | `ws://127.0.0.1:8000`                    | server base URL                      |
| `TMASTER_AGENT_AGENT_ID`                  | from config                              | agent identity                       |
| `TMASTER_AGENT_AGENT_TOKEN`               | from config                              | long-lived token                     |
| `TMASTER_AGENT_MACHINE_NAME`              | hostname                                 | display name                         |
| `TMASTER_AGENT_TMUX_BIN`                  | `tmux`                                   | tmux executable                      |
| `TMASTER_AGENT_DEFAULT_WORKSPACE_CWD`     | `$HOME`                                  | default workspace cwd                |
| `TMASTER_AGENT_SESSION_PREFIX`            | `tm_`                                    | only sessions with this prefix are managed |
| `TMASTER_AGENT_STATE_DIR`                 | `$XDG_STATE_HOME/tmaster`                | registry SQLite & logs               |
| `TMASTER_AGENT_RUNTIME_DIR`               | `$XDG_RUNTIME_DIR/tmaster`               | sidecar UDS sockets                  |
| `TMASTER_AGENT_TLS_INSECURE`              | `false`                                  | accept self-signed certs (dev only)  |

### 2.5 Run the agent as a systemd service

```ini
# ~/.config/systemd/user/tmaster-agent.service
[Unit]
Description=TMaster agent
After=default.target

[Service]
Environment=TMASTER_AGENT_SERVER_URL=wss://tmaster.example.com
ExecStart=%h/TMaster/agent/.venv/bin/tmaster-agent
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now tmaster-agent
journalctl --user -u tmaster-agent -f
```

### 2.6 Create and use a workspace

From the dashboard:
1. **Workspaces** page → pick an agent → give it a label and optional working
   directory → **Create workspace**. A real `tmux` session named
   `tm_<uuid8>` is created on that agent's host.
2. Click **Terminal** to attach the xterm-based remote terminal.
3. Click **Files** to browse / edit files under the workspace cwd
   (Monaco editor, save with `Ctrl+S`).
4. Click **Kill** to destroy the workspace (and tmux session).

Locally on the agent host, the same session is reachable through native
tmux:

```bash
tmaster workspace ls                 # lists TMaster-managed sessions
tmaster workspace open <label>       # exec tmux attach -t tm_<uuid8>
# or directly:
tmux attach -t tm_<uuid8>
```

Multiple dashboards can attach to the same workspace simultaneously; the
server's hub fans the PTY stream out.

---

## 3. Troubleshooting

| Symptom                                                | What to check                                                                 |
|--------------------------------------------------------|-------------------------------------------------------------------------------|
| Dashboard login returns 401                            | Bootstrap password wrong, or you dropped it after creating a different user. |
| Agent stuck in reconnect loop                          | Server URL/scheme (`wss://` behind TLS), token correct, clock skew < few min |
| Workspace shows `offline` in the UI                    | The owning agent disconnected. Check `systemctl --user status tmaster-agent`. |
| `sidecar never created the socket`                     | UDS path > 108 chars. Override `TMASTER_AGENT_RUNTIME_DIR` to a short path.   |
| `fs.list` fails with `forbidden`                       | Path escapes the workspace root (symlink or `..`). This is the sandbox working. |
| Typing in the terminal shows no output                 | `tmux` too old (< 3.0) or your shell has no `echo` on each line; try a fresh `bash --norc`. |

Server logs: `journalctl -u tmaster-server -f`.
Agent logs:  `journalctl --user -u tmaster-agent -f`.

---

## 4. What next

- See [`wiki.md`](wiki.md) for the architecture and implementation details.
- See [`docs/protocol.md`](docs/protocol.md) for the on-the-wire format if
  you want to build an alternative client.
