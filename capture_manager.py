#!/usr/bin/env python3
"""Starting a camera's capture process the moment someone actually needs it,
and stopping it again once nobody does.

WHY ON DEMAND, NOT "RUN EVERYTHING THE CONFIG LISTS":
    A site's config can list far more cameras than one GPU or one link can run
    at once (Babadag's 12, against a ~6.5 tiled-inference/s budget). Starting
    all of them because they're merely configured wastes exactly the resources
    the rest of this app is careful about. Starting one the moment a person
    picks it in Live -- and only that one -- means the fleet in use is always
    the fleet someone is actually looking at or has asked to record.

WHAT COUNTS AS "NEEDED":
    A camera's process runs for as long as EITHER is true:
      - at least one MJPEG viewer is connected to its stream (tracked by
        _stream()'s own connect/disconnect, not by a separate button -- the
        open HTTP connection already IS the signal), or
      - recording is switched on for it.
    Losing the last viewer, or recording being switched off with nobody
    watching, stops the process immediately -- no grace period. An earlier
    version of this waited ~20s before stopping, specifically so a page
    reload or a flaky reconnect wouldn't pay a freshly loaded model's startup
    cost again for nothing. That grace period turned out to have a real cost
    of its own: several stale-but-still-running viewers left open (e.g. from
    Matrix's "watch all") hold GPU/VRAM the budget check (capacity.py) counts
    against every *new* camera someone tries to open next, which read as the
    whole app stalling. Stopping instantly was chosen over that -- a real
    page reload now does pay the reload cost again, a tradeoff accepted on
    purpose rather than discovered as a regression.

WHY A SOFT CAPACITY WARNING HERE, NOT A HARD REFUSAL:
    live_capture_all.py refuses outright, because it is handed a whole fleet in
    one shot and a silent partial start would be confusing. Here a person just
    clicked one specific camera; refusing that click with no way through is
    worse than starting it anyway and saying, honestly, that it and its
    neighbours may now be oversubscribed.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

from capacity import budget_warning, gpu_free_mib, total_demand, vram_refusal
from metrics_log import log_metric

HERE = Path(__file__).resolve().parent

RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX = 120.0
# How often the reaper's own periodic capacity snapshot is logged -- ticking
# every 2s (the reaper's own loop interval) would be far more resolution than
# this budget number needs, per PERFORMANCE.md's structured-output requirement.
CAPACITY_SNAPSHOT_SECONDS = 60.0


class ManagedCamera:
    """One camera's on-demand process, and why it is or isn't running."""

    def __init__(self, name: str, camera, argv: list[str], log_path: Path,
                 camera_live_dir: Path):
        self.name = name
        self.camera = camera
        self.argv = argv
        self.log_path = log_path
        self.camera_live_dir = camera_live_dir
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.viewers = 0
        self.recording = False
        self.backoff = RESTART_BACKOFF_SECONDS
        self.started_at = 0.0
        self.last_exit: tuple[int, str] | None = None   # (code, reason) for the UI
        self.budget_warning: str | None = None
        self.restart_at: float | None = None   # monotonic time a pending restart fires
        # Detection is a capability of a *running* process, not a precondition
        # for starting one -- see _ensure_started. weights/no_weights_reason
        # describe the site's configuration (fixed); detecting/detection_note
        # describe what the *current* process actually loaded (can differ
        # run to run, e.g. VRAM was tight at the moment this one started).
        self.weights: str | None = None
        self.no_weights_reason: str | None = None
        self.detecting = False
        self.detection_note: str | None = None

    def wanted(self) -> bool:
        return self.viewers > 0 or self.recording

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class CaptureManager:
    """Every camera this app knows how to start, across every configured site.

    One instance, shared by every request handler -- a lock around each
    camera's own state is what keeps a viewer connecting on one thread and a
    recording toggle arriving on another from racing each other into starting
    the same process twice or stopping one the other still needs.
    """

    def __init__(self, logs_dir: Path, gpu_fps: float, weights_by_site: dict[str, str | None]):
        self.logs_dir = logs_dir
        self.gpu_fps = gpu_fps
        self.weights_by_site = weights_by_site
        self.cameras: dict[str, ManagedCamera] = {}
        self._reaper_stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        # Process-global, not per-camera or per-site: one GPU, one shared
        # budget (capacity.py's own docstring), so this belongs next to the
        # server's own log rather than under any one site's dataset/ tree.
        self._capacity_log_path = self.logs_dir / "capacity.jsonl"
        self._next_capacity_snapshot = 0.0

    def register(self, site, camera) -> None:
        """Called once at startup per camera -- builds the command line but
        starts nothing yet.

        --weights is deliberately NOT baked in here: whether this camera's
        process gets one is decided fresh at every start, in _ensure_started,
        because free VRAM changes as other cameras start and stop. A camera
        this manager registered at startup with plenty of headroom must not
        be stuck starting without detection forever just because some other
        camera's process happened to load its model first.
        """
        weights = self.weights_by_site.get(site.name)
        argv = [sys.executable, str(HERE / "live_capture.py"),
                "--config", str(site.config_path), "--camera", camera.name,
                "--bucket", site.model_bucket]
        if site.sites_root:
            argv += ["--sites-root", str(site.sites_root)]
        log_path = self.logs_dir / f"{site.name}-{camera.safe_name}-live-{date.today().isoformat()}.out"
        managed = ManagedCamera(camera.name, camera, argv, log_path, site.live_root / camera.name)
        managed.weights = weights
        if not weights:
            managed.no_weights_reason = (
                f"no weights configured for site {site.name!r} -- pass --weights to "
                f"app.py or set model.weights in {site.config_path} to enable detection "
                f"on this camera. The live video itself still works.")
        self.cameras[camera.name] = managed

    def reap_orphans(self) -> None:
        """Kill any live_capture.py already running for a camera this manager
        is about to own.

        This app is meant to be the only thing that starts these processes, so
        the only way one can already be running at startup is a previous
        instance of this app that did not shut down cleanly (SIGKILL, a crash,
        the machine coming back after a power loss) -- orphaned, still holding
        an RTSP connection and GPU memory open, with nothing left able to ask
        it to stop. Matched on the exact --config/--camera pair, so this can
        never touch a process for a camera some other tool started.
        """
        for managed in self.cameras.values():
            try:
                config_arg = managed.argv[managed.argv.index("--config") + 1]
                pattern = f"live_capture.py.*--config {config_arg}.*--camera {managed.name}(\\s|$)"
                found = subprocess.run(["pgrep", "-f", pattern],
                                       capture_output=True, text=True, timeout=5)
                pids = [int(p) for p in found.stdout.split()]
            except (ValueError, OSError, subprocess.TimeoutExpired):
                continue
            for pid in pids:
                print(f"[{managed.name}] found an orphaned process from a previous "
                      f"run (pid {pid}); stopping it", flush=True)
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            # The orphan's own latest.jpg/status.json are now stale, and would
            # otherwise sit there describing a connection that no longer
            # exists until the next process overwrites them -- briefly telling
            # a viewer "stable" or "live" from files a dead process left
            # behind, right as a fresh one is starting from zero.
            if pids:
                for name in ("latest.jpg", "status.json"):
                    (managed.camera_live_dir / name).unlink(missing_ok=True)
            if pids:
                time.sleep(1.0)
                for pid in pids:
                    try:
                        os.kill(pid, 0)   # still alive? (raises if not -- see below)
                    except ProcessLookupError:
                        continue          # already gone: SIGTERM was enough
                    # Still around a second after SIGTERM: force it. Checking
                    # first, rather than sending SIGKILL unconditionally,
                    # avoids the (rare but real) risk of the PID having been
                    # reused by an unrelated process in that one second.
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def start(self) -> None:
        self.reap_orphans()
        self._reaper.start()

    def shutdown(self) -> None:
        self._reaper_stop.set()
        for managed in self.cameras.values():
            self._stop_process(managed)

    # ------------------------------------------------------------ viewers
    def acquire(self, name: str) -> str | None:
        """A viewer connected. Starts the process if this is the first one.
        Returns an error string if it could not be started, else None."""
        managed = self.cameras.get(name)
        if managed is None:
            return "unknown camera"
        with managed.lock:
            managed.viewers += 1
            return self._ensure_started(managed)

    def release(self, name: str) -> None:
        """A viewer disconnected. Stops the process right away if nobody
        else needs it -- see the module docstring for why this is instant,
        not grace-period-delayed, as of this project's own explicit request."""
        managed = self.cameras.get(name)
        if managed is None:
            return
        with managed.lock:
            managed.viewers = max(0, managed.viewers - 1)
            should_stop = not managed.wanted()
        if should_stop:
            self._stop_process(managed)

    # ----------------------------------------------------------- recording
    def note_recording(self, name: str, enabled: bool) -> str | None:
        """The site's flag file is already written by the caller; this only
        starts or stops the process to match."""
        managed = self.cameras.get(name)
        if managed is None:
            return "unknown camera"
        with managed.lock:
            managed.recording = enabled
            if enabled:
                return self._ensure_started(managed)
            should_stop = not managed.wanted()
        if should_stop:
            self._stop_process(managed)
        return None

    # ------------------------------------------------------------- status
    def status(self, name: str) -> dict:
        managed = self.cameras.get(name)
        if managed is None:
            return {"running": False, "starting": False, "error": "unknown camera",
                    "detecting": False, "detection_note": None}
        with managed.lock:
            running = managed.running()
            starting = running and (time.monotonic() - managed.started_at) < 20.0
            # A dead process (last_exit) is a real failure -- the video itself
            # is down, not just detection on top of it. Missing/refused
            # detection while the video runs fine is a separate, softer fact,
            # reported as detection_note, never folded into `error`: a client
            # or an operator scanning for broken cameras must not have to
            # tell "this feed is down" and "this feed has no detector right
            # now" apart by reading a sentence.
            error = managed.last_exit[1] if managed.last_exit else None
            return {"running": running, "starting": starting, "error": error,
                    "warning": managed.budget_warning if running else None,
                    "detecting": managed.detecting if running else False,
                    "detection_note": managed.detection_note if running else None}

    # -------------------------------------------------------- process control
    def _ensure_started(self, managed: ManagedCamera) -> str | None:
        """Caller holds managed.lock.

        Starting the process is never refused: video (decode + live preview)
        needs no GPU model at all, so there is no resource for "not enough
        of it" to mean here -- see WHY VIDEO IS ALWAYS AVAILABLE in
        live_capture.py. Detection is a separate, optional capability of the
        process this call is about to start, decided fresh every time: if
        this site has no weights configured, or the GPU genuinely does not
        have room for one more model right now, the process starts anyway,
        without --weights, and live_capture.py runs in video-only mode --
        decoding and writing the live preview, inferring nothing, saving
        nothing (there is no detection to trigger a window). The camera
        stays visible and watchable either way; only detection is gated.
        """
        if managed.running():
            return None

        detecting = bool(managed.weights)
        free_mib = gpu_free_mib()
        if detecting:
            refusal = vram_refusal()
            if refusal:
                detecting = False
                managed.detection_note = refusal
            else:
                managed.detection_note = None
        else:
            managed.detection_note = managed.no_weights_reason

        argv = list(managed.argv)
        if detecting:
            argv += ["--weights", managed.weights]

        # The FPS budget is about inference throughput -- a video-only
        # process asks the GPU for nothing, so it has no place in this sum;
        # counting it would make every other camera look more oversubscribed
        # than the GPU actually is.
        detecting_cameras = [m.camera for m in self.cameras.values()
                             if m.name != managed.name and m.running() and m.detecting]
        if detecting:
            detecting_cameras.append(managed.camera)
        idle_demand, peak_demand = total_demand(detecting_cameras) if detecting_cameras else (0.0, 0.0)
        warning = budget_warning(detecting_cameras, self.gpu_fps) if detecting_cameras else None
        if warning:
            print(f"[{managed.name}] starting anyway, but: {warning}", file=sys.stderr, flush=True)
        managed.budget_warning = warning
        # Phase 0 instrumentation: this is the one moment `capacity.py`'s
        # math and the VRAM-refusal reason were previously only ever a
        # console print -- surface both as structured data too
        # (PERFORMANCE.md §4: "already computed, not yet surfaced to the UI").
        log_metric(self._capacity_log_path, "capacity_check",
                  camera=managed.name, detecting=detecting,
                  idle_demand=round(idle_demand, 2), peak_demand=round(peak_demand, 2),
                  gpu_fps=self.gpu_fps, warning=warning,
                  vram_free_mib=free_mib, vram_refusal=managed.detection_note if not detecting else None)

        managed.log_path.parent.mkdir(parents=True, exist_ok=True)
        managed.process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True,
                                          bufsize=1, errors="replace")
        managed.detecting = detecting
        managed.started_at = time.monotonic()
        managed.backoff = RESTART_BACKOFF_SECONDS
        managed.last_exit = None
        threading.Thread(target=self._pump_log, args=(managed,), daemon=True).start()
        print(f"[{managed.name}] started (pid {managed.process.pid}, "
              f"{'detecting' if detecting else 'video only: ' + (managed.detection_note or '')})"
              f" -> {managed.log_path}", flush=True)
        return None

    def _pump_log(self, managed: ManagedCamera) -> None:
        from camera_config import redact_text
        process = managed.process
        with managed.log_path.open("a", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(redact_text(line))
                log.flush()

    def _stop_process(self, managed: ManagedCamera) -> None:
        with managed.lock:
            process = managed.process
            managed.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    # ------------------------------------------------------------ reaper
    def _reap_loop(self) -> None:
        """One background thread covering every camera: fires scheduled stops
        and pending restarts. Never sleeps out a backoff itself -- one
        camera's crash-restart delay must not stall the stop/restart check for
        every other camera sharing this single thread; the wait is a
        timestamp another tick polls, not a blocking call."""
        while not self._reaper_stop.wait(2.0):
            now = time.monotonic()

            if now >= self._next_capacity_snapshot:
                self._next_capacity_snapshot = now + CAPACITY_SNAPSHOT_SECONDS
                running_detecting = [m.camera for m in self.cameras.values()
                                     if m.running() and m.detecting]
                idle_demand, peak_demand = (total_demand(running_detecting)
                                            if running_detecting else (0.0, 0.0))
                log_metric(self._capacity_log_path, "capacity_snapshot", print_line=False,
                          running_cameras=len(running_detecting),
                          idle_demand=round(idle_demand, 2), peak_demand=round(peak_demand, 2),
                          gpu_fps=self.gpu_fps, vram_free_mib=gpu_free_mib())

            for managed in list(self.cameras.values()):
                with managed.lock:
                    process = managed.process
                    crashed = process is not None and process.poll() is not None
                    restart_due = managed.restart_at is not None and now >= managed.restart_at

                if crashed:
                    code = process.returncode
                    with managed.lock:
                        ran_for = now - managed.started_at
                        managed.process = None
                        managed.last_exit = (code, f"exited with {code} after {ran_for:.0f}s")
                        still_wanted = managed.wanted()
                        if still_wanted:
                            wait = managed.backoff if ran_for < 60 else RESTART_BACKOFF_SECONDS
                            managed.backoff = min(managed.backoff * 2, RESTART_BACKOFF_MAX) \
                                if ran_for < 60 else RESTART_BACKOFF_SECONDS
                            managed.restart_at = now + wait
                    if still_wanted:
                        print(f"[{managed.name}] exited with {code} after {ran_for:.0f}s; "
                              f"restarting in {wait:.0f}s", flush=True)

                elif restart_due:
                    with managed.lock:
                        managed.restart_at = None
                        if managed.wanted():
                            self._ensure_started(managed)
