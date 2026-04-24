import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import {
  FrameTag,
  MsgType,
  Ops,
  Scope,
  TMasterWS,
  newEnvelope,
} from "@/lib/ws";
import { useAuth } from "@/lib/auth";

const encoder = new TextEncoder();

export default function Terminal() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const token = useAuth((s) => s.accessToken);
  const nav = useNavigate();
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("connecting…");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !workspaceId) return;
    const host = hostRef.current;
    if (!host) return;

    const term = new XTerm({
      fontFamily: '"JetBrains Mono", Menlo, Consolas, monospace',
      fontSize: 13,
      theme: { background: "#0b1020" },
      cursorBlink: true,
      scrollback: 5000,
      convertEol: false,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    try {
      fit.fit();
    } catch {
      // pre-layout
    }

    const ws = new TMasterWS(token);
    let streamId = 0;
    let disposed = false;
    const td = new TextDecoder();

    ws.onBinary((frame) => {
      if (frame.streamId !== streamId) return;
      if (frame.tag === FrameTag.PTY_OUT) {
        term.write(td.decode(frame.payload, { stream: true }));
      } else if (frame.tag === FrameTag.STREAM_CLOSE) {
        setStatus("closed");
      }
    });

    ws.onEnvelope((env) => {
      if (env.type === MsgType.EVENT && env.op === "tmux.state") {
        // future: window/pane tree updates
      }
    });

    term.onData((data) => {
      if (!streamId) return;
      try {
        ws.sendBinary(FrameTag.PTY_IN, streamId, encoder.encode(data));
      } catch {
        // ignore
      }
    });

    const resize = () => {
      try {
        fit.fit();
      } catch {
        return;
      }
      if (!streamId) return;
      const cols = term.cols;
      const rows = term.rows;
      ws.sendEnvelope(
        newEnvelope({
          scope: Scope.WORKSPACE,
          target: workspaceId,
          op: Ops.TMUX_RESIZE,
          type: MsgType.REQ,
          payload: { stream_id: streamId, cols, rows },
        }),
      );
    };
    const ro = new ResizeObserver(() => resize());
    ro.observe(host);

    (async () => {
      try {
        await ws.connect();
        if (disposed) return;
        // allocate a per-dashboard stream id (hub will remap)
        streamId = Math.floor(Math.random() * 2 ** 30) + 1;
        try {
          fit.fit();
        } catch {
          // ignore
        }
        const openResp = await ws.request(
          newEnvelope({
            scope: Scope.WORKSPACE,
            target: workspaceId,
            op: Ops.TMUX_OPEN,
            type: MsgType.REQ,
            payload: { stream_id: streamId, cols: term.cols, rows: term.rows },
          }),
        );
        if (!openResp.ok) {
          const msg = openResp.error?.message ?? "open failed";
          setErr(msg);
          return;
        }
        setStatus("attached");
      } catch (e) {
        setErr((e as Error).message);
      }
    })();

    return () => {
      disposed = true;
      ro.disconnect();
      try {
        if (streamId) {
          ws.sendEnvelope(
            newEnvelope({
              scope: Scope.WORKSPACE,
              target: workspaceId,
              op: Ops.TMUX_CLOSE,
              type: MsgType.REQ,
              payload: { stream_id: streamId },
            }),
          );
        }
      } catch {
        // ignore
      }
      ws.close();
      term.dispose();
    };
  }, [token, workspaceId]);

  return (
    <div className="h-full flex flex-col bg-[#0b1020]">
      <div className="flex items-center gap-3 px-4 py-2 bg-slate-900 border-b border-slate-800 text-sm">
        <button
          onClick={() => nav("/")}
          className="text-slate-400 hover:text-white"
        >
          ← Back
        </button>
        <span className="text-slate-300 font-medium">{workspaceId}</span>
        <span className="ml-auto text-xs text-slate-400">{status}</span>
        {err && <span className="text-rose-400 text-xs">{err}</span>}
      </div>
      <div ref={hostRef} className="flex-1 min-h-0" />
    </div>
  );
}
