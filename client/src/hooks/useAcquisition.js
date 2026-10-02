import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api.js";

// The service's state as the header button shows it. Polled on its own interval, separate from
// useCameras (camera health) and useDetectionEvents (what was seen): whether acquisition is
// running is a third question with its own freshness.
const POLL_MS = 3000;

export function useAcquisition() {
  const [status, setStatus] = useState(null);       // null until the first answer
  const [error, setError] = useState(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const failures = useRef(0);

  const load = useCallback(async () => {
    try {
      setStatus(await api.acquisition());
      setError(null);
      setReconnecting(false);
      failures.current = 0;
    } catch (err) {
      failures.current += 1;
      if (failures.current >= 2) setReconnecting(true);
      if (err instanceof ApiError) setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  // Optimistic only for the button's own busy state: what is shown as on/off always comes from
  // the server's answer, never from the click.
  const setEnabled = useCallback(async (enabled) => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.setAcquisition(enabled));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not reach the server");
    } finally {
      setBusy(false);
    }
  }, []);

  return { status, error, reconnecting, busy, setEnabled };
}
