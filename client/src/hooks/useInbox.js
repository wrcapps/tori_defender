import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

const POLL_MS = 10000;

// Pending windows across every site. `error` is set only when no data has ever loaded; once there is data a
// failed poll just marks it stale (`reconnecting`) and the list stays usable.
export function useInbox(enabled = true) {
  const [windows, setWindows] = useState(null);
  const [error, setError] = useState(null);
  const [reconnecting, setReconnecting] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.inbox("*");
      setWindows(data.windows);
      setError(null);
      setReconnecting(false);
    } catch (err) {
      setReconnecting(true);
      setError((prev) => prev || (err && err.message) || "network-error");
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [enabled, load]);

  return { windows, error: windows === null ? error : null, reconnecting, reload: load };
}
