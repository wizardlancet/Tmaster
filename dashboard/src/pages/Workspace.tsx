import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import Editor from "@monaco-editor/react";
import {
  FrameTag,
  MsgType,
  Ops,
  Scope,
  TMasterWS,
  newEnvelope,
} from "@/lib/ws";
import { useAuth } from "@/lib/auth";

/**
 * Unified workspace page: Terminal + Files in a single view.
 *
 * Layout:
 *  - Desktop (>= lg): horizontal split, Files on the left (resizable), Terminal on the right.
 *  - Mobile (< lg):   tab switcher, one panel at a time.
 *
 * A single TMasterWS connection is used for both panels so we don't pay the
 * handshake cost twice.
 */

const encoder = new TextEncoder();

interface DirEntry {
  name: string;
  type: "dir" | "file" | "symlink" | "other";
  size?: number;
}

const languageFromPath = (p: string): string | undefined => {
  const ext = p.split(".").pop()?.toLowerCase();
  const map: Record<string, string> = {
    ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
    py: "python", go: "go", rs: "rust", java: "java", c: "c", h: "c",
    cpp: "cpp", cc: "cpp", hpp: "cpp", cs: "csharp", rb: "ruby", php: "php",
    md: "markdown", json: "json", yaml: "yaml", yml: "yaml", toml: "toml",
    sh: "shell", bash: "shell", zsh: "shell", sql: "sql", html: "html",
    css: "css", scss: "scss", xml: "xml",
  };
  return ext ? map[ext] : undefined;
};

type Tab = "terminal" | "files";

