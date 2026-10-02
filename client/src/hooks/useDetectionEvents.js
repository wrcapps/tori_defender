import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Own poll loop, deliberately separate from useCameras.js's 3s interval --
// "camera is online" and "a bird was just detected" are different concerns
// with different freshness needs (detector FPS vs render FPS, kept apart
// the same way everywhere else in this app), so this doesn't piggyback on
// that hook or its interval.
const POLL_MS = 4000;
const MAX_EVENTS = 20; // never let the notification queue grow unbounded

export function useDetectionEvents() {
  const [events, setEvents] = useState([]);
  const [latestByCamera, setLatestByCamera] = useState({});
  const seen = useRef(new Set());
  const firstLoad = useRef(true);
  // Server timestamps throughout, so a browser with a skewed clock cannot hide or replay events.
  const liveSince = useRef(0);

  const load = useCallback(async () => {
    let rows;
    try {
      rows = await api.recentDetections();
    } catch {
      return; // a missed poll just tries again in POLL_MS -- no error UI for a background feed
    }

    // Notifications from the acquisition service: new tracks on enabled/recording cameras,
    // whether or not anything was saved.
    try {
      const live = await api.liveEvents(liveSince.current);
      const fresh = live.filter((r) => !seen.current.has(r.id));
      if (firstLoad.current) {
        // Whatever was already there when this page opened is history, not news.
        fresh.forEach((r) => seen.current.add(r.id));
        liveSince.current = Math.max(0, ...fresh.map((r) => r.ts));
      } else if (fresh.length) {
        fresh.forEach((r) => seen.current.add(r.id));
        liveSince.current = Math.max(liveSince.current, ...fresh.map((r) => r.ts));
        setEvents((prev) => [...fresh.map((r) => ({ ...r, kind: "live" })), ...prev].slice(0, MAX_EVENTS));
      }
    } catch {
      /* the next poll tries again */
    }

    const byCamera = {};
    for (const row of rows) byCamera[`${row.site}/${row.camera}`] = row;
    setLatestByCamera(byCamera);

    // On the very first load, every row is "already there" -- only treat
    // rows seen *after* this hook mounted as new notifications, otherwise
    // opening the app would instantly toast every recent detection at once.
    // A track is "seen" at one risk level: if its risk changes (a distance was just
    // entered, or the zone was crossed) it is news again, with the new decision.
    const keyOf = (row) => `${row.id}|${row.risk || ""}`;
    if (firstLoad.current) {
      for (const row of rows) seen.current.add(keyOf(row));
      firstLoad.current = false;
      return;
    }

    const fresh = rows.filter((row) => !seen.current.has(keyOf(row)));
    if (fresh.length === 0) return;
    for (const row of fresh) seen.current.add(keyOf(row));
    // Re-alerting an id replaces its older entry rather than stacking a second one.
    setEvents((prev) => [...fresh, ...prev.filter((e) => !fresh.some((f) => f.id === e.id))]
      .slice(0, MAX_EVENTS));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  function dismiss(id) {
    setEvents((prev) => prev.filter((e) => e.id !== id));
  }

  function clearAll() {
    setEvents([]);
  }

  return { events, latestByCamera, dismiss, clearAll };
}
