#!/usr/bin/env python3
"""ONE process for every camera: synchronised rounds, one model, one batch per round.

THE LOOP
    A round starts when the first camera is due. Every camera due within `sync_ms` of it joins
    the round, and all of them are snapshotted at the same moment: each reader thread decodes
    its NEXT frame the instant the round asks (so the frames of one round were captured within
    one frame interval of each other, ~40 ms at 25 fps, and the spread is measured and
    reported). The frames go through the detector together -- 12 cameras = 12 frames = 384
    tiles -- and each camera's result is then handled (windows, crops, events) off the round
    thread, so saving a 4K JPEG never delays the next round.

    Then the next round, and so on. Cameras keep the phase they were sampled in, so a fleet
    that started together stays together: idle cameras land in the same round every
    `scan_interval`, a camera inside a recording window is sampled every `save_interval` and
    only the cameras that are due that tick are in it.

WHY IT IS NOT 12 PROCESSES
    The old design ran one live_capture.py per camera, each with its own copy of the model and
    its own CUDA context. Measured: ~1.4 GB of VRAM each, so 12 cameras (~17 GB) did not fit a
    12 GB card at all and the app refused to start more than a few. One process holds the
    model once.

WHY THE NETWORK RUNS ON CHUNKS OF 32 TILES INSIDE A 12-FRAME ROUND  (measured, RTX 4070 Ti)
        tiles per forward   ms per 12-frame round   torch peak     nvidia-smi total
                32                  470              1014 MiB         1741 MiB
                96                  517              2391 MiB         3803 MiB
               384                  544              8498 MiB        10419 MiB
    Frames are uploaded and tiled together and the round is one synchronous unit, but one
    frame's 32 tiles already saturate the GPU, so bigger forwards only cost memory and time.
    See batch_detector.py for the single-frame numbers and the YOLO/RF-DETR comparison.

NOTHING BLOCKS CAPTURE, NOTHING QUEUES WITHOUT BOUND
    Readers never wait for inference: they keep reading the socket and only decode a frame
    when a round (or the preview cadence) asks. A camera whose previous result is still being
    saved skips this round instead of queueing another. A camera that cannot deliver a frame
    in time is left out of the round, not waited for. All three are counted in status.json.

CONTROL AND STATUS (files, so the app and this process share nothing but a directory)
    control.json   {"enabled": bool, "viewed": [camera, ...]}   written by app.py
    status.json    round timing, capture skew, frame age, VRAM, per-camera state, written ~1/s
    A camera is processed when `enabled` (every camera), or its `recording` flag file exists,
    or it is in `viewed`. Notifications go out for cameras that are enabled or recording;
    `recording` alone decides whether frames are written to disk -- enabling acquisition never
    turns recording on, and recording never needs acquisition enabled.

    ./acquisition_service.py --config config.yaml --enable-all            # headless, all cameras
    ./acquisition_service.py --config config.yaml --control /tmp/acq/control.json   # as app.py runs it
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import statistics
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import yaml

from batch_detector import build_detector
from camera_config import ROTATE_CODES, build_cameras, redact
from gpu_frame import GpuFrame, as_bgr, fit_width_any, rotate_frame
from metrics_log import log_metric
from model_infer import draw_boxes, fit_width, save_crop, write_jpeg
from segment_feed import SegmentFeed, SegmentHub
from layout import SiteLayout
import captured
from sitepaths import add_sites_root_argument, site_dir, site_root
from tracker import Linker



def configure_ffmpeg_env() -> None:
    """OpenCV's ffmpeg options for the live streams. Done in main(), not at import: importing this
    module (tests, tools) must not change how every other video file in the process is opened."""
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                          "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")
    # A degraded HEVC link prints several decode errors per frame; at twelve cameras that floods the
    # service log. Silenced here -- the reader still notices a stream that stops delivering.
    os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")


# What a camera with no data is shown as: a plain black frame, never a stale picture.
BLACK_JPEG = cv2.imencode(".jpg", np.zeros((720, 1280, 3), np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 60])[1].tobytes()

NO_DECODABLE_FRAME_TIMEOUT = 15.0
SNAPSHOT_TIMEOUT = 0.5          # a camera slower than this to deliver its frame sits the round out
MISS_BACKOFF_MAX_S = 8.0
EVENT_COOLDOWN_S = 5.0          # at most one notification per camera in this time
PREROLL_MAX_AGE_S = 30.0        # idle frames older than this are not context for a detection
IDLE_LINKER_RESET_S = 60.0
EVENTS_MAX_BYTES = 2_000_000


def rescale(boxes, frame, width):
    longest = max(frame.shape[:2])
    if longest <= width:
        return boxes
    k = width / longest
    return [{**b, "bbox": [v * k for v in b["bbox"]]} for b in boxes]


class _ClosedCapture:
    """What _open_capture returns after NVDEC failed to start and the CPU path is not (yet) used."""
    failure = "nvdec"

    def isOpened(self) -> bool:
        return False

    def release(self) -> None:
        pass


class Window:
    """One detection burst on disk: <frames>/<day>/<camera>-<HHMMSS>/ (same layout as live_capture)."""

    def __init__(self, root: Path, camera: str, now: datetime, layout: SiteLayout | None = None):
        self.camera = camera
        self.day = now.strftime("%Y-%m-%d")
        # Unique across inbox, confirmed and trash, so a repeated HHMMSS (restart in the same
        # second, the autumn clock change) can never reuse the identity of a window that moved.
        base = f"{camera}-{now.strftime('%H%M%S')}"
        self.name = layout.unique_window(self.day, base) if layout else base
        self.dir = root / self.day / self.name
        suffix = 1
        while self.dir.exists():
            suffix += 1
            self.name = f"{base}-{suffix}"
            self.dir = root / self.day / self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.extra: dict = {}                    # extra window.json fields (e.g. preroll_frames)
        self.opened_at = time.time()
        self.opened_monotonic = time.monotonic()
        self.last_detection = time.monotonic()

    def next_path(self) -> Path:
        path = self.dir / f"frame_{self.frames}.jpg"
        self.frames += 1
        return path

    def close(self) -> None:
        (self.dir / "window.json").write_text(json.dumps({
            "camera": self.camera, "frames": self.frames,
            "opened_at": self.opened_at, "closed_at": time.time(), **self.extra}, indent=1), encoding="utf-8")


@dataclass
class SiteCtx:
    name: str
    cfg: dict
    frames_root: Path           # where NEW windows are written: the site's inbox/
    detections_root: Path
    live_root: Path
    layout: SiteLayout | None = None
    manifest_lock: threading.Lock = field(default_factory=threading.Lock)
    manifest: object = None

    @property
    def crops_root(self) -> Path:
        return self.detections_root / "crops"

    @property
    def events_path(self) -> Path:
        return self.detections_root / "live_events.jsonl"

    def write_manifest(self, rows: list[dict]) -> None:
        with self.manifest_lock:
            if self.manifest is None:
                self.manifest = (self.detections_root / "detections.jsonl").open("a", encoding="utf-8")
            self.manifest.writelines(json.dumps(r) + "\n" for r in rows)
            self.manifest.flush()

    def write_event(self, row: dict) -> None:
        with self.manifest_lock:
            path = self.events_path
            try:
                if path.exists() and path.stat().st_size > EVENTS_MAX_BYTES:
                    keep = path.read_text(encoding="utf-8").splitlines()[-500:]
                    path.write_text("\n".join(keep) + "\n", encoding="utf-8")
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")


@dataclass
class Snapshot:
    frame: object
    capture_ts: float
    capture_mono: float
    kind: str = "frame"          # "frame" | "caught_up" (nothing new yet) | "none" (no data at all)
    source: str = "live"         # "live" | "segments"


class CameraWorker:
    """One camera: a reader thread (socket, decode on demand, preview) plus the per-camera
    state machine (windows, track linking, events) that results are fed through."""

    def __init__(self, camera, site: SiteCtx, args):
        self.camera, self.site, self.args = camera, site, args
        self.live = camera.live
        self.rotation = ROTATE_CODES.get(camera.rotate)
        self.live_dir = site.live_root / camera.name
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self.preview_path = self.live_dir / "latest.jpg"
        self.recording_flag = self.live_dir / "recording"
        self.scan_interval = 1.0 / float(self.live["scan_fps"])
        self.save_interval = 1.0 / float(self.live["save_fps"])
        self.infer_every_n = int(self.live["infer_every_n"])
        self.cooldown = float(self.live["cooldown_seconds"])
        self.max_window = float(self.live["max_window_seconds"])
        self.quality = int(self.live["jpeg_quality"])
        self.preview_interval = 1.0 / float(self.live["preview_fps"])
        self.idle_preview_interval = 1.0 / float(self.live.get("idle_preview_fps", 0.2))
        self.link_px = float(self.live.get("link_px", 0) or 0)
        self.preroll_frames = int(self.live.get("preroll_frames", 3))
        # Hard negatives: a few frames per period in which the detector found nothing (default 5 per 2 h).
        per, period = float(self.live.get("negatives_per_period", 5)), float(self.live.get("negatives_period_s", 7200))
        self.neg_every = period / per if per > 0 and period > 0 else 0.0     # 0 = off
        self.next_negative = 0.0                                             # monotonic; first one is due at once
        self.preroll: deque = deque(maxlen=max(1, self.preroll_frames))   # (bgr frame, capture_ts, monotonic)

        # control, set by the service
        self.viewed = False
        self.notify = False
        self.acq_enabled = False        # the header's Acquisition switch: while on, every camera records

        # where frames come from: the live stream, or the NVR's recording pulled in segments
        self.feed: SegmentFeed | None = None            # set by the service when an NVR is configured
        self.source_pref = str(self.live.get("source", "auto"))
        self.live_giveup_s = float(self.live.get("live_giveup_s", 30.0))
        self.live_retry_s = float(self.live.get("live_retry_s", 300.0))
        self.degrade_misses = int(self.live.get("degrade_misses", 3))
        self.mode = "live"
        self.decoder = "cpu"                              # "nvdec" | "cpu", chosen by the service
        self.decoder_pref = str(self.live.get("decoder", "auto"))
        self.nvdec_failures = 0
        self.retry_live_at = 0.0
        self.last_frame = None                           # last real picture, for the segment-mode preview
        self.last_lag_s: float | None = None

        # reader <-> round handshake
        self._stop = threading.Event()
        self._snap_req = threading.Event()
        self._snap_ready = threading.Event()
        self._snapshot: Snapshot | None = None
        self._thread: threading.Thread | None = None
        self.state = "connecting"
        self.connected_at: float | None = None
        self.reconnects = -1
        self.last_error: str | None = None

        # scheduling
        self.next_due = 0.0
        self.busy = threading.Event()          # a result is still being handled
        self.snapshot_misses = 0
        self.miss_streak = 0
        self.armed = False                      # joined the round grid since it last became live
        self.post_skips = 0
        self.samples = 0
        self.last_sample_mono = 0.0

        # per-camera detection state (touched only by the post-processing task, one at a time)
        self.window: Window | None = None
        self.carried: list[dict] = []
        self.since_infer = 0
        self.saved_frames = 0
        self._ids = iter(range(10 ** 9))
        self.linker = Linker(self._fresh, min_gate_px=self.link_px) if self.link_px > 0 else None
        self.last_box_mono = 0.0
        self.announced: set[int] = set()
        self.last_event_mono = 0.0
        self.preview_boxes: tuple[list[dict], bool] = ([], True)

    def _fresh(self) -> int:
        return next(self._ids)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.feed is not None:
            self.feed.start()
        self._thread = threading.Thread(target=self._run, name=f"read-{self.camera.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)
        if self.feed is not None:
            self.feed.stop()
        self.close_window()

    def cadence(self) -> float:
        # segment frames exist only at the scan cadence, so a window cannot ask for more
        return self.scan_interval if self.mode == "segments" else (
            self.save_interval if self.window else self.scan_interval)

    def has_data(self) -> bool:
        return self.state == "live" or (self.state == "segments")

    def source(self) -> str:
        return {"live": "live", "segments": "segments"}.get(self.state, "none")

    def recording(self) -> bool:
        """Save windows? Yes while Acquisition is on (every camera), or this camera's own Record flag is set.
        Merely watching a stream (neither) previews and detects but writes nothing."""
        return self.args.always_record or self.acq_enabled or self.recording_flag.exists()

    # --------------------------------------------------------------------- reader
    def request_snapshot(self) -> None:
        self._snapshot = None
        self._snap_ready.clear()
        self._snap_req.set()

    def wait_snapshot(self, deadline: float) -> Snapshot | None:
        self._snap_ready.wait(max(0.0, deadline - time.monotonic()))
        self._snap_req.clear()
        snap, self._snapshot = self._snapshot, None
        return snap

    def _write_json(self, name: str, payload: dict) -> None:
        path = self.live_dir / name
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    def _write_black(self) -> None:
        """A camera with no data is a black frame, so nobody mistakes an old picture for now."""
        tmp = self.preview_path.with_name(f".{self.preview_path.name}.tmp")
        tmp.write_bytes(BLACK_JPEG)
        tmp.replace(self.preview_path)

    def can_fall_back(self) -> bool:
        return self.source_pref == "auto" and self.feed is not None

    def _run(self) -> None:
        """Supervisor: live while it can be held, the NVR's recording when it cannot."""
        if self.source_pref == "segments" and self.feed is not None:
            self.mode = "segments"
        if self.mode == "segments":
            self.feed.set_active(True)
        while not self._stop.is_set():
            if self.mode == "live":
                if self._live_session() == "degrade" and self.can_fall_back():
                    self.mode = "segments"
                    self.retry_live_at = time.monotonic() + self.live_retry_s
                    self.miss_streak = 0
                    self.feed.set_active(True)
                    print(f"[{self.camera.name}] live is not holding -> pulling NVR segments "
                          f"(live is retried in {self.live_retry_s:.0f}s)", flush=True)
            else:
                if self._segments_session() == "retry_live":
                    self.mode = "live"
                    self.miss_streak = 0
                    print(f"[{self.camera.name}] trying the live stream again", flush=True)
        self.state = "stopped"

    def _open_capture(self, url: str):
        """NVDEC when this camera is set up for it, else the CPU decoder (cv2/ffmpeg)."""
        if self.decoder == "nvdec":
            from nvdec_capture import NvdecCapture
            try:
                return NvdecCapture(url)
            except Exception as exc:                  # no CUDA context, decoder refused, ...
                self.last_error = f"nvdec: {type(exc).__name__}: {exc}"
                return _ClosedCapture()          # failure = "nvdec": no CUDA context, import error, ...
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    def _note_open_failure(self, capture) -> None:
        """A stream NVDEC itself cannot decode, three times in a row, is served by the CPU decoder.
        A camera that is merely unreachable is not NVDEC's fault and never changes the decoder
        (otherwise one network outage would silently move the whole fleet back onto the CPU)."""
        if self.decoder != "nvdec" or getattr(capture, "failure", None) != "nvdec":
            return
        self.nvdec_failures += 1
        if self.nvdec_failures >= 3 and self.decoder_pref == "auto":
            self.decoder = "cpu"
            print(f"[{self.camera.name}] NVDEC could not open the stream {self.nvdec_failures}x "
                  f"-> decoding on the CPU", flush=True)

    def _preview(self, frame, boxes_carried) -> None:
        boxes, carried = boxes_carried
        width = int(self.live["preview_width"])
        write_jpeg(self.preview_path, draw_boxes(fit_width_any(frame, width),
                                                 rescale(boxes, frame, width), carried=carried), 80)

    def _live_session(self) -> str:
        """Hold the RTSP stream. Returns "degrade" when it cannot be held, "stop" at shutdown."""
        url = self.camera.record_url
        backoff = 1.0
        last_preview = 0.0
        trying_since = time.monotonic()
        while not self._stop.is_set():
            capture = self._open_capture(url)
            if not capture.isOpened():
                self.state, self.last_error = "connecting", f"cannot open {redact(url)}"
                capture.release()
                self._note_open_failure(capture)
                if self.can_fall_back() and time.monotonic() - trying_since > self.live_giveup_s:
                    return "degrade"
                if not self.can_fall_back() and time.monotonic() - last_preview > self.idle_preview_interval:
                    last_preview = time.monotonic()
                    self._write_black()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.nvdec_failures = 0
            self.reconnects += 1
            self.connected_at = time.time()
            self.state, self.last_error = "live", None
            self._write_json("status.json", {"connected_at": self.connected_at,
                                             "reconnects": max(0, self.reconnects)})
            print(f"[{self.camera.name}] connected" +
                  (f" (reconnect #{self.reconnects})" if self.reconnects else ""), flush=True)
            backoff = 1.0
            last_good = time.monotonic()
            trying_since = time.monotonic()
            if self.feed is not None and self.source_pref == "auto":
                self.feed.set_active(False)            # live holds again: free the bandwidth
            try:
                while not self._stop.is_set():
                    if self.can_fall_back() and self.miss_streak >= self.degrade_misses:
                        self.last_error = f"{self.miss_streak} rounds without a frame"
                        return "degrade"
                    # grab() takes the next frame off the socket; retrieve() converts it. Only the
                    # frames a round or the preview actually wants are converted.
                    if not capture.grab():
                        self.last_error = "stream ended"
                        break
                    now = time.monotonic()
                    want_snap = self._snap_req.is_set()
                    interval = self.preview_interval if self.viewed else self.idle_preview_interval
                    want_preview = now - last_preview >= interval
                    if not (want_snap or want_preview):
                        continue
                    ok, frame = capture.retrieve()
                    if not ok or frame is None:
                        if now - last_good > NO_DECODABLE_FRAME_TIMEOUT:
                            self.last_error = "link up but no frame decodes"
                            break
                        continue
                    last_good = now
                    if self.rotation is not None:
                        frame = rotate_frame(frame, self.rotation)
                    ts = time.time()
                    if want_snap:
                        self._snapshot = Snapshot(frame, ts, now)
                        self._snap_req.clear()
                        self._snap_ready.set()
                        self._write_json("frame_ts.json", {"capture_ts": ts})
                    if want_preview:
                        last_preview = time.monotonic()
                        self._preview(frame, self.preview_boxes)
                        if not want_snap:
                            self._write_json("frame_ts.json", {"capture_ts": ts})
            finally:
                capture.release()
            self.state = "reconnecting"
            if self.can_fall_back() and time.monotonic() - last_good > self.live_giveup_s:
                return "degrade"
            self._stop.wait(2.0)
        return "stop"

    def _deliver(self, snap: Snapshot) -> None:
        self._snapshot = snap
        self._snap_req.clear()
        self._snap_ready.set()

    def _segments_session(self) -> str:
        """Serve frames pulled from the NVR. Returns "retry_live" when live should be tried."""
        last_preview = 0.0
        self.last_error = None
        while not self._stop.is_set():
            if self.source_pref == "auto" and time.monotonic() >= self.retry_live_at:
                return "retry_live"
            self.state = "segments" if self.feed.has_data() else "nodata"
            if self._snap_req.wait(0.25):
                kind, image, wall = self.feed.next_frame()
                if kind == "frame":
                    self.last_frame = image
                    self.last_lag_s = max(0.0, time.time() - wall)
                    self._deliver(Snapshot(image, wall, time.monotonic(), "frame", "segments"))
                    self._write_json("frame_ts.json", {"capture_ts": wall})
                else:
                    self._deliver(Snapshot(None, time.time(), time.monotonic(), kind, "segments"))
            now = time.monotonic()
            interval = self.preview_interval if self.viewed else self.idle_preview_interval
            if now - last_preview >= interval:
                last_preview = now
                if self.last_frame is not None and self.feed.has_data():
                    self._preview(self.last_frame, self.preview_boxes)
                else:
                    self.last_frame = None
                    self._write_black()
        return "stop"

    # ------------------------------------------------------- per-camera state machine
    def needs_model(self) -> bool:
        return self.window is None or self.since_infer % self.infer_every_n == 0

    def close_window(self) -> None:
        if self.window is None:
            return
        self.window.close()
        print(f"[{self.camera.name}] window {self.window.name} closed after "
              f"{self.window.frames} frame(s)", flush=True)
        self.window = None
        self.carried = []
        self.since_infer = 0

    def process(self, snap: Snapshot, boxes: list[dict] | None, timing: dict, round_info: dict) -> None:
        """Handle one sampled frame. `boxes` is None when the model did not run on it."""
        try:
            self._process(snap, boxes, timing, round_info)
        except Exception as exc:                     # a bad frame must not kill the camera
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[{self.camera.name}] processing failed: {self.last_error}", file=sys.stderr,
                  flush=True)
        finally:
            self.busy.clear()

    def _process(self, snap, boxes, timing, round_info) -> None:
        frame, capture_ts = snap.frame, snap.capture_ts
        run_model = boxes is not None
        site, camera = self.site, self.camera
        self.samples += 1
        self.last_sample_mono = time.monotonic()

        if run_model:
            if self.linker is not None:
                if time.monotonic() - self.last_box_mono > IDLE_LINKER_RESET_S:
                    self.linker = Linker(self._fresh, min_gate_px=self.link_px)
                self.linker.update(boxes)
            else:
                for b in boxes:
                    b["track"] = self._fresh()
            if boxes:
                self.last_box_mono = time.monotonic()
            self.carried = boxes
        else:
            boxes = self.carried
        self.since_infer += 1

        recording = self.recording()
        if self.window is not None and not recording:
            print(f"[{camera.name}] window {self.window.name} stopped early: recording turned off",
                  flush=True)
            self.close_window()

        stamp = datetime.now()
        if boxes and run_model and recording:
            if self.window is None:
                self.window = Window(site.frames_root, camera.name, stamp, site.layout)
                self.since_infer = 1
                print(f"[{stamp:%H:%M:%S}] [{camera.name}] window {self.window.name} opened "
                      f"({len(boxes)} detection(s), best {max(b['conf'] for b in boxes):.2f})",
                      flush=True)
            self._flush_preroll()
            self.window.last_detection = time.monotonic()

        if (self.neg_every > 0 and recording and run_model and not boxes and self.window is None
                and time.monotonic() >= self.next_negative and site.layout is not None):
            self.next_negative = time.monotonic() + self.neg_every
            save_path = captured.negative_path(site.layout, camera.name, stamp)
            write_jpeg(save_path, as_bgr(frame), self.quality)
            print(f"[{stamp:%H:%M:%S}] [{camera.name}] negative frame saved ({save_path.name})", flush=True)

        if self.window is None and recording and self.preroll_frames > 0:
            # Idle frames stay in a short ring (host memory, not encoded), so the seconds BEFORE a detection
            # can be saved when one arrives -- the detector usually fires a few frames after the bird appears.
            self.preroll.append((as_bgr(frame), capture_ts, time.monotonic()))
        elif not recording:
            self.preroll.clear()

        saved_name = None
        encode_ms = None
        if self.window is not None:
            path = self.window.next_path()
            t0 = time.monotonic()
            frame = as_bgr(frame)                  # saving needs host pixels (GpuFrame -> one D2H)
            write_jpeg(path, frame, self.quality)
            encode_ms = (time.monotonic() - t0) * 1000
            saved_name = path.name
            self.saved_frames += 1
            rows = []
            for b in boxes:
                crop_rel = None
                if run_model:
                    crop_path = (site.crops_root / self.window.day / self.window.name /
                                 f"{path.stem}-t{b['track']:04d}.jpg")
                    if save_crop(frame, b["bbox"], b["conf"], crop_path):
                        crop_rel = str(crop_path.relative_to(site.crops_root.parent))
                rows.append({"day": self.window.day, "window": self.window.name, "file": path.name,
                             "bbox": b["bbox"], "conf": b["conf"], "track": b["track"],
                             "camera": camera.name, "carried": not run_model, "vx": 0.0, "vy": 0.0,
                             "saved": True, "crop": crop_rel, "capture_ts": capture_ts})
            if rows:
                site.write_manifest(rows)
            if time.monotonic() - self.window.last_detection > self.cooldown:
                self.close_window()
            elif time.monotonic() - self.window.opened_monotonic > self.max_window:
                print(f"[{camera.name}] window {self.window.name} hit {self.max_window:g}s; splitting",
                      flush=True)
                self.close_window()

        if run_model and self.notify and boxes:
            self._emit_events(boxes, capture_ts, saved_name, recording, snap.source)

        self.preview_boxes = (boxes, not run_model)
        log_metric(self.live_dir / "metrics.jsonl", "stage_latency", print_line=False,
                   camera=camera.name,
                   age_ms=round(round_info["age_ms"], 1), skew_ms=round(round_info["skew_ms"], 1),
                   round_frames=round_info["frames"], round_ms=round(round_info["round_ms"], 1),
                   infer_ms_round=round(timing.get("infer_ms", 0.0), 1) if run_model else None,
                   encode_ms=None if encode_ms is None else round(encode_ms, 2), run_model=run_model)

    def _flush_preroll(self) -> None:
        """Write the ring into the window just opened, oldest first, as frames with no detections.
        Frames older than PREROLL_MAX_AGE_S are dropped (a stalled camera's stale pictures are not context)."""
        now, kept = time.monotonic(), 0
        while self.preroll:
            image, _, mono = self.preroll.popleft()
            if now - mono > PREROLL_MAX_AGE_S:
                continue
            write_jpeg(self.window.next_path(), image, self.quality)
            self.saved_frames += 1
            kept += 1
        self.window.extra["preroll_frames"] = kept

    def _emit_events(self, boxes, capture_ts, file, recording, source="live") -> None:
        new = [b for b in boxes if b["track"] not in self.announced]
        if not new:
            return
        for b in new:
            self.announced.add(b["track"])
        if len(self.announced) > 5000:
            self.announced = {b["track"] for b in boxes}
        now = time.monotonic()
        if now - self.last_event_mono < EVENT_COOLDOWN_S:
            return
        self.last_event_mono = now
        best = max(new, key=lambda b: b["conf"])
        row = {"ts": time.time(), "frame_ts": capture_ts,
               "lag_s": round(max(0.0, time.time() - capture_ts), 1),
               "source": source, "site": self.site.name, "camera": self.camera.name,
               "track": best["track"], "conf": round(best["conf"], 3),
               "bbox": [round(v, 1) for v in best["bbox"]], "count": len(boxes),
               "recording": bool(recording)}
        if self.window is not None and file:
            row.update(day=self.window.day, window=self.window.name, file=file)
        self.site.write_event(row)

    def status(self) -> dict:
        return {"state": self.state, "source": self.source(), "mode": self.mode,
                "decoder": self.decoder,
                "lag_s": self.last_lag_s if self.mode == "segments" else None,
                "segments": self.feed.status() if self.feed is not None else None,
                "connected_at": self.connected_at,
                "reconnects": max(0, self.reconnects), "samples": self.samples,
                "last_sample_age_s": (None if not self.last_sample_mono
                                      else round(time.monotonic() - self.last_sample_mono, 1)),
                "window": self.window.name if self.window else None,
                "recording": self.recording(), "notify": self.notify, "viewed": self.viewed,
                "snapshot_misses": self.snapshot_misses, "post_skips": self.post_skips,
                "error": self.last_error}