function useIsMobile(breakpointPx = 1024): boolean {
  const [is, setIs] = useState(
    typeof window !== "undefined" ? window.innerWidth < breakpointPx : false,
  );
  useEffect(() => {
    const onResize = () => setIs(window.innerWidth < breakpointPx);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [breakpointPx]);
  return is;
}

export default function Workspace() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const [search, setSearch] = useSearchParams();
  const nav = useNavigate();
  const token = useAuth((s) => s.accessToken);
  const isMobile = useIsMobile();

  const initialTab = (search.get("tab") === "files" ? "files" : "terminal") as Tab;
  const [tab, setTab] = useState<Tab>(initialTab);
  // Desktop split ratio (0.15..0.55 of the width, Files panel).
  const [splitPct, setSplitPct] = useState(() => {
    const n = Number(localStorage.getItem("tm.splitPct"));
    return Number.isFinite(n) && n > 0.1 && n < 0.8 ? n : 0.3;
  });
  useEffect(() => {
    localStorage.setItem("tm.splitPct", String(splitPct));
  }, [splitPct]);

  const [wsReady, setWsReady] = useState(false);
  const [wsErr, setWsErr] = useState<string | null>(null);
  const wsRef = useRef<TMasterWS | null>(null);

  useEffect(() => {
    if (!token) return;
    const ws = new TMasterWS(token);
    wsRef.current = ws;
    let disposed = false;
    (async () => {
      try {
        await ws.connect();
        if (!disposed) setWsReady(true);
      } catch (e) {
        if (!disposed) setWsErr((e as Error).message);
      }
    })();
    return () => {
      disposed = true;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      wsRef.current = null;
    };
  }, [token]);

  // Keep URL ?tab= in sync on mobile (harmless on desktop).
  useEffect(() => {
    if (isMobile) {
      const next = new URLSearchParams(search);
      next.set("tab", tab);
      setSearch(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, isMobile]);

  return (
    <div className="h-full flex flex-col bg-[#0b1020]">
      <div className="flex items-center gap-2 px-3 py-2 bg-slate-900 border-b border-slate-800 text-sm">
        <button
          onClick={() => nav("/")}
          className="text-slate-400 hover:text-white"
        >
          ← Back
        </button>
        <span className="text-slate-300 font-medium truncate max-w-[14rem] sm:max-w-none">
          {workspaceId}
        </span>
        {isMobile && (
          <div className="ml-auto flex text-xs rounded overflow-hidden border border-slate-700">
            <button
              onClick={() => setTab("terminal")}
              className={`px-3 py-1 ${tab === "terminal" ? "bg-sky-600 text-white" : "bg-slate-800 text-slate-300"}`}
            >
              Terminal
            </button>
            <button
              onClick={() => setTab("files")}
              className={`px-3 py-1 ${tab === "files" ? "bg-sky-600 text-white" : "bg-slate-800 text-slate-300"}`}
            >
              Files
            </button>
          </div>
        )}
        <span className={`ml-auto ${isMobile ? "hidden" : ""} text-xs text-slate-400`}>
          {wsReady ? "connected" : wsErr ?? "connecting…"}
        </span>
      </div>

      <div className="flex-1 min-h-0 flex">
        {/* Files panel */}
        {(!isMobile || tab === "files") && (
          <div
            className="min-w-0 h-full border-r border-slate-800"
            style={{
              width: isMobile ? "100%" : `${splitPct * 100}%`,
            }}
          >
            <FilesPanel
              ws={wsReady ? wsRef.current : null}
              workspaceId={workspaceId ?? ""}
            />
          </div>
        )}

        {/* Drag handle */}
        {!isMobile && (
          <ResizeHandle
            onDrag={(dx, totalWidth) => {
              const delta = dx / totalWidth;
              setSplitPct((p) => Math.min(0.65, Math.max(0.12, p + delta)));
            }}
          />
        )}

        {/* Terminal panel */}
        {(!isMobile || tab === "terminal") && (
          <div className="flex-1 min-w-0 h-full">
            <TerminalPanel
              ws={wsReady ? wsRef.current : null}
              workspaceId={workspaceId ?? ""}
              // On mobile, we fully unmount when tab switches, closing the stream.
              // On desktop, it stays mounted.
              visible={!isMobile || tab === "terminal"}
            />
          </div>
        )}
      </div>
    </div>
  );
}

function ResizeHandle({
  onDrag,
}: {
  onDrag: (dx: number, totalWidth: number) => void;
}) {
  const dragging = useRef(false);
  const startX = useRef(0);
  const parentWidth = useRef(1);
  return (
    <div
      onMouseDown={(e) => {
        dragging.current = true;
        startX.current = e.clientX;
        parentWidth.current = (e.currentTarget.parentElement?.clientWidth) || 1;
        const onMove = (ev: MouseEvent) => {
          if (!dragging.current) return;
          onDrag(ev.clientX - startX.current, parentWidth.current);
          startX.current = ev.clientX;
        };
        const onUp = () => {
          dragging.current = false;
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
      }}
      className="w-1 cursor-col-resize bg-slate-800 hover:bg-sky-500/40"
    />
  );
}

// ---------------- Terminal panel ----------------

function TerminalPanel({
  ws,
  workspaceId,
  visible,
}: {
  ws: TMasterWS | null;
  workspaceId: string;
  visible: boolean;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("connecting…");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!ws || !workspaceId || !visible) return;
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
      /* pre-layout */
    }

    let streamId = 0;
    let disposed = false;
    const td = new TextDecoder();

    const offBin = ws.onBinary((frame) => {
      if (frame.streamId !== streamId) return;
      if (frame.tag === FrameTag.PTY_OUT) {
        term.write(td.decode(frame.payload, { stream: true }));
      } else if (frame.tag === FrameTag.STREAM_CLOSE) {
        setStatus("closed");
      }
    });

    term.onData((data) => {
      if (!streamId) return;
      try {
        ws.sendBinary(FrameTag.PTY_IN, streamId, encoder.encode(data));
      } catch {
        /* ignore */
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
        streamId = Math.floor(Math.random() * 2 ** 30) + 1;
        try {
          fit.fit();
        } catch {
          /* ignore */
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
        if (disposed) return;
        if (!openResp.ok) {
          setErr(openResp.error?.message ?? "open failed");
          return;
        }
        setStatus("attached");
        term.focus();
      } catch (e) {
        setErr((e as Error).message);
      }
    })();

    return () => {
      disposed = true;
      ro.disconnect();
      offBin();
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
        /* ignore */
      }
      term.dispose();
    };
  }, [ws, workspaceId, visible]);

  return (
    <div className="h-full flex flex-col bg-[#0b1020]">
      <div className="flex items-center gap-3 px-3 py-1 bg-slate-900/50 text-xs text-slate-400 border-b border-slate-800">
        <span>Terminal</span>
        <span className="ml-auto">{err ? <span className="text-rose-400">{err}</span> : status}</span>
      </div>
      <div ref={hostRef} className="flex-1 min-h-0" />
    </div>
  );
}

// ---------------- Files panel ----------------

