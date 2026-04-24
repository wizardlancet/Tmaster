# TMaster Wire Protocol

This document is normative for all TMaster components. Any change here implies a
coordinated bump of the protocol version handshake (see §1).

There are three transport hops:

```
Dashboard  <— WSS —>  Server  <— WSS —>  Agent  <— UDS —>  Sidecar
```

The same envelope format is used on every hop so that the server can (and does)
do pure routing for most ops without re-serialising payloads.

---

## 1. Handshake & versioning

All three transports start with a handshake frame after the socket is
established.

**Dashboard → Server** (WebSocket subprotocol `tmaster.dashboard.v1`, bearer
JWT either as `Sec-WebSocket-Protocol` second value or `?token=` query):

```json
{ "type": "hello", "proto": 1, "client": "dashboard", "version": "0.0.1" }
```

Server replies:

```json
{ "type": "hello_ack", "proto": 1, "session_id": "...", "user_id": "..." }
```

**Agent → Server** (`tmaster.agent.v1`, bearer agent token):

```json
{ "type": "hello", "proto": 1, "client": "agent", "agent_id": "...",
  "version": "0.0.1", "machine": { "hostname": "...", "os": "linux",
  "arch": "x86_64", "tmux_version": "3.4" },
  "workspaces": [ { "id": "...", "label": "...", "status": "running",
                    "tmux_session": "tm_ab12cd34", "cwd": "/home/x/proj" } ] }
```

Server replies with `hello_ack` plus any pending reconciliation commands.

**Agent → Sidecar** (UDS, single Agent owns each sidecar):
`hello` contains `{ workspace_id, protocol_caps: [...] }`.

If `proto` doesn't match, the peer closes with code 4001 (protocol mismatch).

---

## 2. Message envelope

Every non-handshake frame (control plane) is a JSON object:

```
Envelope := {
  "id":      string,              // ULID, unique per sender per connection
  "type":    "req" | "resp" | "event",
  "scope":   "agent" | "workspace" | "server",
  "target":  string | null,       // agent_id or workspace_id (null for server-local)
  "op":      string,              // dot-namespaced, see §4
  "payload": object,              // op-specific
  "ts":      number               // unix ms, advisory
}

// Additional fields for responses:
"in_reply_to": string             // id of the originating req
"ok":          boolean
"error":       { "code": string, "message": string, "details": object? }
```

Rules:

* `req` MUST be answered by exactly one `resp` (or the transport closes with
  all in-flight reqs rejected with `error.code = "transport_closed"`).
* `event` is fire-and-forget.
* `scope = "workspace"` messages are routed to the sidecar owning `target`;
  `scope = "agent"` messages are handled by the agent itself; `scope = "server"`
  is handled locally by the server.
* An intermediate node MAY rewrite `target` but MUST NOT change `id`, `op`, or
  `payload` for routed messages (except for server-injected errors with
  `in_reply_to`).

Size limit: 1 MiB per control-plane frame. Larger payloads use binary frames
(§3).

---

## 3. Binary frames (data plane)

WebSocket binary frames and the UDS data-plane socket both carry the same
layout to avoid re-encoding. All multi-byte fields are big-endian.

```
  0        1        2        3        4        5 ...
+--------+--------+--------+--------+--------+----------------+
|  tag   |          stream_id (uint32)        |   payload ... |
+--------+--------+--------+--------+--------+----------------+
```

| tag  | meaning                    | payload                                   |
|------|----------------------------|-------------------------------------------|
| 0x01 | PTY stdout (sidecar→...)   | raw terminal bytes                        |
| 0x02 | PTY input (...→sidecar)    | raw terminal bytes                        |
| 0x03 | PTY resize                 | `uint16 cols, uint16 rows`                |
| 0x10 | File chunk                 | `uint64 offset, bytes...`                 |
| 0x11 | File chunk EOF             | (empty) — stream complete                 |
| 0x12 | File abort                 | utf-8 reason                              |
| 0xFE | Stream open ack            | (empty) — sent once after `*.open` resp   |
| 0xFF | Stream close               | `uint8 code` (0 = ok, others = error)     |

`stream_id` is allocated by the initiator of a stream via a control-plane
`req` (see §4). Both directions share the same id space per connection.

Intermediate nodes (server, agent) MUST NOT rewrite `stream_id`; they maintain
per-hop mapping tables when multiplexing.

---

## 4. Operation catalogue

### 4.1 Agent scope (`scope: "agent"`)

| op                   | dir            | payload                               | resp                          |
|----------------------|----------------|---------------------------------------|-------------------------------|
| `agent.ping`         | S→A, A→S       | `{}`                                  | `{ uptime_s }`                |
| `agent.workspace.list` | S→A          | `{}`                                  | `{ workspaces: [...] }`       |
| `agent.workspace.create` | S→A        | `{ label, cwd, config }`              | `{ workspace: {...} }`        |
| `agent.workspace.kill` | S→A          | `{ workspace_id, force? }`            | `{}`                          |
| `agent.workspace.update` | A→S (event) | `{ workspace: {...} }`               | —                             |

