#!/usr/bin/env python3
"""Owns the ONE acquisition process (acquisition_service.py) and tells it what to do.

WHAT CHANGED FROM "A PROCESS PER CAMERA"
    This used to start a live_capture.py per camera on demand: a model and a CUDA context per
    camera (~1.4 GB of VRAM each, so twelve cameras were ~17 GB and never fit a 12 GB card),
    all fighting over one GPU with no coordination. There is now a single service that loads
    the model once and runs the cameras in synchronised rounds (see acquisition_service.py).
    This class keeps the interface the rest of app.py already uses -- acquire/release for
    viewers, note_recording, status(camera) -- and adds the one new thing, `set_enabled`.

THREE INDEPENDENT REASONS A CAMERA IS PROCESSED (any one is enough)
    - acquisition is ENABLED (the header button): every camera, with detection and
      notifications -- but nothing is written to disk unless that camera is also recording;
    - the camera's RECORDING flag file exists (the per-camera Record button, unchanged): it is
      captured and saved even if acquisition is off;
    - someone is VIEWING it (an open MJPEG stream): it is processed so the view has boxes.
    Enabling acquisition never touches a recording flag, and recording never needs acquisition.

HOW THE APP AND THE SERVICE TALK
    Files in <logs>/acquisition/:  control.json (app -> service: enabled, viewed cameras) and
    status.json (service -> app, ~1/s). A control write is atomic and the service polls its
    mtime, so there is no socket, no port, and nothing to keep in sync after a crash.
    `enabled` is persisted in control.json, so it survives an app restart.

THE SERVICE RUNS ONLY WHILE SOMETHING WANTS IT
    No enabled flag, no viewers and no recording flag -> the process is stopped and its VRAM
    is returned. It is restarted with backoff if it dies while wanted.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent

RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX = 120.0
STARTING_SECONDS = 25.0
# Above this much of the GPU's time spent just scanning, warn: the rounds will start to run late.
GPU_DUTY_WARN = 0.8


class CaptureManager:
    def __init__(self, logs_dir: Path, gpu_fps: float, weights_by_site: dict[str, str | None],
                 idle_stop_s: float = 600.0):
        self.logs_dir = logs_dir
        # The Acquisition switch is remembered across restarts, so without this a service started "for a
        # while" kept running for days with nobody looking. 0 disables the auto-stop.
        self.idle_stop_s = idle_stop_s
        self.last_seen = time.monotonic()
        self.auto_stopped_at: float | None = None
        self.gpu_fps = gpu_fps
        self.weights_by_site = weights_by_site
        # What Settings chose, if anything: {weights, type, variant} wins over config.yaml and
        # --weights (a person picked it); device is "auto" | "cpu" | "cuda" (acquisition_service --device).
        self.model_choice: dict | None = None
        self.device = "auto"
        self.dir = logs_dir / "acquisition"
        self.control_path = self.dir / "control.json"
        self.status_path = self.dir / "status.json"
        self.lock = threading.RLock()
        self.sites: dict[str, object] = {}
        self.cameras: dict[str, tuple[object, object]] = {}     # camera name -> (Camera, Site)
        self.viewers: dict[str, int] = {}
        self.enabled = False
        self.process: subprocess.Popen | None = None
        self.started_at = 0.0
        self.last_exit: tuple[int, str] | None = None
        self.backoff = RESTART_BACKOFF_SECONDS
        self.restart_at: float | None = None
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._written: dict | None = None

    # ------------------------------------------------------------ registration
    def register(self, site, camera) -> None:
        self.sites[site.name] = site
        self.cameras[camera.name] = (camera, site)

    # ---------------------------------------------------------------- commands
    def _argv(self) -> list[str]:
        sites = list(self.sites.values())
        # Frozen by PyInstaller there is no interpreter to hand a script to: the launcher
        # exe itself runs the service when asked with --service (desktop/launcher.py).
        if getattr(sys, "frozen", False):
            argv = [sys.executable, "--service"]
        else:
            argv = [sys.executable, str(HERE / "acquisition_service.py")]
        for site in sites:
            argv += ["--config", str(site.config_path)]
        argv += ["--stop-on-stdin-eof",
                 "--bucket", sites[0].model_bucket,
                 "--control", str(self.control_path), "--status", str(self.status_path)]
        if sites[0].sites_root:
            argv += ["--sites-root", str(sites[0].sites_root)]
        weights = next((w for w in self.weights_by_site.values() if w), None)
        if self.model_choice:
            argv += ["--weights", self.model_choice["weights"], "--model-type", self.model_choice["type"]]
            if self.model_choice.get("variant"):
                argv += ["--rfdetr-variant", self.model_choice["variant"]]
        elif weights:
            argv += ["--weights", weights]
        argv += ["--device", self.device]
        return argv

    def _pattern(self) -> str:
        return f"(acquisition_service.py|--service).*--control {self.control_path}"

    def reap_orphans(self) -> None:
        """A service left running by an app that died without cleaning up would hold the GPU
        and every camera connection with nothing able to stop it; matched on this app's own
        control path, so it can never touch another instance's."""
        try:
            found = subprocess.run(["pgrep", "-f", self._pattern()], capture_output=True,
                                   text=True, timeout=5)
            pids = [int(p) for p in found.stdout.split() if int(p) != os.getpid()]
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return
        for pid in pids:
            print(f"found an orphaned acquisition service (pid {pid}); stopping it", flush=True)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if pids:
            time.sleep(1.5)

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.reap_orphans()
        # Acquisition starts only when a person switches it on. A previous run's `enabled` in
        # control.json is deliberately NOT restored: a restart (or a crash-loop supervisor) must not
        # put the cameras and the GPU back to work on its own.
        self.enabled = False
        with self.lock:
            self._sync()
        self._reaper.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            self._stop_process()

    # ------------------------------------------------------------- desired state
    def _recording_cameras(self) -> list[str]:
        return [n for n, (_, site) in self.cameras.items() if site.is_recording(n)]

    def _wanted(self) -> bool:
        return (self.enabled or any(v > 0 for v in self.viewers.values())
                or bool(self._recording_cameras()))

    def _write_control(self) -> None:
        payload = {"enabled": self.enabled,
                   "viewed": sorted(n for n, v in self.viewers.items() if v > 0)}
        if payload == self._written:
            return
        self._written = payload
        tmp = self.control_path.with_name(f".{self.control_path.name}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.control_path)

    def _sync(self, from_reaper: bool = False) -> str | None:
        """Make the process match what is wanted. Caller holds the lock.

        The reaper's periodic call must not start a service that is sitting out a crash
        backoff; a person's click (enable, open a stream, record) may -- it asked for it.
        """
        self._write_control()
        if self._wanted():
            if from_reaper and self.restart_at is not None:
                return None
            return self._ensure_started()
        self._stop_process()
        self.restart_at = None
        return None

    def touch(self) -> None:
        """A signed-in page made a request: somebody is still here."""
        self.last_seen = time.monotonic()

    def _stop_if_abandoned(self, now: float) -> bool:
        """Switch Acquisition off when no page has been open for idle_stop_s. Caller holds the lock.
        Cameras with their own Record flag and open streams are separate, explicit reasons to run."""
        if not (self.enabled and self.idle_stop_s > 0 and now - self.last_seen > self.idle_stop_s):
            return False
        self.enabled = False
        self.auto_stopped_at = time.time()
        print(f"acquisition switched off: no page open for {self.idle_stop_s / 60:g} min", flush=True)
        self._sync()
        return True

    def set_enabled(self, enabled: bool) -> str | None:
        with self.lock:
            self.last_seen = time.monotonic()
            if enabled:
                self.auto_stopped_at = None
            self.enabled = enabled
            return self._sync()

    def reconfigure(self, model_choice: dict | None, device: str) -> str | None:
        """Switch model and/or device. The service loads both once at start, so a running one is
        stopped (it closes its open windows first) and started again with the new flags -- only if
        something still wants it; otherwise the next start picks them up. Returns an error or None."""
        with self.lock:
            if model_choice == self.model_choice and device == self.device:
                return None
            self.model_choice, self.device = model_choice, device
            was_running = self.running()
            self._stop_process()
            self.restart_at = None
            self.backoff = RESTART_BACKOFF_SECONDS
            self.last_exit = None
            return self._sync() if was_running else None

    def acquire(self, name: str) -> str | None:
        if name not in self.cameras:
            return "unknown camera"
        with self.lock:
            self.viewers[name] = self.viewers.get(name, 0) + 1
            return self._sync()

    def release(self, name: str) -> None:
        with self.lock:
            self.viewers[name] = max(0, self.viewers.get(name, 0) - 1)
            self._sync()

    def note_recording(self, name: str, enabled: bool) -> str | None:
        """The flag file is already written by the caller; start or stop the service to match."""
        if name not in self.cameras:
            return "unknown camera"
        with self.lock:
            return self._sync()

    # --------------------------------------------------------------- the process
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _ensure_started(self) -> str | None:
        if self.running():
            return None
        if not self.sites:
            return "no site registered"
        self.dir.mkdir(parents=True, exist_ok=True)
        log_path = self.dir / f"service-{date.today().isoformat()}.out"
        try:
            self.status_path.unlink()
        except OSError:
            pass
        # stdin is the stop channel: closing it asks the service to finish cleanly (and its
        # death closes it for the service, so an orphan can never outlive this app) --
        # there is no SIGTERM that closes a window gracefully on Windows.
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.process = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                                        errors="replace", **extra)
        self.started_at = time.monotonic()
        self.last_exit = None
        self.restart_at = None
        threading.Thread(target=self._pump_log, args=(self.process, log_path), daemon=True).start()
        print(f"acquisition service started (pid {self.process.pid}) -> {log_path}", flush=True)
        return None

    @staticmethod
    def _pump_log(process, log_path: Path) -> None:
        from camera_config import redact_text
        with log_path.open("a", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(redact_text(line))
                log.flush()

    def _stop_process(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.close()          # --stop-on-stdin-eof: the service closes any open window
            process.wait(timeout=15)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    def _reap_loop(self) -> None:
        while not self._stop.wait(2.0):
            with self.lock:
                now = time.monotonic()
                if self.process is not None and self.process.poll() is not None:
                    code = self.process.returncode
                    ran = now - self.started_at
                    self.process = None
                    self.last_exit = (code, f"acquisition service exited with {code} after {ran:.0f}s")
                    if self._wanted():
                        wait = self.backoff if ran < 60 else RESTART_BACKOFF_SECONDS
                        self.backoff = (min(self.backoff * 2, RESTART_BACKOFF_MAX)
                                        if ran < 60 else RESTART_BACKOFF_SECONDS)
                        self.restart_at = now + wait
                        print(f"{self.last_exit[1]}; restarting in {wait:.0f}s", flush=True)
                elif self.restart_at is not None and now >= self.restart_at:
                    self.restart_at = None
                    if self._wanted():
                        self._ensure_started()
                self._stop_if_abandoned(now)
                # recording flags are files: a flag set or cleared outside this app (touch, rm)
                # must still start or stop the service
                self._sync(from_reaper=True)

    # -------------------------------------------------------------------- status
    def _read_status(self) -> dict | None:
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if time.time() - data.get("updated", 0) < 10 else None   # stale = not running

    def acquisition_status(self) -> dict:
        """Everything the header button and its popover show."""
        with self.lock:
            running = self.running()
            st = self._read_status() if running else None
            young = time.monotonic() - self.started_at < STARTING_SECONDS
            starting = running and (st is None or (young and not st.get("cameras_active")))
            out = {"enabled": self.enabled, "running": running, "starting": bool(starting),
                   "error": self.last_exit[1] if self.last_exit and not running else None,
                   "idle_stop_minutes": self.idle_stop_s / 60,
                   "auto_stopped_at": self.auto_stopped_at,
                   "cameras_configured": len(self.cameras),
                   "recording": self._recording_cameras()}
        if st:
            out.update({k: st.get(k) for k in (
                "cameras_active", "detecting", "detection_note", "model", "rounds", "round_ms",
                "infer_ms", "capture_skew_ms", "frame_age_ms", "frames_per_round",
                "cameras_per_round", "max_tiles_per_forward", "vram", "est_mbit_s", "oom_events",
                "sources", "segment_hub")})
            out["warning"] = self._warning(st)
        return out

    def _warning(self, st: dict) -> str | None:
        active = st.get("cameras_active") or 0
        per_frame = (st.get("infer_ms") or {}).get("p50")
        frames = st.get("frames_per_round") or 0
        if not (active and per_frame and frames):
            return None
        scan = next(iter(self.cameras.values()))[0].live["scan_fps"]
        duty = active * float(scan) * (per_frame / frames) / 1000.0
        if duty > GPU_DUTY_WARN:
            return (f"{active} cameras scanning at {float(scan):g}/s need {duty:.0%} of the GPU "
                    f"for detection alone; rounds will start to run late")
        return None

    def status(self, name: str) -> dict:
        """The per-camera fields /api/cameras reports (same shape as before)."""
        if name not in self.cameras:
            return {"running": False, "starting": False, "error": "unknown camera",
                    "detecting": False, "detection_note": None}
        with self.lock:
            running = self.running()
            st = self._read_status() if running else None
            error = self.last_exit[1] if self.last_exit and not running else None
        cam = (st or {}).get("cameras", {}).get(name)
        if cam is None:
            return {"running": False, "starting": running and st is None, "error": error,
                    "warning": None, "detecting": False, "detection_note": None}
        cam_error = cam.get("error") if cam["state"] not in ("live", "segments") else None
        source = cam.get("source")
        if source == "segments":
            lag = cam.get("lag_s")
            note = ("live is not holding: showing the NVR's recording"
                    + (f", {lag:.0f}s behind" if lag is not None else ""))
        elif source == "none":
            note = "no data (black): live is down and no NVR segment has arrived"
        else:
            note = None
        return {"running": True, "starting": cam["state"] == "connecting",
                "error": error or cam_error, "source": source, "lag_s": cam.get("lag_s"),
                "warning": note or self._warning(st),
                "detecting": bool(st.get("detecting")),
                "detection_note": st.get("detection_note")}
