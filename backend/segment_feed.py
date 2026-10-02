#!/usr/bin/env python3
"""Frames from the NVR's recording, pulled asynchronously, for a camera that cannot be watched live.

THE IDEA
    A camera whose RTSP stream will not hold (the WireGuard tunnel is saturated, the camera is
    unreachable, frames will not decode) has usually still been recorded by the NVR. For such a
    camera a background thread pulls SHORT SEGMENTS of that recording, decodes each once from
    start to end, keeps the frames at the scan cadence as JPEGs, and deletes the segment. The
    acquisition rounds then consume those frames in order -- detection runs on whatever has
    arrived, a little behind real time, instead of on nothing.

WHAT "NO DATA" MEANS HERE
    No frame has been pulled for `stale_s` seconds (NVR unreachable, camera not recorded, every
    request failing). The camera is then shown as a BLACK frame; it is never given a stale
    picture presented as current. A camera that is merely waiting for its next segment is
    "caught up", not "no data": it keeps its last picture and sits the round out.

WHY SEGMENTS ARE DECODED SEQUENTIALLY, NOT SEEKED  (measured, 4K HEVC, 10 s clip)
    Sequential decode: 3.7 ms/frame (271 fps), clean frames. Seeking to an arbitrary time:
    137-454 ms and a burst of "Could not find ref with POC" errors, because the first frames
    after a seek depend on frames that were skipped -- the same grey, smeared frame ffmpeg
    `-ss` produced earlier in this project. So frames are taken in one pass at download time.

HONEST LIMITS
    - Frames are BEHIND real time by the segment length plus download time (typically 20-40 s),
      and time-stamped from the segment's requested start: a clip begins on a keyframe, so the
      stamp can be ~1 s off. Segment-mode frames are scan-rate (one per `scan_interval`), never
      the denser rate of a recording window.
    - Bandwidth is shared: at most `max_downloads` segments are fetched at once across ALL
      cameras, and a camera only downloads while it is actually in segment mode.
    - The backlog is bounded: frames older than `max_backlog_s` are dropped, never queued.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

import cv2

from nvr_archive import Nvr, NvrError

SAFETY_S = 6.0            # never ask for footage newer than this: the NVR is still writing it
CHANNEL_TTL_S = 600.0


class SegmentHub:
    """What every camera's feed shares: one NVR session, the channel map, one download gate."""

    def __init__(self, host: str, user: str, password: str, scheme: str = "https",
                 tz: str = "Europe/Bucharest", max_downloads: int = 3):
        self.nvr = Nvr(host, user, password, scheme, tz)
        self.gate = threading.BoundedSemaphore(max_downloads)
        self.lock = threading.Lock()
        self.channels: dict[str, int] = {}
        self.channels_at = 0.0
        self.reachable: bool | None = None
        self.last_error: str | None = None
        self.clock_at = 0.0
        self.downloaded_bytes = 0
        self.downloads = 0

    def track_for(self, host: str) -> int:
        """NVR track id for a camera address; the channel map is asked of the NVR itself."""
        with self.lock:
            if not self.channels or time.monotonic() - self.channels_at > CHANNEL_TTL_S \
                    or host not in self.channels:
                try:
                    self.channels = self.nvr.channel_map()
                    self.channels_at = time.monotonic()
                    self.reachable, self.last_error = True, None
                except NvrError as exc:
                    self.reachable, self.last_error = False, str(exc)
                    if host not in self.channels:
                        raise
            if host not in self.channels:
                raise NvrError(f"{host} is not an online channel on the NVR")
            return self.channels[host] * 100 + 1

    def now(self):
        with self.lock:
            if time.monotonic() - self.clock_at > CHANNEL_TTL_S:
                try:
                    self.nvr.sync_clock()
                    self.clock_at = time.monotonic()
                    self.reachable = True
                except NvrError as exc:
                    self.reachable, self.last_error = False, str(exc)
                    self.clock_at = time.monotonic() - CHANNEL_TTL_S + 30      # retry in 30 s
        return self.nvr.now()

    def status(self) -> dict:
        return {"reachable": self.reachable, "error": self.last_error,
                "clock_offset_s": round(self.nvr.clock_offset, 1),
                "downloads": self.downloads, "downloaded_mib": round(self.downloaded_bytes / 2**20, 1)}


