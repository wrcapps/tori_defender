#!/usr/bin/env python3
"""Measure how responsive the UI stays while the Matrix page holds N live streams open.

Probes /api/whoami from inside the page every 250 ms (same connection pool the
app's own polling uses) and reports latency percentiles plus timeouts.

    python tests/perf/browser_probe.py --port 8791 --seconds 20 [--proto-label before]
"""
import argparse, json, statistics, time
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8791)
ap.add_argument("--seconds", type=int, default=20)
ap.add_argument("--path", default="/app/live/sandbox/matrix")
ap.add_argument("--label", default="")
ap.add_argument("--timeout-ms", type=int, default=5000)
a = ap.parse_args()
base = f"http://127.0.0.1:{a.port}"

PROBE = """async ([seconds, timeoutMs]) => {
  window.__long = []; try { new PerformanceObserver(l => l.getEntries().forEach(e => window.__long.push(e.duration))).observe({entryTypes:['longtask']}); } catch(e) {}
  const st0 = window.__liveWallStats ? window.__liveWallStats() : null; const w0 = performance.now();
  const lat = []; let timeouts = 0; const end = performance.now() + seconds*1000;
  while (performance.now() < end) {
    const t0 = performance.now();
    try {
      const c = new AbortController(); const to = setTimeout(() => c.abort(), timeoutMs);
      await fetch('/api/whoami', {signal: c.signal, cache: 'no-store'}); clearTimeout(to);
      lat.push(performance.now() - t0);
    } catch (e) { timeouts++; }
    await new Promise(r => setTimeout(r, 250));
  }
  const imgs = [...document.querySelectorAll('img, canvas')];
  const st1 = window.__liveWallStats ? window.__liveWallStats() : null; const secs = (performance.now()-w0)/1000;
  const stats = st1 && {fps_total: +((st1.frames-st0.frames)/secs).toFixed(1), dropped: st1.dropped-st0.dropped, connects: st1.connects, longtasks: window.__long.length, longtask_ms: Math.round(window.__long.reduce((a,b)=>a+b,0))};
  return {stats, lat, timeouts, imgs: imgs.length, loaded: imgs.filter(i => (i.naturalWidth || (i.tagName==='CANVAS' && i.width > 300 ? 1 : 0)) > 0).length};
}"""

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    page = ctx.new_page()
    page.goto(f"{base}/login")
    page.request.post(f"{base}/api/login", data=json.dumps({"username": "t", "password": "test"}),
                      headers={"Content-Type": "application/json"})
    t0 = time.time()
    page.goto(base + a.path, wait_until="domcontentloaded")
    time.sleep(2)
    r = page.evaluate(PROBE, [a.seconds, a.timeout_ms])
    lat = sorted(r["lat"])
    pct = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else float("nan")
    print(json.dumps({"label": a.label, "path": a.path, "tiles": r["imgs"], "tiles_with_frame": r["loaded"],
                      "stats": r["stats"], "probes_ok": len(lat), "probes_timeout": r["timeouts"],
                      "p50_ms": round(pct(.5), 1), "p95_ms": round(pct(.95), 1),
                      "max_ms": round(lat[-1], 1) if lat else None}))
    page.screenshot(path=f"/tmp/claude-1000/probe-{a.label or 'x'}.png")
    b.close()
