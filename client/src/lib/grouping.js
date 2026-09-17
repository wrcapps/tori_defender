// Groups camera rows from /api/cameras by site, and within a site by mast
// ("pair" in the backend's own terms -- see camera_config.py's Camera.pair
// and the root README's "two cameras, same pair, one mast" convention).
//
// Deliberately data-driven, not hardcoded: earlier drafts of this page
// considered hardcoding Babadag's "6 masts, 60 degrees apart" geometry
// (see the camera-mast-pairing memory) directly into the UI. That was
// dropped -- a wrong or stale hardcoded mapping would show a client an
// inaccurate diagram with no way to notice, whereas grouping by the
// backend's own `pair` field is always exactly as accurate as the config
// is. A camera with no `pair` set becomes its own singleton group (this is
// simply the honest fallback -- Corbu's single camera never had a pair to
// begin with).

export function groupBySite(rows) {
  const out = {};
  for (const row of rows) {
    (out[row.site] ||= []).push(row);
  }
  return out;
}

export function groupByMast(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = row.pair != null ? `pair:${row.pair}` : `solo:${row.name}`;
    if (!groups.has(key)) groups.set(key, { key, pair: row.pair, cameras: [] });
    groups.get(key).cameras.push(row);
  }
  return [...groups.values()].sort((a, b) => a.key.localeCompare(b.key, undefined, { numeric: true }));
}

// A camera-level status kind, shared with StatusPill's vocabulary, reduced
// to whatever a summary card needs: healthiest wins so a mast/site with one
// good camera doesn't read as broken because another is still starting up
// (starting is optimistic; only "no signal after having tried" pulls the
// whole group down to red).
const RANK = { live: 0, starting: 1, "live-no-detect": 2, stale: 3, "no-signal": 4, error: 5 };

export function summarize(rows) {
  let best = null;
  for (const row of rows) {
    const kind = deriveKind(row);
    if (best === null || RANK[kind] < RANK[best]) best = kind;
  }
  return { total: rows.length, kind: best || "no-signal" };
}

// "detecting" is a capability of the process, independent of whether video
// itself is flowing -- a camera can be fully live (fresh, stable frames) with
// no model loaded (no weights configured for the site, or the GPU had no
// room for one more when this process started). That is not the same fact
// as "stale" or "no-signal" and must not collapse into either: the video is
// fine, only the AI on top of it is off. See capture_manager.py's
// _ensure_started for why starting a camera never waits on GPU headroom.
export function deriveKind(cam) {
  if (cam.error) return "error";
  if (!cam.running) return "no-signal";
  if (cam.starting) return "starting";
  if (cam.live && cam.stable) return cam.detecting ? "live" : "live-no-detect";
  if (cam.live && !cam.stable) return "starting";
  return "stale";
}
