#!/usr/bin/env python3
"""Run live_capture.py for every camera in the config, one process each, and
keep them running.

WHY ONE PROCESS PER CAMERA:
    A camera that loses its stream, wedges in a decode, or dies outright takes
    only itself down, and comes back on its own without disturbing the others.

WHY IT REFUSES TO START AN OVERSUBSCRIBED FLEET:
    Tiled 4K inference measures ~154 ms/frame at FP16 -- about 6.5 frames per
    second for the whole GPU, shared by every camera. Past that ceiling nothing
    crashes: each process simply falls behind its own sampling interval and
    writes thinner windows than it was configured to, which looks like normal
    output and is only noticed much later, as choppy playback. So the arithmetic
    is done up front, against --gpu-fps, and an impossible configuration is an
    error rather than a silent degradation.

    The demand a camera places on the GPU is:
        idle       scan_fps
        in-window  save_fps / infer_every_n
    and the worst case is every camera in a window at once.

WHAT IT DOES NOT SOLVE:
    Bandwidth. Each process pulls its camera's main 4K stream continuously
    (~6 Mbit/s), and 12 of those over one VPN link is more than the link
    carries, whatever the GPU is doing. Use --cameras to run a subset.

    ./live_capture_all.py --config config.yaml
    ./live_capture_all.py --config config.yaml --cameras mast2-a,mast2-b
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import yaml

from camera_config import build_cameras, redact_text, select_cameras
from capacity import MEASURED_GPU_FPS, budget_warning, total_demand

HERE = Path(__file__).resolve().parent

# Each process loads its own copy of the model and its own CUDA context before
# it infers anything, so starting twelve at once spikes VRAM far above the
# steady state. They are started one at a time, this many seconds apart.
START_STAGGER_SECONDS = 6.0
RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX = 120.0


def check_capacity(cameras: list, gpu_fps: float, force: bool) -> None:
    idle, busy = total_demand(cameras)
    print(f"GPU budget: {gpu_fps:g} tiled inferences/second available")
    print(f"  idle (all cameras scanning)   {idle:6.1f} /s")
    print(f"  worst case (all in a window)  {busy:6.1f} /s")
    warning = budget_warning(cameras, gpu_fps)
    if warning is None:
        print("  fits.")
        return
    message = ("\n" + warning + "\nOptions: fewer cameras (--cameras), a lower "
              "live.save_fps, a higher live.infer_every_n, or --force if you "
              "have measured otherwise.")
    if not force:
        raise SystemExit(message)
    print(message + "\n  --force given: starting anyway.")


class Child:
    """One camera's capture process, restarted if it dies."""

    def __init__(self, camera, argv: list[str], log_path: Path):
        self.camera = camera
        self.argv = argv
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self.backoff = RESTART_BACKOFF_SECONDS
        self.restarts = 0
        self.started_at = 0.0
        self.restart_at = 0.0

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Output is piped and filtered rather than redirected straight to the
        # file: ffmpeg prints the stream URL -- password and all -- into its own
        # error messages when a camera is unreachable, which would otherwise put
        # the camera's password in a log file, permanently, on the first outage.
        self.process = subprocess.Popen(self.argv, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        bufsize=1, errors="replace")
        threading.Thread(target=self._pump_log, args=(self.process,), daemon=True).start()
        self.started_at = time.monotonic()
        self.restart_at = 0.0
        print(f"[{self.camera.name}] started (pid {self.process.pid}) -> {self.log_path}",
              flush=True)

    def _pump_log(self, process: subprocess.Popen) -> None:
        with self.log_path.open("a", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(redact_text(line))
                log.flush()

    def poll(self) -> None:
        if self.process is None:
            return
        if self.restart_at:
            # Waited out here rather than slept through, so a Ctrl-C during a
            # backoff is noticed straight away instead of minutes later.
            if time.monotonic() >= self.restart_at:
                self.start()
            return
        if self.process.poll() is None:
            return
        code = self.process.returncode
        ran_for = time.monotonic() - self.started_at
        # A process that stayed up is a transient failure; one that dies
        # immediately is usually a bad config or missing weights, and retrying
        # it in a tight loop would only fill the log.
        self.backoff = RESTART_BACKOFF_SECONDS if ran_for > 60 else min(
            self.backoff * 2, RESTART_BACKOFF_MAX)
        self.restarts += 1
        self.restart_at = time.monotonic() + self.backoff
        print(f"[{self.camera.name}] exited with {code} after {ran_for:.0f}s; "
              f"restarting in {self.backoff:.0f}s (see {self.log_path})", flush=True)

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--cameras", default=None,
                    help="comma-separated names to run (default: every camera in the config)")
    ap.add_argument("--weights", default=None, help="passed through to each capture process")
    ap.add_argument("--bucket", default="live", help="passed through to each capture process")
    ap.add_argument("--sites-root", type=Path, default=None,
                    help="passed through to each capture process")
    ap.add_argument("--logs", type=Path, default=HERE / "logs",
                    help=f"where per-camera logs go (default: {HERE / 'logs'})")
    ap.add_argument("--gpu-fps", type=float, default=MEASURED_GPU_FPS,
                    help=f"tiled inferences per second this GPU can sustain "
                         f"(default {MEASURED_GPU_FPS:g}, measured at FP16 on 4K frames)")
    ap.add_argument("--force", action="store_true",
                    help="start even if the cameras ask for more than the GPU can do")
    ap.add_argument("--fp32", action="store_true", help="passed through to each capture process")
    ap.add_argument("--no-preview", action="store_true",
                    help="passed through to each capture process")
    ap.add_argument("--always-record", action="store_true",
                    help="passed through to each capture process -- capture on every "
                         "detection regardless of the app's per-camera record toggle")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    site = cfg.get("site", "site")
    cameras = select_cameras(build_cameras(cfg), args.cameras)
    print(f"{len(cameras)} camera(s): {', '.join(c.name for c in cameras)}\n")
    check_capacity(cameras, args.gpu_fps, args.force)

    shared = ["--config", str(args.config), "--bucket", args.bucket]
    if args.weights:
        shared += ["--weights", args.weights]
    if args.sites_root:
        shared += ["--sites-root", str(args.sites_root)]
    if args.fp32:
        shared.append("--fp32")
    if args.no_preview:
        shared.append("--no-preview")
    if args.always_record:
        shared.append("--always-record")

    today = date.today().isoformat()
    children = [
        Child(camera,
              [sys.executable, str(HERE / "live_capture.py"), *shared, "--camera", camera.name],
              args.logs / f"{site}-{camera.safe_name}-live-{today}.out")
        for camera in cameras
    ]

    stopping = False

    def handle_signal(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print()
    for i, child in enumerate(children):
        if stopping:
            break
        child.start()
        if i < len(children) - 1:
            time.sleep(START_STAGGER_SECONDS)

    print(f"\nall started. logs in {args.logs}/ -- watch one with:"
          f"\n  tail -f {children[0].log_path}"
          f"\nCtrl-C to stop every camera.\n")
    try:
        while not stopping:
            for child in children:
                if stopping:
                    break
                child.poll()
            time.sleep(2.0)
    finally:
        print("\nstopping every camera...", flush=True)
        for child in children:
            child.stop()
        for child in children:
            if child.restarts:
                print(f"  {child.camera.name}: {child.restarts} restart(s) during this run")
        print("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
