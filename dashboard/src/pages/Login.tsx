import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const setTokens = useAuth((s) => s.setTokens);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = await api.login(username, password);
      setTokens(res.access_token, res.refresh_token, res.user_id);
      nav("/", { replace: true });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full flex items-center justify-center">
      <form
        onSubmit={submit}
        className="w-80 bg-slate-900 border border-slate-800 rounded-lg p-6 shadow-xl"
      >
        <h1 className="text-xl font-semibold mb-4 text-sky-400">TMaster</h1>
        <label className="block text-sm mb-1 text-slate-300">Username</label>
        <input
          className="w-full mb-3 px-2 py-1 rounded bg-slate-800 border border-slate-700 focus:outline-none focus:border-sky-500"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
        <label className="block text-sm mb-1 text-slate-300">Password</label>
        <input
          type="password"
          className="w-full mb-4 px-2 py-1 rounded bg-slate-800 border border-slate-700 focus:outline-none focus:border-sky-500"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {err && <div className="mb-3 text-rose-400 text-sm">{err}</div>}
        <button
          type="submit"
          disabled={busy}
          className="w-full py-1.5 rounded bg-sky-600 hover:bg-sky-500 text-white font-medium disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
