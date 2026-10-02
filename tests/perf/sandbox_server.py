#!/usr/bin/env python3
"""Throw-away app.py instance for load tests: 12 fake cameras, no GPU, no real data.

Everything lives in a temp dir (--root); the real sites/ tree and the running
acquisition service are never touched. A feeder thread rewrites each camera's
latest.jpg at ~FPS so /stream/<cam>.mjpg behaves like a live camera.

    python tests/perf/sandbox_server.py --root /tmp/x --port 8791
"""
import argparse, os, shutil, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

ap = argparse.ArgumentParser()
ap.add_argument("--root", type=Path, required=True)
ap.add_argument("--port", type=int, default=8791)
ap.add_argument("--cameras", type=int, default=12)
ap.add_argument("--fps", type=float, default=5.0)
ap.add_argument("--seed-review", action="store_true", help="also create inbox/frames windows + detections to review")
ap.add_argument("--frame", type=Path, default=ROOT / "tests/perf/frame.jpg")
a = ap.parse_args()

root = a.root
(root / "sites").mkdir(parents=True, exist_ok=True)
(root / "logs").mkdir(exist_ok=True)
names = [f"cam{i:02d}" for i in range(a.cameras)]
(root / "config.yaml").write_text(
    "site: sandbox\ndefaults:\n  username: x\n  password: x\n"
    "  record_url: rtsp://{host}:554/x\ncameras:\n"
    + "".join(f"- name: {n}\n  host: 10.0.0.{i+1}\n" for i, n in enumerate(names)))
import auth
store_path = root / "users.yaml"
import yaml
store_path.write_text(yaml.safe_dump({"users": {"t": {"password": auth.hash_password("test"), "role": "operator"}}}))

frame = a.frame.read_bytes()
live = root / "sites/sandbox/live"
for n in names:
    (live / n).mkdir(parents=True, exist_ok=True)

def feed():
    while True:
        for n in names:
            p = live / n / "latest.jpg"
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(frame)
            tmp.replace(p)
        time.sleep(1 / a.fps)
threading.Thread(target=feed, daemon=True).start()

if a.seed_review:
    import json as _json
    day = time.strftime("%Y-%m-%d")
    site = root / "sites/sandbox"
    bucket = site / "dataset/detections/live"
    bucket.mkdir(parents=True, exist_ok=True)
    rows = []
    def mk(base, name, tracks, closed_days_ago=0.0, extra=None):
        d = site / base / day / name
        d.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (d / f"frame_{i}.jpg").write_bytes(frame)
        (d / "window.json").write_text(_json.dumps({"camera": "cam00", "frames": 3, "opened_at": None,
                                       "closed_at": time.time() - closed_days_ago * 86400, **(extra or {})}))
        for t in tracks:
            for i in range(3):
                rows.append({"day": day, "window": name, "file": f"frame_{i}.jpg", "track": t, "conf": 0.8,
                             "bbox": [300 + 40 * t + 10 * i, 200, 380 + 40 * t + 10 * i, 260], "camera": "cam00",
                             "carried": False, "crop": None})
    mk("inbox", "cam00-100000", [1, 2])            # two tracks: confirm one, reject the other
    mk("inbox", "cam00-100100", [1])               # rejected -> trash
    mk("inbox", "cam00-100200", [1], closed_days_ago=5)   # unreviewed and old -> expires
    mk("inbox", "cam00-100300", [1])               # stays pending
    mk("frames", "cam00-090000", [1], closed_days_ago=30)  # legacy, confirmed
    (bucket / "detections.jsonl").write_text("".join(_json.dumps(r) + "\n" for r in rows))

import capture_manager
capture_manager.CaptureManager._ensure_started = lambda self: None   # never spawn the GPU service

import app
sys.argv = ["app.py", "--config", str(root / "config.yaml"), "--sites-root", str(root / "sites"),
            "--users", str(store_path), "--capture-logs", str(root / "logs"),
            "--port", str(a.port), "--fps-cap", "10"]
raise SystemExit(app.main())
