import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Workspace } from "@/lib/api";
import { useState } from "react";
import { useTMasterWS } from "@/lib/useWs";
import { MsgType, Ops, Scope, newEnvelope } from "@/lib/ws";

export default function Workspaces() {
  const qc = useQueryClient();
  const { ws, err: wsErr } = useTMasterWS();
  const q = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => (await api.listWorkspaces()).workspaces,
    refetchInterval: 5000,
  });
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: async () =>
      (await api.listAgents()).agents as Array<{
        id: string;
        name: string;
        last_seen_at?: number;
        created_at?: number;
      }>,
    refetchInterval: 10000,
  });

  const [enrollTok, setEnrollTok] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [newLabel, setNewLabel] = useState("");
  const [newCwd, setNewCwd] = useState("");
  const [newAgent, setNewAgent] = useState<string>("");
  const [showDeleted, setShowDeleted] = useState(false);

  async function createWorkspace(e: React.FormEvent) {
    e.preventDefault();
    if (!ws) return;
    if (!newAgent) {
      setActionErr("choose an agent");
      return;
    }
    setBusy(true);
    setActionErr(null);
    try {
      const resp = await ws.request(
        newEnvelope({
          scope: Scope.AGENT,
          target: newAgent,
          op: Ops.AGENT_WS_CREATE,
          type: MsgType.REQ,
          payload: {
            label: newLabel || "workspace",
            cwd: newCwd || undefined,
          },
        }),
      );
      if (!resp.ok) {
        setActionErr(resp.error?.message ?? "create failed");
      } else {
        setNewLabel("");
        setNewCwd("");
        qc.invalidateQueries({ queryKey: ["workspaces"] });
      }
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function killWorkspace(w: Workspace) {
    if (!ws) return;
    if (!confirm(`Kill workspace "${w.label}"?`)) return;
    try {
      const resp = await ws.request(
        newEnvelope({
          scope: Scope.AGENT,
          target: w.agent_id,
          op: Ops.AGENT_WS_KILL,
          type: MsgType.REQ,
          payload: { workspace_id: w.id },
        }),
      );
      if (!resp.ok) setActionErr(resp.error?.message ?? "kill failed");
      else qc.invalidateQueries({ queryKey: ["workspaces"] });
    } catch (e) {
      setActionErr((e as Error).message);
    }
  }

  async function mintToken() {
    try {
      const r = await api.createEnrollmentToken();
      setEnrollTok(r.token);
    } catch (e) {
      setActionErr((e as Error).message);
    }
  }

  const all = q.data ?? [];
  const live = all.filter((w) => w.status !== "deleted");
  const deleted = all.filter((w) => w.status === "deleted");

  return (
    <div className="p-4 sm:p-6 overflow-auto h-full">
      <div className="flex flex-wrap items-center mb-4 gap-3">
        <h1 className="text-2xl font-semibold">Workspaces</h1>
        <span className="text-xs text-slate-500">
          {ws ? "connected" : wsErr ?? "connecting…"}
        </span>
        <div className="ml-auto">
          <button
            onClick={mintToken}
            className="px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-sm"
          >
            New enrollment token
          </button>
        </div>
      </div>

      {enrollTok && (
        <div className="mb-4 p-3 bg-slate-900 border border-slate-800 rounded">
          <div className="text-xs text-slate-400 mb-1">
            Enrollment token (valid 1h; single-use):
          </div>
          <code className="text-emerald-400 break-all text-sm">{enrollTok}</code>
        </div>
      )}

      <h2 className="text-sm uppercase text-slate-400 tracking-wider mb-2">Agents</h2>
      <div className="mb-6 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {(agents.data ?? []).map((a) => (
          <div
            key={a.id}
            className="p-3 bg-slate-900 border border-slate-800 rounded text-sm"
          >
            <div className="font-medium">{a.name}</div>
            <div className="text-slate-500 text-xs mt-1 break-all">{a.id}</div>
            <div className="text-slate-400 text-xs">
              last seen:{" "}
              {a.last_seen_at
                ? new Date(a.last_seen_at * 1000).toLocaleString()
                : "never"}
            </div>
          </div>
        ))}
        {agents.data?.length === 0 && (
          <div className="text-slate-500 text-sm col-span-full">
            No agents enrolled yet. Mint an enrollment token and run{" "}
            <code>tmaster enroll</code> on a target machine.
          </div>
        )}
      </div>

      {(agents.data?.length ?? 0) > 0 && (
        <form
          onSubmit={createWorkspace}
          className="mb-6 p-3 bg-slate-900 border border-slate-800 rounded flex flex-wrap gap-2 items-end"
        >
          <div>
            <label className="block text-xs text-slate-400 mb-1">Agent</label>
            <select
              className="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm"
              value={newAgent}
              onChange={(e) => setNewAgent(e.target.value)}
            >
              <option value="">— choose —</option>
              {agents.data!.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">Label</label>
            <input
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
              placeholder="my-workspace"
              className="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm"
            />
          </div>
          <div className="flex-1 min-w-40">
            <label className="block text-xs text-slate-400 mb-1">Working dir</label>
            <input
              value={newCwd}
              onChange={(e) => setNewCwd(e.target.value)}
              placeholder="(agent default)"
              className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-sm"
            />
          </div>
          <button
            type="submit"
            disabled={busy || !ws}
            className="px-4 py-1.5 rounded bg-sky-600 hover:bg-sky-500 text-sm text-white disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create workspace"}
          </button>
        </form>
      )}

      {actionErr && (
        <div className="mb-4 text-rose-400 text-sm">{actionErr}</div>
      )}

      <h2 className="text-sm uppercase text-slate-400 tracking-wider mb-2">
        Active workspaces
      </h2>
      {q.isLoading && <div className="text-slate-400">Loading…</div>}
      {q.error && (
        <div className="text-rose-400">{(q.error as Error).message}</div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {live.map((w) => (
          <WorkspaceCard key={w.id} w={w} onKill={() => killWorkspace(w)} />
        ))}
        {live.length === 0 && !q.isLoading && (
          <div className="text-slate-500 text-sm col-span-full">
            No active workspaces.
          </div>
        )}
      </div>

      {deleted.length > 0 && (
        <div className="mt-6">
          <button
            onClick={() => setShowDeleted((v) => !v)}
            className="text-xs text-slate-400 hover:text-slate-200 flex items-center gap-1"
          >
            <span>{showDeleted ? "▾" : "▸"}</span>
            Deleted ({deleted.length})
          </button>
          {showDeleted && (
            <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 opacity-60">
              {deleted.map((w) => (
                <WorkspaceCard key={w.id} w={w} onKill={() => {}} deleted />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function WorkspaceCard({
  w,
  onKill,
  deleted,
}: {
  w: Workspace;
  onKill: () => void;
  deleted?: boolean;
}) {
  const offline = w.agent_online === false;
  const activity = w.activity;
  const cmd = w.current_command;
  return (
    <div className="p-4 bg-slate-900 border border-slate-800 rounded flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-medium text-sky-300 truncate">{w.label}</div>
          <div className="text-xs text-slate-400 mt-1 truncate">
            tmux: {w.tmux_session_name}
          </div>
          <div className="text-xs text-slate-500 mt-0.5 truncate">{w.cwd}</div>
        </div>
        <StatusBadge w={w} />
      </div>

      {!deleted && (
        <div className="flex items-center gap-2 text-xs">
          <span
            className={
              "inline-flex items-center gap-1.5 " +
              (activity === "busy"
                ? "text-amber-300"
                : activity === "idle"
                  ? "text-emerald-300"
                  : "text-slate-500")
            }
            title={
              w.last_activity_at
                ? `last activity: ${new Date(w.last_activity_at * 1000).toLocaleTimeString()}`
                : undefined
            }
          >
            <span
              className={
                "inline-block w-2 h-2 rounded-full " +
                (activity === "busy"
                  ? "bg-amber-400 animate-pulse"
                  : activity === "idle"
                    ? "bg-emerald-400"
                    : "bg-slate-600")
              }
            />
            {cmd ? cmd : activity ?? "—"}
          </span>
          {w.current_pid && (
            <span className="text-slate-500">pid {w.current_pid}</span>
          )}
        </div>
      )}

      {!deleted && (
        <div className="flex gap-2 mt-1">
          <Link
            to={`/w/${w.id}`}
            className="px-2 py-1 text-xs rounded bg-sky-700/60 hover:bg-sky-600 text-white"
          >
            Open
          </Link>
          <Link
            to={`/w/${w.id}?tab=files`}
            className="px-2 py-1 text-xs rounded bg-slate-800 hover:bg-slate-700"
          >
            Files
          </Link>
          <button
            onClick={onKill}
            disabled={offline}
            className="ml-auto px-2 py-1 text-xs rounded bg-rose-900/40 hover:bg-rose-800/60 text-rose-300 disabled:opacity-40"
          >
            Kill
          </button>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ w }: { w: Workspace }) {
  const offline = w.agent_online === false;
  const deleted = w.status === "deleted";
  const label = deleted ? "deleted" : offline ? "offline" : w.status;
  const cls = deleted
    ? "bg-slate-800 text-slate-500"
    : offline
      ? "bg-slate-700 text-slate-400"
      : "bg-emerald-700/40 text-emerald-300";
  return (
    <span className={`shrink-0 inline-block px-2 py-0.5 rounded text-xs ${cls}`}>
      {label}
    </span>
  );
}
