import { useAuth } from "./auth";

const API_BASE = "/api";

async function request<T>(
  path: string,
  init: RequestInit = {},
  auth = true,
): Promise<T> {
  const token = useAuth.getState().accessToken;
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (auth && token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(API_BASE + path, { ...init, headers });
  if (res.status === 401 && auth) {
    useAuth.getState().logout();
    throw new Error("unauthenticated");
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface TokenResponse {
  access_token: string;
  access_expires_at: number;
  refresh_token: string;
  refresh_expires_at: number;
  user_id: string;
}

export interface Workspace {
  id: string;
  agent_id: string;
  agent_online?: boolean;
  label: string;
  tmux_session_name: string;
  cwd: string | null;
  status: string;
  created_at?: number;
}

export const api = {
  login: (username: string, password: string) =>
    request<TokenResponse>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ username, password }) },
      false,
    ),
  refresh: (refresh_token: string) =>
    request<TokenResponse>(
      "/auth/refresh",
      { method: "POST", body: JSON.stringify({ refresh_token }) },
      false,
    ),
  listWorkspaces: () =>
    request<{ workspaces: Workspace[] }>("/workspaces"),
  listAgents: () => request<{ agents: Array<Record<string, unknown>> }>("/agents"),
  createEnrollmentToken: () =>
    request<{ token: string; expires_in: number }>(
      "/agents/enrollment-token",
      { method: "POST" },
    ),
};
