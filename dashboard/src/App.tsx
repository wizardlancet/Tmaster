import { Routes, Route, Navigate, Link, useLocation } from "react-router-dom";
import Login from "@/pages/Login";
import Workspaces from "@/pages/Workspaces";
import Terminal from "@/pages/Terminal";
import Files from "@/pages/Files";
import { useAuth } from "@/lib/auth";

function RequireAuth({ children }: { children: JSX.Element }) {
  const token = useAuth((s) => s.accessToken);
  const loc = useLocation();
  if (!token) return <Navigate to="/login" replace state={{ from: loc }} />;
  return children;
}

function Nav() {
  const logout = useAuth((s) => s.logout);
  const token = useAuth((s) => s.accessToken);
  if (!token) return null;
  return (
    <nav className="flex items-center gap-4 px-4 py-2 bg-slate-900 border-b border-slate-800">
      <Link to="/" className="font-semibold text-sky-400">TMaster</Link>
      <Link to="/" className="text-slate-300 hover:text-white">Workspaces</Link>
      <div className="ml-auto">
        <button onClick={logout} className="text-slate-400 hover:text-white text-sm">
          Log out
        </button>
      </div>
    </nav>
  );
}

export default function App() {
  return (
    <div className="flex flex-col h-full">
      <Nav />
      <div className="flex-1 min-h-0">
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <Workspaces />
              </RequireAuth>
            }
          />
          <Route
            path="/terminal/:workspaceId"
            element={
              <RequireAuth>
                <Terminal />
              </RequireAuth>
            }
          />
          <Route
            path="/files/:workspaceId"
            element={
              <RequireAuth>
                <Files />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  );
}