function FilesPanel({
  ws,
  workspaceId,
}: {
  ws: TMasterWS | null;
  workspaceId: string;
}) {
  const [cwd, setCwd] = useState<string>(".");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [listErr, setListErr] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileBody, setFileBody] = useState<string>("");
  const [origBody, setOrigBody] = useState<string>("");
  const [fileErr, setFileErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadingFile, setLoadingFile] = useState(false);
  const [showEditor, setShowEditor] = useState(false);

  const dirty = fileBody !== origBody;
  const language = selectedPath ? languageFromPath(selectedPath) : undefined;

  useEffect(() => {
    if (!ws || !workspaceId) return;
    let cancelled = false;
    (async () => {
      setListErr(null);
      try {
        const resp = await ws.request(
          newEnvelope({
            scope: Scope.WORKSPACE,
            target: workspaceId,
            op: Ops.FS_LIST,
            type: MsgType.REQ,
            payload: { path: cwd },
          }),
        );
        if (cancelled) return;
        if (!resp.ok) {
          setListErr(resp.error?.message ?? "list failed");
          return;
        }
        const raw = (resp.payload?.entries ?? []) as DirEntry[];
        const sorted = [...raw].sort((a, b) => {
          if (a.type === "dir" && b.type !== "dir") return -1;
          if (b.type === "dir" && a.type !== "dir") return 1;
          return a.name.localeCompare(b.name);
        });
        setEntries(sorted);
      } catch (e) {
        if (!cancelled) setListErr((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ws, workspaceId, cwd]);

  function joinPath(a: string, b: string): string {
    if (a === "." || a === "") return b;
    if (a.endsWith("/")) return a + b;
    return a + "/" + b;
  }

  function parentOf(p: string): string {
    if (p === "." || p === "") return ".";
    const idx = p.lastIndexOf("/");
    if (idx < 0) return ".";
    return p.slice(0, idx) || ".";
  }

  async function openFile(path: string) {
    if (!ws || !workspaceId) return;
    if (dirty && !confirm("Discard unsaved changes?")) return;
    setSelectedPath(path);
    setShowEditor(true);
    setFileErr(null);
    setLoadingFile(true);
    try {
      const resp = await ws.request(
        newEnvelope({
          scope: Scope.WORKSPACE,
          target: workspaceId,
          op: Ops.FS_READ,
          type: MsgType.REQ,
          payload: { path },
        }),
      );
      if (!resp.ok) {
        setFileErr(resp.error?.message ?? "read failed");
        setFileBody("");
        setOrigBody("");
        return;
      }
      const b64 = (resp.payload?.content_b64 ?? "") as string;
      let text: string;
      try {
        text = new TextDecoder("utf-8", { fatal: false }).decode(
          Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)),
        );
      } catch {
        text = "";
        setFileErr("binary file — preview not supported");
      }
      setFileBody(text);
      setOrigBody(text);
    } catch (e) {
      setFileErr((e as Error).message);
    } finally {
      setLoadingFile(false);
    }
  }

  async function save() {
    if (!ws || !workspaceId || !selectedPath) return;
    setSaving(true);
    setFileErr(null);
    try {
      const enc = new TextEncoder().encode(fileBody);
      let bin = "";
      for (const b of enc) bin += String.fromCharCode(b);
      const b64 = btoa(bin);
      const resp = await ws.request(
        newEnvelope({
          scope: Scope.WORKSPACE,
          target: workspaceId,
          op: Ops.FS_WRITE,
          type: MsgType.REQ,
          payload: { path: selectedPath, content_b64: b64 },
        }),
      );
      if (!resp.ok) setFileErr(resp.error?.message ?? "write failed");
      else setOrigBody(fileBody);
    } catch (e) {
      setFileErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  // On mobile, editor takes over full panel when a file is open.
  return (
    <div className="h-full flex flex-col bg-slate-950">
      <div className="flex items-center gap-2 px-3 py-1 bg-slate-900 border-b border-slate-800 text-xs">
        {showEditor ? (
          <button
            onClick={() => setShowEditor(false)}
            className="text-slate-400 hover:text-white"
          >
            ← Files
          </button>
        ) : (
          <button
            onClick={() => setCwd(parentOf(cwd))}
            className="text-slate-400 hover:text-white disabled:opacity-40"
            disabled={cwd === "."}
          >
            ← parent
          </button>
        )}
        <span className="text-slate-300 truncate">
          {showEditor ? (selectedPath ?? "—") : cwd}
        </span>
        {showEditor && dirty && <span className="text-amber-400">● unsaved</span>}
        {showEditor && (
          <button
            onClick={save}
            disabled={!selectedPath || !dirty || saving}
            className="ml-auto px-2 py-0.5 rounded bg-sky-600 hover:bg-sky-500 disabled:opacity-40 text-white"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        )}
      </div>
      {showEditor ? (
        <div className="flex-1 min-h-0">
          {fileErr && <div className="px-3 py-1 text-rose-400 text-xs">{fileErr}</div>}
          {loadingFile ? (
            <div className="p-4 text-slate-500 text-sm">Loading…</div>
          ) : selectedPath ? (
            <Editor
              theme="vs-dark"
              language={language}
              value={fileBody}
              onChange={(v) => setFileBody(v ?? "")}
              onMount={(ed, monaco) => {
                ed.addCommand(
                  monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS,
                  () => save(),
                );
              }}
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                automaticLayout: true,
                wordWrap: "on",
              }}
            />
          ) : (
            <div className="p-6 text-slate-500 text-sm">Select a file.</div>
          )}
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-auto">
          {listErr && <div className="px-3 py-1 text-rose-400 text-xs">{listErr}</div>}
          <ul className="text-sm">
            {entries.map((e) => {
              const full = joinPath(cwd, e.name);
              const isDir = e.type === "dir";
              return (
                <li
                  key={e.name}
                  onClick={() => (isDir ? setCwd(full) : openFile(full))}
                  className={
                    "px-3 py-1 cursor-pointer hover:bg-slate-800 truncate " +
                    (selectedPath === full ? "bg-slate-800 text-sky-300" : "")
                  }
                >
                  <span className="mr-1">{isDir ? "📁" : "📄"}</span>
                  {e.name}
                </li>
              );
            })}
            {entries.length === 0 && !listErr && (
              <li className="px-3 py-2 text-slate-500 text-xs">empty</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