class SegmentFeed:
    def __init__(self, name: str, host: str, hub: SegmentHub, cache_dir: Path,
                 sample_interval: float, clip_seconds: float = 16.0, max_backlog_s: float = 60.0,
                 stale_s: float = 180.0, rotation=None):
        self.name, self.host, self.hub = name, host, hub
        self.cache_dir = cache_dir
        self.sample_interval = sample_interval
        self.clip_seconds, self.max_backlog_s, self.stale_s = clip_seconds, max_backlog_s, stale_s
        self.rotation = rotation
        cache_dir.mkdir(parents=True, exist_ok=True)
        for leftover in cache_dir.glob("*"):          # a previous run's frames are not "now"
            leftover.unlink(missing_ok=True)
        self.active = threading.Event()               # set while the camera is in segment mode
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._frames: deque[tuple[float, Path]] = deque()      # (wall epoch, jpeg), oldest first
        self._seen_ms: set[int] = set()                # frame times already kept (a retried span re-extracts)
        self._frontier = None                          # wall time up to which footage was pulled
        self.last_frame_mono = 0.0
        self.last_error: str | None = None
        self.error_streak = 0
        self.segments_pulled = 0
        self.dropped_stale = 0
        self.gaps = 0
        self.last_wall: float | None = None
        self._thread = threading.Thread(target=self._loop, name=f"seg-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)
        for leftover in self.cache_dir.glob("*"):
            leftover.unlink(missing_ok=True)

    def set_active(self, on: bool) -> None:
        if on:
            self.active.set()
            self._wake.set()
        else:
            self.active.clear()

    # ---------------------------------------------------------------- consumer
    def has_data(self) -> bool:
        return bool(self._frames) or (self.last_frame_mono and
                                      time.monotonic() - self.last_frame_mono < self.stale_s)

    def next_frame(self):
        """('frame', image, wall_epoch) | ('caught_up', None, None) | ('none', None, None)."""
        cutoff = time.time() - self.max_backlog_s
        with self._lock:
            while self._frames and self._frames[0][0] < cutoff - self.sample_interval:
                _, path = self._frames.popleft()
                path.unlink(missing_ok=True)
                self.dropped_stale += 1
            entry = self._frames.popleft() if self._frames else None
        if entry is not None:
            wall, path = entry
            image = cv2.imread(str(path))
            path.unlink(missing_ok=True)
            if image is not None:
                self.last_wall = wall
                return "frame", image, wall
        if self.last_frame_mono and time.monotonic() - self.last_frame_mono < self.stale_s:
            return "caught_up", None, None
        return "none", None, None

    # ---------------------------------------------------------------- producer
    def _loop(self) -> None:
        track = None
        while not self._stop.is_set():
            if not self.active.is_set():
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                if track is None:
                    track = self.hub.track_for(self.host)
                now = self.hub.now()
            except NvrError as exc:
                self._fail(exc)
                continue
            if self._frontier is None or (now - self._frontier).total_seconds() > \
                    self.max_backlog_s + 2 * self.clip_seconds:
                # first pull, or far behind: start near live and drop the gap rather than queue it
                self._frontier = now - timedelta(seconds=self.clip_seconds + SAFETY_S)
            end = self._frontier + timedelta(seconds=self.clip_seconds)
            wait = (end - (now - timedelta(seconds=SAFETY_S))).total_seconds()
            if wait > 0:
                self._stop.wait(min(wait, 2.0))
                continue
            if not self.hub.gate.acquire(timeout=3.0):
                continue
            try:
                self._pull(track, self._frontier, end)
                self._frontier = end
                self.error_streak = 0
                self.last_error = None
            except NvrError as exc:
                if "nothing recorded" in str(exc):
                    self.gaps += 1                       # the NVR has nothing for this span
                    self._frontier = end
                else:
                    self._fail(exc)
            finally:
                self.hub.gate.release()

    def _fail(self, exc: Exception) -> None:
        self.error_streak += 1
        self.last_error = str(exc)[:200]
        self._stop.wait(min(30.0, 2.0 ** min(self.error_streak, 5)))

    def _pull(self, track: int, start, end) -> None:
        nvr = self.hub.nvr
        segments = nvr.search(track, start, end)
        pieces, _ = nvr.slices(start, end, segments)
        for a, b, uri in pieces:
            clip = self.cache_dir / f"{int(a.timestamp() * 1000)}.mp4"
            size = nvr.download_to(uri, clip)
            self.hub.downloaded_bytes += size
            self.hub.downloads += 1
            try:
                # stored as the equivalent HOST epoch, so lag = time.time() - frame time is honest
                self._extract(clip, a.timestamp() - nvr.clock_offset)
            finally:
                clip.unlink(missing_ok=True)
            self.segments_pulled += 1

    def _extract(self, clip: Path, start_wall: float) -> None:
        """One sequential pass: keep a frame every sample_interval, as JPEG."""
        capture = cv2.VideoCapture(str(clip), cv2.CAP_FFMPEG)
        try:
            fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
            step = max(1, round(self.sample_interval * fps))
            index = 0
            while capture.grab():
                if index % step == 0:
                    ok, frame = capture.retrieve()
                    if ok and frame is not None:
                        if self.rotation is not None:
                            frame = cv2.rotate(frame, self.rotation)
                        wall = start_wall + index / fps
                        ms = int(wall * 1000)
                        path = self.cache_dir / f"{ms}.jpg"
                        if ms in self._seen_ms:
                            index += 1
                            continue
                        if cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                            with self._lock:
                                self._frames.append((wall, path))
                                self._seen_ms.add(ms)
                                if len(self._seen_ms) > 2000:
                                    self._seen_ms = {int(w * 1000) for w, _ in self._frames}
                            self.last_frame_mono = time.monotonic()
                index += 1
        finally:
            capture.release()

    def status(self) -> dict:
        return {"frames_waiting": len(self._frames), "segments_pulled": self.segments_pulled,
                "dropped_stale": self.dropped_stale, "gaps": self.gaps,
                "error": self.last_error, "error_streak": self.error_streak,
                "lag_s": None if self.last_wall is None else round(time.time() - self.last_wall, 1)}
