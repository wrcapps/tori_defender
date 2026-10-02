// Thin fetch wrapper. Every call sends the session cookie (same-origin, so
// `credentials: "same-origin"` is enough -- no cross-origin case exists once
// this is served from app.py itself) and normalizes the two failure shapes a
// caller actually needs to distinguish: "the request reached the server and
// it said no" (an ApiError, with the server's own message) vs "the request
// never got an answer" (a network/offline error) -- these need different UI
// (an inline validation message vs a full reconnecting/offline state).

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.error) || `request failed (${status})`);
    this.status = status;
    this.body = body;
  }
}

// Paths whose own 401 is a normal, expected outcome (a wrong password, or a
// logout confirming there's nothing to log out of) -- these must NOT trip
// the global "session died" event below, or a wrong-password attempt on the
// login screen itself would fire it.
const AUTH_ENDPOINTS = new Set(["/api/login", "/api/logout"]);

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new Error("network-error");
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    // A 401 on any *other* call means the session that was valid a moment
    // ago no longer is -- cleared cookies, an expired/destroyed session, or
    // (on this single-process app) a server restart, which wipes every
    // session by design (auth.py's own documented failure mode). Without
    // this, a page already open when that happens just shows broken/empty
    // data forever (every fetch quietly 401s) until someone thinks to
    // reload by hand. AuthContext listens for this and redirects to /login
    // immediately, refresh or not.
    if (res.status === 401 && !AUTH_ENDPOINTS.has(path)) {
      dispatchEvent(new Event("auth:expired"));
    }
    throw new ApiError(res.status, data);
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),

  whoami: () => request("GET", "/api/whoami"),
  login: (username, password) => request("POST", "/api/login", { username, password }),
  logout: () => request("POST", "/api/logout"),

  cameras: () => request("GET", "/api/cameras"),

  sightings: (params) => request("GET", `/api/sightings?${new URLSearchParams(params)}`),
  speciesSummary: (site) => request("GET", `/api/species?${new URLSearchParams({ site })}`),

  // ---- live notifications / per-camera detail ----
  recentDetections: () => request("GET", "/api/recent_detections"),
  alerts: () => request("GET", "/api/alerts"),
  acquisition: () => request("GET", "/api/acquisition"),
  storage: () => request("GET", "/api/storage"),
  setAcquisition: (enabled) => request("POST", "/api/acquisition", { enabled }),
  settings: () => request("GET", "/api/settings"),
  saveSettings: (changes) => request("POST", "/api/settings", changes),
  devices: (refresh) => request("GET", `/api/devices${refresh ? "?refresh=1" : ""}`),
  checkDataDir: (path) => request("GET", `/api/settings/check_dir?${new URLSearchParams({ path })}`),
  liveEvents: (since) => request("GET", `/api/live_events?${new URLSearchParams({ since })}`),
  riskPolicy: (site) => request("GET", `/api/risk_policy?${new URLSearchParams({ site })}`),
  cameraActivity: (site, camera) =>
    request("GET", `/api/camera_activity?${new URLSearchParams({ site, camera })}`),
  inbox: (site) => request("GET", `/api/inbox?${new URLSearchParams({ site })}`),
  review: (site, id, decision, reason, note) =>
    request("POST", "/api/review", { site, id, decision, reason, note }),
  undoReview: (site, day, windowName) =>
    request("POST", "/api/review/undo", { site, day, window: windowName }),
  track: (site, id) => request("GET", `/api/track?${new URLSearchParams({ site, id })}`),

  // ---- Review (M4b) ----
  folders: (site) => request("GET", `/api/folders?${new URLSearchParams({ site })}`),
  folder: (site, id) => request("GET", `/api/folder?${new URLSearchParams({ site, id })}`),
  boxes: (site, day, windowName, file) =>
    request("GET", `/api/boxes?${new URLSearchParams({ site, day, window: windowName, file })}`),
  verdicts: (site) => request("GET", `/api/verdicts?${new URLSearchParams({ site })}`),
  trackMeta: (site) => request("GET", `/api/track_meta?${new URLSearchParams({ site })}`),
  manualBoxes: (site) => request("GET", `/api/manual_boxes?${new URLSearchParams({ site })}`),

  setVerdict: (site, id, verdict) => request("POST", "/api/verdict", { site, id, verdict }),
  saveTrackMeta: (site, id, fields) => request("POST", "/api/track_meta", { site, id, ...fields }),
  boxEdit: (site, day, windowName, file, track, patch) =>
    request("POST", "/api/box_edit", { site, day, window: windowName, file, track, ...patch }),
  addManualBox: (site, day, windowName, file, bbox, fields) =>
    request("POST", "/api/manual_box", { site, day, window: windowName, file, bbox, ...fields }),
  updateManualBox: (site, id, bbox) => request("POST", "/api/manual_box/update", { site, id, bbox }),
  deleteManualBox: (site, id) => request("POST", "/api/manual_box/delete", { site, id }),
  startManualTrack: (site) => request("POST", "/api/manual_track", { site }),
  setFolderStatus: (site, id, reviewed) =>
    request("POST", "/api/folder_status", { site, id, reviewed }),

  // ---- Dataset (M4b) ----
  datasetSummary: () => request("GET", "/api/dataset/summary"),
  datasetItems: (params) => request("GET", `/api/dataset/items?${new URLSearchParams(params)}`),
  capturedSummary: (site) => request("GET", `/api/captured/summary?${new URLSearchParams({ site })}`),
  capturedItems: (params) => request("GET", `/api/captured/items?${new URLSearchParams(params)}`),
  capturedDelete: (site, group, ids) => request("POST", "/api/captured/delete", { site, group, ids }),
  capturedToReview: (site, id) => request("POST", "/api/captured/to_review", { site, group: "no_detections", ids: [id] }),
  datasetMark: (id, bad) => request("POST", "/api/dataset/mark", { id, bad }),
};
