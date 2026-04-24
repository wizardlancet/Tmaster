import { useEffect, useRef, useState } from "react";
import { TMasterWS } from "./ws";
import { useAuth } from "./auth";

/** Creates a short-lived TMasterWS bound to component lifetime. */
export function useTMasterWS(): { ws: TMasterWS | null; err: string | null } {
  const token = useAuth((s) => s.accessToken);
  const [ws, setWs] = useState<TMasterWS | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<TMasterWS | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const w = new TMasterWS(token);
    ref.current = w;
    w.connect()
      .then(() => {
        if (!cancelled) setWs(w);
      })
      .catch((e) => {
        if (!cancelled) setErr((e as Error).message);
      });
    return () => {
      cancelled = true;
      w.close();
      ref.current = null;
    };
  }, [token]);

  return { ws, err };
}