### 4.2 Workspace scope (`scope: "workspace"`, routed to sidecar)

| op                      | dir            | payload                                | resp/notes                    |
|-------------------------|----------------|----------------------------------------|-------------------------------|
| `tmux.open`             | D→...→Sc       | `{ cols, rows }`                       | `{ stream_id }`               |
| `tmux.close`            | D→...→Sc       | `{ stream_id }`                        | `{}`                          |
| `tmux.resize`           | D→...→Sc       | `{ stream_id, cols, rows }`            | `{}` (or use tag 0x03)        |
| `tmux.state`            | Sc→...→D (evt) | `{ windows:[...], active_window, ... }`| —                             |
| `fs.list`               | D→...→Sc       | `{ path }`                             | `{ entries: [...] }`          |
| `fs.stat`               | D→...→Sc       | `{ path }`                             | `{ stat: {...} }`             |
| `fs.read`               | D→...→Sc       | `{ path, max_bytes? }`                 | `{ stream_id, size, mtime }`  |
| `fs.write`              | D→...→Sc       | `{ path, expected_mtime?, mode? }`     | `{ stream_id }`               |
| `fs.mkdir`              | D→...→Sc       | `{ path, parents? }`                   | `{}`                          |
| `fs.delete`             | D→...→Sc       | `{ path, recursive? }`                 | `{}`                          |
| `fs.rename`             | D→...→Sc       | `{ from, to }`                         | `{}`                          |
| `fs.upload`             | D→...→Sc       | `{ path, size, mode?, overwrite? }`    | `{ stream_id }`               |
| `fs.download`           | D→...→Sc       | `{ path }`                             | `{ stream_id, size }`         |
| `status.get`            | D→...→Sc       | `{}`                                   | `{ probes: {...} }`           |
| `status.update`         | Sc→...→D (evt) | `{ probes: {...}, overall }`           | —                             |

### 4.3 Server scope (`scope: "server"`, dashboard only)

| op                      | payload                          | resp                                    |
|-------------------------|----------------------------------|-----------------------------------------|
| `auth.refresh`          | `{ refresh_token }`              | `{ access_token, expires_at }`          |
| `workspace.list`        | `{}`                             | `{ workspaces: [...] }`                 |
| `audit.query`           | `{ since?, kind?, limit? }`      | `{ events: [...] }`                     |

---

## 5. Error codes

| code                  | meaning                                                     |
|-----------------------|-------------------------------------------------------------|
| `bad_request`         | Malformed envelope or payload                               |
| `unauthenticated`     | Missing/invalid token                                       |
| `forbidden`           | Authenticated but not allowed                               |
| `not_found`           | Agent / workspace / path missing                            |
| `conflict`            | mtime mismatch, duplicate label, etc.                       |
| `unavailable`         | Agent offline, sidecar crashed, etc.                        |
| `path_denied`         | Filesystem sandbox violation                                |
| `rate_limited`        | Too many requests                                           |
| `internal`            | Unexpected server/agent/sidecar error                       |
| `transport_closed`    | Connection dropped with reqs in flight                      |
| `proto_mismatch`      | Handshake protocol version disagreement                     |

---

## 6. UDS-specific notes (agent ↔ sidecar)

* Socket path: `$XDG_RUNTIME_DIR/tmaster/ws-<workspace_id>.sock`
  (fallback `/tmp/tmaster-<uid>/ws-<workspace_id>.sock`).
* File mode `0600`, parent directory `0700`.
* Sidecar verifies `SO_PEERCRED` uid matches its own uid; any mismatch → close
  with `proto_mismatch`.
* Framing on UDS:
  * Control plane: newline-delimited UTF-8 JSON (one envelope per line).
  * Data plane: length-prefixed frames — `uint32 length` followed by the
    binary frame from §3. The two planes share the same socket and are
    distinguished by a leading marker byte per "record":
    * `0x0A` (`\n`)  — not a marker; the byte itself; interpret rest of line as JSON until next `\n`.
    * `0x00`         — a length-prefixed binary frame follows (`uint32 len`, then `len` bytes of §3 layout).
  * Writers MUST ensure atomicity per record.

---

## 7. Reconciliation

When an agent reconnects after a disconnect:

1. Agent sends `hello` with its current `workspaces[]` (from its local registry).
2. Server compares against its last-known state and emits:
   * `agent.workspace.kill` for any workspace the server considers removed.
   * `agent.workspace.update` events back to dashboards for recovered state.
3. Dashboards re-subscribe to PTY streams on their own — no server-side
   persistence of stream state across reconnects.