class Service:
    def __init__(self, args):
        self.args = args
        self.stop = threading.Event()
        self.sites: dict[str, SiteCtx] = {}
        self.cameras = {}          # name -> (Camera, SiteCtx)
        self.workers: dict[str, CameraWorker] = {}
        self.hub: SegmentHub | None = None
        self.detector = None
        self.detector_note: str | None = None
        self.model_info: dict = {}
        self.pool = ThreadPoolExecutor(max_workers=args.post_workers, thread_name_prefix="post")
        self.started = time.time()
        # round statistics (bounded)
        self.rounds = 0
        self.modelled_frames = 0        # frames that went through the network since start
        self.stat = {k: deque(maxlen=240) for k in
                     ("round_ms", "infer_ms", "skew_ms", "age_ms", "frames", "members")}
        self.oom_events = 0
        self.nvdec_ok, self.nvdec_why = False, "not checked"
        self.anchor = time.monotonic()
        self.control_mtime = 0.0
        self.control = {"enabled": bool(args.enable_all), "viewed": []}

    # ----------------------------------------------------------------------- setup
    def load(self) -> None:
        if self.args.device == "cpu":
            self.args.decoder = "cpu"          # NVDEC is GPU work too: "CPU" must mean no GPU at all
        if self.args.decoder != "cpu":
            from nvdec_capture import available
            self.nvdec_ok, self.nvdec_why = available()
            print("NVDEC decode: " + ("available" if self.nvdec_ok else f"not used ({self.nvdec_why})"),
                  flush=True)
        first_model = None
        for config in self.args.config:
            cfg = yaml.safe_load(Path(config).read_text(encoding="utf-8")) or {}
            name = cfg.get("site", "site")
            layout = SiteLayout(site_root(cfg, self.args.sites_root, config))
            layout.ensure()
            ctx = SiteCtx(
                name=name, cfg=cfg,
                frames_root=layout.inbox, layout=layout,
                detections_root=site_dir(cfg, "dataset", "detections", base=self.args.sites_root,
                                         source=config) / self.args.bucket,
                live_root=site_dir(cfg, "live", base=self.args.sites_root, source=config))
            ctx.detections_root.mkdir(parents=True, exist_ok=True)
            self.sites[name] = ctx
            first_model = first_model or (cfg.get("model") or {})
            nvr = cfg.get("nvr")
            if nvr and self.hub is None and self.args.source != "live":
                creds = cfg.get("defaults") or {}
                self.hub = SegmentHub(nvr["host"], str(creds.get("username", "")),
                                      str(creds.get("password", "")), nvr.get("scheme", "https"),
                                      nvr.get("timezone", "Europe/Bucharest"),
                                      int(nvr.get("max_downloads", 3)))
                print(f"NVR {nvr['host']}: segment fallback available "
                      f"(max {int(nvr.get('max_downloads', 3))} downloads at once)", flush=True)
            for camera in build_cameras(cfg):
                if camera.name in self.cameras:
                    raise SystemExit(f"camera {camera.name!r} appears in two configs")
                self.cameras[camera.name] = (camera, ctx)
        model_cfg = first_model or {}
        weights = self.args.weights or model_cfg.get("weights")
        mtype = self.args.model_type or model_cfg.get("type") or "yolo"
        variant = self.args.rfdetr_variant or model_cfg.get("variant") or "nano"
        self.model_info = {"weights": weights, "type": mtype, "device": self.args.device}
        if not weights or not Path(weights).exists():
            self.detector_note = f"no usable weights ({weights}) -- video only, no detection"
            print(self.detector_note, file=sys.stderr, flush=True)
            return
        live0 = next(iter(self.cameras.values()))[0].live
        print(f"loading {weights} ({mtype}) once for {len(self.cameras)} camera(s)", flush=True)
        try:
            self.detector = build_detector(
                weights, mtype, variant, tile=int(live0["tile"]), overlap=int(live0["overlap"]),
                conf=float(live0["conf"]), merge_iou=float(live0["nms_iou"]), half=not self.args.fp32,
                force_list=self.args.detector == "list", device=self.args.device)
            if hasattr(self.detector, "max_tiles"):
                self.detector.max_tiles = self.args.max_tiles
        except Exception as exc:
            self.detector_note = f"could not load {weights}: {exc} -- video only"
            print(self.detector_note, file=sys.stderr, flush=True)
            self.detector = None

    # --------------------------------------------------------------------- control
    def read_control(self) -> None:
        path = self.args.control
        if not path:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self.control_mtime:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.control_mtime = mtime
        self.control = {"enabled": bool(data.get("enabled")) or self.args.enable_all,
                        "viewed": list(data.get("viewed") or [])}

    def reconcile(self) -> None:
        enabled = self.control["enabled"]
        viewed = set(self.control["viewed"])
        wanted = set()
        for name, (camera, ctx) in self.cameras.items():
            recording = self.args.always_record or (ctx.live_root / name / "recording").exists()
            if enabled or recording or name in viewed:
                wanted.add(name)
        for name in wanted - self.workers.keys():
            camera, ctx = self.cameras[name]
            worker = CameraWorker(camera, ctx, self.args)
            if self.args.source:                      # --source overrides the per-camera config
                worker.source_pref = self.args.source
            worker.decoder_pref = self.args.decoder or worker.decoder_pref
            worker.decoder = ("nvdec" if self.nvdec_ok and worker.decoder_pref in ("auto", "nvdec")
                              else "cpu")
            if self.hub is not None and camera.host:
                lv = camera.live
                worker.feed = SegmentFeed(
                    name, camera.host, self.hub, ctx.live_root / name / "segments",
                    sample_interval=worker.scan_interval,
                    clip_seconds=float(lv.get("segment_seconds", 16.0)),
                    max_backlog_s=float(lv.get("max_backlog_s", 60.0)),
                    stale_s=float(lv.get("stale_s", 180.0)), rotation=worker.rotation)
            self.workers[name] = worker
            worker.start()
            print(f"[{name}] activated", flush=True)
        for name in list(self.workers.keys() - wanted):
            self.workers.pop(name).stop()
            print(f"[{name}] deactivated", flush=True)
        for name, worker in self.workers.items():
            worker.viewed = name in viewed
            worker.acq_enabled = enabled
            worker.notify = enabled or worker.recording()

    # ---------------------------------------------------------------------- rounds
    def align(self, t: float, step: float) -> float:
        """The first multiple of `step` after the service's anchor that is at or after t.

        Each camera schedules itself onto the grid of its OWN cadence (idle: scan interval,
        in a window: save interval), all counted from one anchor. Two idle cameras therefore
        land on the same instants forever, however differently they connected, and a camera
        in a window (0.25 s) falls on every 8th point of the idle (2 s) grid.
        """
        return self.anchor + math.ceil((t - self.anchor) / step - 1e-6) * step

    def arm_live_workers(self) -> None:
        now = time.monotonic()
        for w in self.workers.values():
            if w.has_data() and not w.armed:
                w.armed = True
                w.next_due = self.align(now, w.cadence())
            elif not w.has_data():
                w.armed = False

    def run_round(self) -> bool:
        """One synchronised round. Returns False when nothing was due."""
        self.arm_live_workers()
        now = time.monotonic()
        idle = [w for w in self.workers.values() if w.armed and not w.busy.is_set()]
        if not idle:
            return False
        first = min(w.next_due for w in idle)
        if first > now:
            return False
        members = [w for w in idle if w.next_due <= first + self.args.sync_ms / 1000.0]
        round_start = time.monotonic()
        for w in self.workers.values():            # due, but still saving its last result
            if w.busy.is_set() and w.next_due <= round_start:
                w.post_skips += 1
                w.next_due = self.align(round_start + w.cadence() - self.args.sync_ms / 1000.0,
                                        w.cadence())
        for w in members:
            w.request_snapshot()
        deadline = round_start + SNAPSHOT_TIMEOUT
        got: list[tuple[CameraWorker, Snapshot]] = []
        for w in members:
            snap = w.wait_snapshot(deadline)
            if snap is not None and snap.kind != "frame":
                # segment mode: nothing new yet ("caught up") or nothing at all ("none" -> black)
                if snap.kind == "none":
                    w.state = "nodata"
                    w.last_frame = None
                    w._write_black()
                w.next_due = self.align(round_start + w.cadence() - self.args.sync_ms / 1000.0,
                                        w.cadence())
                continue
            if snap is None:
                w.snapshot_misses += 1
                w.miss_streak += 1
                # back off exponentially so a camera that cannot deliver is not allowed to
                # make every round wait for it
                w.next_due = self.align(time.monotonic() + min(MISS_BACKOFF_MAX_S,
                                                               0.25 * 2 ** min(w.miss_streak, 6)),
                                        w.cadence())
            else:
                w.miss_streak = 0
                got.append((w, snap))
        if not got:
            return True

        slack = self.args.sync_ms / 1000.0
        for w, _ in got:
            w.next_due = self.align(round_start + w.cadence() - slack, w.cadence())
        live_marks = [s.capture_mono for _, s in got if s.source == "live"]   # replayed frames have no sync
        skew_ms = (max(live_marks) - min(live_marks)) * 1000 if len(live_marks) > 1 else 0.0
        modelled = [(w, s) for w, s in got if self.detector is not None and w.needs_model()]
        timing: dict = {}
        results: dict[str, list[dict]] = {}
        age_ms = 0.0
        t_detect = time.monotonic()
        if modelled:
            age_ms = (t_detect - min(s.capture_mono for _, s in modelled)) * 1000
            try:
                out = self.detector.detect([s.frame for _, s in modelled], timing)
            except Exception as exc:
                out = self._recover(exc, modelled, timing)
            for (w, _), boxes in zip(modelled, out):
                results[w.camera.name] = boxes
        round_ms = (time.monotonic() - round_start) * 1000

        info = {"age_ms": age_ms, "skew_ms": skew_ms, "frames": len(modelled), "round_ms": round_ms}
        for w, snap in got:
            w.busy.set()
            self.pool.submit(w.process, snap, results.get(w.camera.name), timing, info)
        self.rounds += 1
        self.modelled_frames += len(modelled)
        self.stat["round_ms"].append(round_ms)
        self.stat["infer_ms"].append(timing.get("infer_ms", 0.0))
        self.stat["skew_ms"].append(skew_ms)
        self.stat["age_ms"].append(age_ms)
        self.stat["frames"].append(len(modelled))
        self.stat["members"].append(len(members))
        return True

    def _recover(self, exc, modelled, timing) -> list[list[dict]]:
        """CUDA out-of-memory: halve the tiles per forward and retry once, rather than lose the round."""
        self.oom_events += 1
        text = str(exc)
        print(f"detector failed: {type(exc).__name__}: {text[:160]}", file=sys.stderr, flush=True)
        if "out of memory" in text.lower() and hasattr(self.detector, "max_tiles"):
            try:
                import torch
                torch.cuda.empty_cache()
            except ImportError:
                pass
            self.detector.max_tiles = max(4, self.detector.max_tiles // 2)
            print(f"retrying with {self.detector.max_tiles} tiles per forward", flush=True)
            try:
                return self.detector.detect([s.frame for _, s in modelled], timing)
            except Exception as exc2:
                print(f"retry failed: {exc2}", file=sys.stderr, flush=True)
        return [[] for _ in modelled]

    # ----------------------------------------------------------------------- status
    def write_status(self) -> None:
        path = self.args.status
        if not path:
            return
        q = lambda name, p: (None if not self.stat[name] else
                             round(sorted(self.stat[name])[min(len(self.stat[name]) - 1,
                                                               int(p * len(self.stat[name])))], 1))
        vram = {}
        try:
            import torch
            # mem_get_info() creates a CUDA context: never in CPU mode, where the GPU must stay untouched
            if self.args.device != "cpu" and torch.cuda.is_available():
                free, total = torch.cuda.mem_get_info()
                vram = {"torch_allocated_mib": round(torch.cuda.memory_allocated() / 2**20),
                        "torch_peak_mib": round(torch.cuda.max_memory_allocated() / 2**20),
                        "gpu_free_mib": round(free / 2**20), "gpu_total_mib": round(total / 2**20)}
        except Exception:
            pass
        active = len(self.workers)
        payload = {
            "pid": os.getpid(), "started": self.started, "updated": time.time(),
            "enabled": self.control["enabled"], "model": self.model_info,
            "detecting": self.detector is not None, "detection_note": self.detector_note,
            "max_tiles_per_forward": getattr(self.detector, "max_tiles", None),
            "cameras_configured": len(self.cameras), "cameras_active": active,
            "est_mbit_s": round(active * 6.0, 1),      # ~6 Mbit/s per 4K main stream (measured earlier)
            "rounds": self.rounds, "modelled_frames": self.modelled_frames, "oom_events": self.oom_events,
            "round_ms": {"p50": q("round_ms", 0.5), "p95": q("round_ms", 0.95)},
            "infer_ms": {"p50": q("infer_ms", 0.5), "p95": q("infer_ms", 0.95)},
            "capture_skew_ms": {"p50": q("skew_ms", 0.5), "p95": q("skew_ms", 0.95)},
            "frame_age_ms": {"p50": q("age_ms", 0.5), "p95": q("age_ms", 0.95)},
            "frames_per_round": (round(statistics.mean(self.stat["frames"]), 2)
                                 if self.stat["frames"] else None),
            "cameras_per_round": (round(statistics.mean(self.stat["members"]), 2)
                                  if self.stat["members"] else None),
            "vram": vram,
            "decoders": {k: sum(1 for w in self.workers.values() if w.decoder == k)
                         for k in ("nvdec", "cpu")},
            "sources": {k: sum(1 for w in self.workers.values() if w.source() == k)
                        for k in ("live", "segments", "none")},
            "segment_hub": self.hub.status() if self.hub else None,
            "cameras": {n: w.status() for n, w in self.workers.items()},
        }
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------------ main
    def serve(self) -> None:
        next_status = 0.0
        next_control = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_control:
                self.read_control()
                self.reconcile()
                next_control = now + 0.5
            if not self.run_round():
                due = [w.next_due for w in self.workers.values() if not w.busy.is_set()]
                busy = any(w.busy.is_set() for w in self.workers.values())
                wait = (0.02 if busy else 0.2) if not due else max(0.005, min(0.2, min(due) - time.monotonic()))
                self.stop.wait(wait)
            if time.monotonic() >= next_status:
                self.write_status()
                next_status = time.monotonic() + 1.0
        for worker in list(self.workers.values()):
            worker.stop()
        self.pool.shutdown(wait=True)
        for ctx in self.sites.values():
            if ctx.manifest:
                ctx.manifest.close()
        self.write_status()


def main() -> int:
    configure_ffmpeg_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", action="append", required=True, type=Path)
    ap.add_argument("--weights", default=None)
    ap.add_argument("--model-type", default=None, choices=["yolo", "rfdetr"])
    ap.add_argument("--rfdetr-variant", default=None)
    ap.add_argument("--fp32", action="store_true")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"],
                    help="auto = GPU when there is one, else CPU; cpu = never use the GPU (also "
                         "decodes on the CPU); cuda = the GPU, or the detector fails to load and "
                         "the service reports it (never a silent CPU fallback)")
    ap.add_argument("--bucket", default="live")
    ap.add_argument("--control", type=Path, default=None, help="control.json written by app.py")
    ap.add_argument("--status", type=Path, default=None, help="status.json this process writes")
    ap.add_argument("--enable-all", action="store_true",
                    help="process every camera regardless of control.json (headless use)")
    ap.add_argument("--always-record", action="store_true",
                    help="save windows without the per-camera recording flag")
    ap.add_argument("--source", default=None, choices=["auto", "live", "segments"],
                    help="override live.source for every camera (default: each camera's config, "
                         "auto = live, falling back to the NVR's recording)")
    ap.add_argument("--detector", default="auto", choices=["auto", "list"],
                    help="auto = GPU-resident tiling + batched network on CUDA; list = the old "
                         "per-tile model.predict path (only for A/B measurements)")
    ap.add_argument("--decoder", default=None, choices=["auto", "nvdec", "cpu"],
                    help="where live video is decoded (default: each camera's live.decoder, "
                         "auto = NVDEC when PyNvVideoCodec + CUDA are usable, else the CPU)")
    ap.add_argument("--max-tiles", type=int, default=32,
                    help="tiles per network forward inside a round (default 32 = one 4K frame; "
                         "see the VRAM table in this file's docstring)")
    ap.add_argument("--sync-ms", type=float, default=150.0,
                    help="cameras due within this long of the first join the same round")
    ap.add_argument("--post-workers", type=int, default=4,
                    help="threads that save frames/crops after a round")
    ap.add_argument("--stop-on-stdin-eof", action="store_true",
                    help="stop cleanly when stdin closes (how app.py asks, and how this "
                         "process notices its parent died; works on Windows too)")
    add_sites_root_argument(ap)
    args = ap.parse_args()

    service = Service(args)
    service.load()
    if args.stop_on_stdin_eof:
        def watch_parent():
            try:
                sys.stdin.read()
            except (OSError, ValueError):
                pass
            service.stop.set()
        threading.Thread(target=watch_parent, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    signal.signal(signal.SIGINT, lambda *_: service.stop.set())
    print(f"acquisition service up: {len(service.cameras)} camera(s) known, "
          f"{'detector ready' if service.detector else 'video only'}", flush=True)
    service.serve()
    print("acquisition service stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
