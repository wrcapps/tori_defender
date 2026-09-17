import { useEffect, useState } from "react";
import { api } from "../api.js";

// A one-shot fetch, not the polling useCameras() does -- Sightings and
// Species don't render video or live status, they only need to know which
// sites exist to populate a site switcher, and that doesn't change while
// someone is looking at a gallery.
export function useSiteNames() {
  const [sites, setSites] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.cameras()
      .then((rows) => {
        if (cancelled) return;
        setSites([...new Set(rows.map((r) => r.site))].sort());
      })
      .catch(() => !cancelled && setSites([]));
    return () => { cancelled = true; };
  }, []);

  return sites;
}
