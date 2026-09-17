import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api.js";

// Polling today (every 3s) -- ARCHITECTURE.md ADR-2 proposes replacing this
// with a pushed WebSocket/SSE channel once it exists (Milestone 4). Every
// page that needs live camera state shares this one hook so that swap only
// has to happen in one place.
const POLL_MS = 3000;

export function useCameras() {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [reconnecting, setReconnecting] = useState(false);
  const failCount = useRef(0);

  const load = useCallback(async () => {
    try {
      const data = await api.cameras();
      setRows(data);
      setError(null);
      setReconnecting(false);
      failCount.current = 0;
    } catch (err) {
      failCount.current += 1;
      if (failCount.current >= 2) setReconnecting(true);
      if (err instanceof ApiError) setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return { rows, error, reconnecting, reload: load };
}
