// WebSocket client mirroring the tmaster.common protocol.
//
// - Text frames are JSON envelopes (see docs/protocol.md §2).
// - Binary frames follow `[1B tag][4B stream_id BE][payload]`.

export const PROTOCOL_VERSION = 1;

export const FrameTag = {
  PTY_OUT: 0x01,
  PTY_IN: 0x02,
  PTY_RESIZE: 0x03,
  FILE_CHUNK: 0x10,
  FILE_EOF: 0x11,
  FILE_ABORT: 0x12,
  STREAM_OPEN_ACK: 0xfe,
  STREAM_CLOSE: 0xff,
} as const;
export type FrameTagValue = (typeof FrameTag)[keyof typeof FrameTag];

export const Scope = { AGENT: "agent", WORKSPACE: "workspace", SERVER: "server" } as const;
export type ScopeValue = (typeof Scope)[keyof typeof Scope];

export const MsgType = { REQ: "req", RESP: "resp", EVENT: "event" } as const;
export type MsgTypeValue = (typeof MsgType)[keyof typeof MsgType];

export const Ops = {
  TMUX_OPEN: "tmux.open",
  TMUX_CLOSE: "tmux.close",
  TMUX_RESIZE: "tmux.resize",
  FS_LIST: "fs.list",
  FS_READ: "fs.read",
  FS_WRITE: "fs.write",
  SUBSCRIBE: "subscribe",
  UNSUBSCRIBE: "unsubscribe",
  AGENT_WS_CREATE: "agent.workspace.create",
  AGENT_WS_KILL: "agent.workspace.kill",
  AGENT_WS_LIST: "agent.workspace.list",
} as const;

export interface Envelope {
  id?: string;
  type: MsgTypeValue;
  scope: ScopeValue;
  target?: string | null;
  op: string;
  payload?: Record<string, unknown>;
  ts?: number;
  in_reply_to?: string | null;
  ok?: boolean | null;
  error?: { code: string; message: string; details?: Record<string, unknown> } | null;
}

export interface BinaryFrame {
  tag: FrameTagValue;
  streamId: number;
  payload: Uint8Array;
}

function uuidHex(): string {
  // Random 32-char hex id matching Python uuid4().hex
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  let s = "";
  for (const b of a) s += b.toString(16).padStart(2, "0");
  return s;
}

export function newEnvelope(partial: Omit<Envelope, "id" | "ts" | "type"> & {
  type?: MsgTypeValue;
}): Envelope {
  return {
    id: uuidHex(),
    type: partial.type ?? MsgType.REQ,
    ts: Date.now(),
    payload: partial.payload ?? {},
    ...partial,
  };
}

export function encodeBinaryFrame(tag: FrameTagValue, streamId: number, payload: Uint8Array): ArrayBuffer {
  const buf = new ArrayBuffer(5 + payload.byteLength);
  const view = new DataView(buf);
  view.setUint8(0, tag);
  view.setUint32(1, streamId >>> 0, false); // big-endian
  new Uint8Array(buf, 5).set(payload);
  return buf;
}

export function decodeBinaryFrame(buf: ArrayBuffer): BinaryFrame {
  const view = new DataView(buf);
  const tag = view.getUint8(0) as FrameTagValue;
  const streamId = view.getUint32(1, false);
  const payload = new Uint8Array(buf, 5);
  return { tag, streamId, payload };
}

export type EnvelopeHandler = (env: Envelope) => void;
export type BinaryHandler = (frame: BinaryFrame) => void;

export class TMasterWS {
  private ws: WebSocket | null = null;
  private envHandlers = new Set<EnvelopeHandler>();
  private binHandlers = new Set<BinaryHandler>();
  private pending = new Map<string, (e: Envelope) => void>();
  private opened = false;
  public dashboardId: string | null = null;
  private _onOpen: (() => void) | null = null;
  private _onClose: ((ev: CloseEvent) => void) | null = null;

  constructor(private token: string) {}

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${window.location.host}/ws/dashboard?token=${encodeURIComponent(this.token)}`;
      const ws = new WebSocket(url, "tmaster.dashboard.v1");
      ws.binaryType = "arraybuffer";
      this.ws = ws;

      let handshakeDone = false;

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "hello", proto: PROTOCOL_VERSION }));
      };

      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          if (!handshakeDone) {
            try {
              const msg = JSON.parse(ev.data);
              if (msg.type === "hello_ack") {
                handshakeDone = true;
                this.opened = true;
                this.dashboardId = msg.dashboard_id;
                this._onOpen?.();
                resolve();
                return;
              }
            } catch {
              // fall through
            }
          }
          try {
            const env = JSON.parse(ev.data) as Envelope;
            if (env.type === MsgType.RESP && env.in_reply_to) {
              const cb = this.pending.get(env.in_reply_to);
              if (cb) {
                this.pending.delete(env.in_reply_to);
                cb(env);
                return;
              }
            }
            for (const h of this.envHandlers) h(env);
          } catch (e) {
            console.warn("bad envelope from server", e);
          }
        } else {
          const frame = decodeBinaryFrame(ev.data as ArrayBuffer);
          for (const h of this.binHandlers) h(frame);
        }
      };

      ws.onerror = () => {
        if (!handshakeDone) reject(new Error("ws error"));
      };

      ws.onclose = (ev) => {
        this.opened = false;
        this._onClose?.(ev);
        if (!handshakeDone) reject(new Error(`ws closed: ${ev.code}`));
      };
    });
  }

  onOpen(fn: () => void) {
    this._onOpen = fn;
  }
  onClose(fn: (ev: CloseEvent) => void) {
    this._onClose = fn;
  }

  onEnvelope(fn: EnvelopeHandler): () => void {
    this.envHandlers.add(fn);
    return () => this.envHandlers.delete(fn);
  }
  onBinary(fn: BinaryHandler): () => void {
    this.binHandlers.add(fn);
    return () => this.binHandlers.delete(fn);
  }

  sendEnvelope(env: Envelope) {
    if (!this.ws || !this.opened) throw new Error("ws not open");
    this.ws.send(JSON.stringify(env));
  }

  sendBinary(tag: FrameTagValue, streamId: number, payload: Uint8Array) {
    if (!this.ws || !this.opened) throw new Error("ws not open");
    this.ws.send(encodeBinaryFrame(tag, streamId, payload));
  }

  request(env: Envelope, timeoutMs = 10_000): Promise<Envelope> {
    if (!env.id) env.id = uuidHex();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(env.id!);
        reject(new Error(`request ${env.op} timed out`));
      }, timeoutMs);
      this.pending.set(env.id!, (resp) => {
        clearTimeout(timer);
        resolve(resp);
      });
      try {
        this.sendEnvelope(env);
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(env.id!);
        reject(e);
      }
    });
  }

  close() {
    try {
      this.ws?.close();
    } catch {
      // ignore
    }
  }
}
