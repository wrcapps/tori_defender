import { useEffect, useState } from "react";
import { api } from "../api.js";

const POLL_MS = 15000;

// Is the data folder (the NAS) reachable right now? Polled, because it can drop while the app runs.
// `unknown` = the status request itself failed (server unreachable): the pill says so instead of
// guessing, and the shell's own reconnecting state covers the rest.
export function useStorage() {
  const [storage, setStorage] = useState({ loading: true });

  useEffect(() => {
    let cancelled = false;
    const poll = () =>
      api.storage()
        .then((s) => !cancelled && setStorage(s))
        .catch(() => !cancelled && setStorage({ unknown: true }));
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return storage;
}
