import { useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import Editor from "@monaco-editor/react";
import { useTMasterWS } from "@/lib/useWs";
import { MsgType, Ops, Scope, newEnvelope } from "@/lib/ws";

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

export default function Files() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const nav = useNavigate();
  const { ws, err: wsErr } = useTMasterWS();
  const [cwd, setCwd] = useState<string>(".");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [listErr, setListErr] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileBody, setFileBody] = useState<string>("");
  const [origBody, setOrigBody] = useState<string>("");
  const [fileErr, setFileErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadingFile, setLoadingFile] = useState(false);

  const dirty = fileBody !== origBody;
  const language = useMemo(
    () => (selectedPath ? languageFromPath(selectedPath) : undefined),
    [selectedPath],
  );

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

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-3 px-4 py-2 bg-slate-900 border-b border-slate-800 text-sm">
        <button
          onClick={() => nav("/")}
          className="text-slate-400 hover:text-white"
        >
          ← Back
        </button>
        <span className="text-slate-300 font-medium">{workspaceId}</span>
        <span className="text-slate-500">{cwd}</span>
        <span className="ml-auto text-xs text-slate-500">
          {ws ? "connected" : wsErr ?? "connecting…"}
        </span>
      </div>
      <div className="flex-1 min-h-0 flex">
        <div className="w-72 border-r border-slate-800 overflow-auto bg-slate-950">
          <div className="p-2 text-xs">
            <button
              onClick={() => setCwd(parentOf(cwd))}
              className="text-slate-400 hover:text-white"
              disabled={cwd === "."}
            >
              ← parent
            </button>
          </div>
          {listErr && <div className="px-3 py-1 text-rose-400 text-xs">{listErr}</div>}
          <ul className="text-sm">
            {entries.map((e) => {
              const full = joinPath(cwd, e.name);
              const isDir = e.type === "dir";
              return (
                <li
                  key={e.name}
                  onClick={() =>
                    isDir ? setCwd(full) : openFile(full)
                  }
                  className={
                    "px-3 py-1 cursor-pointer hover:bg-slate-800 " +
                    (selectedPath === full ? "bg-slate-800 text-sky-300" : "")
                  }
                >
                  <span className="mr-1">{isDir ? "📁" : "📄"}</span>
                  {e.name}
                </li>
              );
            })}
          </ul>
        </div>
        <div className="flex-1 min-w-0 flex flex-col">
          <div className="flex items-center gap-3 px-3 py-1.5 bg-slate-900 border-b border-slate-800 text-xs">
            <span className="text-slate-300">
              {selectedPath ?? "no file open"}
            </span>
            {dirty && <span className="text-amber-400">● unsaved</span>}
            {fileErr && <span className="text-rose-400">{fileErr}</span>}
            <button
              onClick={save}
              disabled={!selectedPath || !dirty || saving}
              className="ml-auto px-3 py-0.5 rounded bg-sky-600 hover:bg-sky-500 disabled:opacity-40 text-white"
            >
              {saving ? "Saving…" : "Save (Ctrl+S)"}
            </button>
          </div>
          <div className="flex-1 min-h-0">
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
              <div className="p-6 text-slate-500 text-sm">
                Choose a file on the left.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
