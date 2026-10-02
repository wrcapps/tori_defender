// One shared connection for every live tile on screen.
//
// Browsers allow ~6 connections per host and an MJPEG <img> never finishes, so
// the 12-camera wall used to leave the API with no free connection at all (the
// whole UI froze; measured in tests/perf/browser_probe.py). Here every
// subscribed camera is multiplexed over a single fetch() to /stream/multi and
// the JPEGs are handed to whichever tile subscribed (see app.py _stream_multi
// for the wire format).
//
// Latest-frame semantics: each camera keeps at most one undecoded frame; if
// decoding falls behind, older frames are dropped rather than queued.

const subs = new Map();      // camera -> Set<{onFrame, onStatus}>
const state = { controller: null, timer: null, retries: 0, key: "" };
const stats = { frames: 0, dropped: 0, connects: 0 };

export const liveWallStats = () => ({ ...stats });
// Read by tests/perf/browser_probe.py (frames/s delivered, dropped, reconnects).
if (typeof window !== "undefined") window.__liveWallStats = liveWallStats;

function notify(cam, status) {
  for (const s of subs.get(cam) || []) s.onStatus?.(status);
}

function scheduleReconnect(delay = 120) {
  clearTimeout(state.timer);
  state.timer = setTimeout(connect, delay);
}

async function connect() {
  const names = [...subs.keys()].sort();
  const key = names.join(",");
  if (state.controller && key === state.key) return;
  state.controller?.abort();
  state.controller = null;
  state.key = key;
  if (!names.length) return;

  const controller = new AbortController();
  state.controller = controller;
  stats.connects += 1;
  names.forEach((n) => notify(n, "connecting"));
  const pending = new Array(names.length).fill(null);
  const busy = new Array(names.length).fill(false);

  async function decode(i) {
    if (busy[i]) return;
    busy[i] = true;
    while (pending[i]) {
      const blob = pending[i];
      pending[i] = null;
      try {
        const bmp = await createImageBitmap(blob);
        for (const s of subs.get(names[i]) || []) s.onFrame(bmp);
        bmp.close();
        stats.frames += 1;
      } catch { /* a torn frame: the next one replaces it */ }
    }
    busy[i] = false;
  }

  try {
    const res = await fetch(`/stream/multi?cams=${names.map(encodeURIComponent).join(",")}`,
                            { credentials: "same-origin", signal: controller.signal });
    if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
    state.retries = 0;
    names.forEach((n) => notify(n, "open"));
    const reader = res.body.getReader();
    let buf = new Uint8Array(0);
    for (;;) {
      const { value, done } = await reader.read();
      if (done) throw new Error("stream ended");
      const merged = new Uint8Array(buf.length + value.length);
      merged.set(buf); merged.set(value, buf.length);
      buf = merged;
      for (;;) {
        if (buf.length < 6) break;
        const dv = new DataView(buf.buffer, buf.byteOffset);
        const idx = dv.getUint16(0), len = dv.getUint32(2);
        if (buf.length < 6 + len) break;
        if (pending[idx]) stats.dropped += 1;
        pending[idx] = new Blob([buf.slice(6, 6 + len)], { type: "image/jpeg" });
        buf = buf.subarray(6 + len);
        decode(idx);
      }
    }
  } catch (err) {
    if (controller.signal.aborted) return;       // we replaced it on purpose
    state.controller = null;
    state.key = "";
    state.retries += 1;
    names.forEach((n) => notify(n, "reconnecting"));
    scheduleReconnect(Math.min(5000, 500 * 2 ** state.retries));
  }
}

// Returns an unsubscribe function.
export function subscribeLive(camera, onFrame, onStatus) {
  const entry = { onFrame, onStatus };
  if (!subs.has(camera)) subs.set(camera, new Set());
  subs.get(camera).add(entry);
  scheduleReconnect();
  return () => {
    const set = subs.get(camera);
    set?.delete(entry);
    if (set && set.size === 0) subs.delete(camera);
    scheduleReconnect(300);
  };
}
