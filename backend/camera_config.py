#!/usr/bin/env python3
"""Camera list from a config file: credentials, RTSP URLs, per-camera capture
settings.

TRIMMED FROM THE RESEARCH PIPELINE'S motion_recorder.py:
    That version also carries ring-buffer spooling, clip recording modes and
    the classical motion detector's tuning block, none of which this app uses --
    here a camera is a name, a stream URL, and how densely to sample it.

WHY CREDENTIALS ARE INJECTED INTO THE URL, AND WHY redact() EXISTS:
    OpenCV/ffmpeg take credentials only as part of the RTSP URL, and they echo
    that URL -- password included -- into their own error output when a stream
    fails. So every URL that reaches a log, a console, or an exception must go
    through redact()/redact_text() first.
"""
from __future__ import annotations

import copy as copymod
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

import cv2

# Same direction convention as the capture pipeline's ffmpeg transpose:
# ccw == transpose=2. Applied to the decoded frame, not to a stored clip.
ROTATE_CODES = {
    "ccw": cv2.ROTATE_90_COUNTERCLOCKWISE,
    "cw": cv2.ROTATE_90_CLOCKWISE,
}

DEFAULTS: dict = {
    "username": None,
    "password": None,
    # Main stream. A bird is 10-45px in a 4K frame; the substream shrinks that
    # to 2-7px, below anything the model has ever been trained on, so detection
    # runs on the main stream even though it costs more bandwidth.
    "record_url": "rtsp://{host}:554/Streaming/Channels/101",
    "live": {
        # Idle: how often a frame is pulled and run through the model while
        # nothing is happening. This is the steady-state GPU and bandwidth cost.
        "scan_fps": 1.0,
        # In-window: how often a frame is SAVED once a detection opens a window.
        # Dense enough to see a bird move between frames and to play the window
        # back as a clip. Every saved frame is a native-resolution JPEG -- on a
        # 4K camera that measures 1.7 MB, so this rate is also the disk bill:
        # 4 fps is about 400 MB per minute that a window is open.
        "save_fps": 4.0,
        # In-window: run the model on every Nth saved frame, carrying the last
        # measured boxes across the frames between. Tiled 4K inference costs
        # ~154ms/frame (FP16) -- about 6.5 fps for the whole GPU -- so inferring
        # every saved frame at save_fps is not physically possible.
        "infer_every_n": 4,
        # Quiet time before a window is declared over.
        "cooldown_seconds": 8.0,
        # Longest a single window may run before it is split into a new one. A
        # busy scene triggers something every few seconds for hours on end, and
        # a window that never closes is both un-reviewable (it never shows as
        # finished) and unbounded on disk -- a 4K camera writes roughly
        # 190 MB/minute while a window is open.
        "max_window_seconds": 120.0,
        # Floor passed to the model.
        "conf": 0.25,
        # Tiling geometry. 640px tiles with 128px overlap so a bird on a tile
        # seam still sits fully inside at least one tile.
        "tile": 640,
        "overlap": 128,
        "nms_iou": 0.5,
        # The live view's own refresh rate, deliberately separate from how often
        # the model runs: a frame is cheap to decode and draw, an inference is
        # not, so the view can stay smooth at several frames a second while the
        # model still only runs once a second.
        "preview_fps": 6.0,
        # Longest edge of the preview JPEG the live viewer streams.
        "preview_width": 1280,
        # JPEG quality for saved window frames. q95 is visually lossless and
        # ~15x smaller than the equivalent native PNG.
        "jpeg_quality": 95,
    },
}

# A camera name becomes a directory component (window folders, preview paths,
# log file names), so it may not carry anything that would escape or confuse a
# path -- the research pipeline only sanitised its `pair` label and left names
# unchecked, which held only because nobody had used an awkward name yet.
UNSAFE_IN_PATH = re.compile(r"[^A-Za-z0-9._-]+")

# ffmpeg echoes the input URL -- password included -- in its error messages, so
# anything from its stderr must be scrubbed before it reaches a log.
CREDENTIALS_IN_TEXT = re.compile(r"(\w+://[^\s:/@]+):[^\s@]+@")


def deep_merge(base: dict, override: dict) -> dict:
    out = copymod.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def with_credentials(url: str, user: str | None, password: str | None) -> str:
    """Inject credentials into an RTSP URL unless it already carries them."""
    if not user:
        return url
    parts = urlsplit(url)
    if "@" in (parts.netloc or ""):
        return url
    creds = quote(user, safe="")
    if password:
        creds += ":" + quote(str(password), safe="")
    return urlunsplit(parts._replace(netloc=f"{creds}@{parts.netloc}"))


def redact(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in (parts.netloc or ""):
        return url
    creds, _, host = parts.netloc.rpartition("@")
    user, sep, _ = creds.partition(":")
    return urlunsplit(parts._replace(netloc=f"{user}{':***' if sep else ''}@{host}"))


def redact_text(text: str) -> str:
    return CREDENTIALS_IN_TEXT.sub(r"\1:***@", text)


@dataclass
class Camera:
    name: str
    record_url: str
    live: dict = field(default_factory=dict)
    host: str | None = None
    # Corbu is mounted on its side and cannot self-correct, so the decoded
    # frame is rotated before anything else touches it.
    rotate: str = "none"
    # Which physical mast this camera shares with another, if any -- the same
    # field motion_recorder.py groups recordings by. Read-only here (this app
    # never writes it): the client UI uses it only to group cameras that are
    # physically on the same mast, never to infer a mast's compass position,
    # which this config does not carry.
    pair: str | None = None

    @property
    def safe_name(self) -> str:
        """The camera's name as a path component."""
        return UNSAFE_IN_PATH.sub("-", self.name).strip("-.") or "camera"


def build_cameras(cfg: dict) -> list[Camera]:
    defaults = deep_merge(DEFAULTS, cfg.get("defaults") or {})
    rotate = str((cfg.get("capture") or {}).get("rotate") or "none").lower()
    if rotate not in ("none", *ROTATE_CODES):
        raise ValueError(f"capture.rotate must be none/ccw/cw, got {rotate!r}")

    cameras: list[Camera] = []
    seen: set[str] = set()
    for entry in cfg.get("cameras") or []:
        merged = deep_merge(defaults, entry)
        name = merged.get("name")
        if not name:
            raise ValueError("every camera needs a 'name'")
        if name in seen:
            raise ValueError(f"duplicate camera name {name!r}")
        seen.add(name)

        record_url = merged.get("record_url")
        if not record_url:
            raise ValueError(f"camera {name!r} is missing record_url")

        # {host} and {channel} are substituted into the URL template. A template
        # referencing a placeholder the camera does not define is a config error.
        subs = {}
        if merged.get("host") is not None:
            subs["host"] = merged["host"]
        if merged.get("channel") is not None:
            subs["channel"] = merged["channel"]
        try:
            record_url = record_url.format(**subs)
        except KeyError as exc:
            raise ValueError(
                f"camera {name!r}: URL template uses {{{exc.args[0]}}} but no "
                f"{exc.args[0]!r} is set") from None

        live = merged["live"]
        if float(live["save_fps"]) <= 0 or float(live["scan_fps"]) <= 0:
            raise ValueError(f"camera {name!r}: live.save_fps and live.scan_fps must be > 0")
        if int(live["infer_every_n"]) < 1:
            raise ValueError(f"camera {name!r}: live.infer_every_n must be >= 1")

        pair = merged.get("pair")
        camera = Camera(
            name=name,
            host=merged.get("host"),
            record_url=with_credentials(record_url, merged.get("username"),
                                        merged.get("password")),
            live=live,
            rotate=rotate,
            pair=str(pair) if pair is not None else None,
        )
        if camera.safe_name != name:
            raise ValueError(
                f"camera {name!r}: the name is used as a directory component, so it "
                f"may only contain letters, digits, dot, dash and underscore "
                f"(would become {camera.safe_name!r})")
        cameras.append(camera)

    if not cameras:
        raise ValueError("config has no cameras")
    return cameras


def select_cameras(cameras: list[Camera], wanted: str | None) -> list[Camera]:
    """Filter by a comma-separated list of names or hosts. Unknown names are an
    error rather than a silent empty selection."""
    if not wanted:
        return cameras
    names = [w.strip() for w in wanted.split(",") if w.strip()]
    by_key = {c.name: c for c in cameras}
    by_key.update({c.host: c for c in cameras if c.host})
    missing = [n for n in names if n not in by_key]
    if missing:
        raise SystemExit(
            f"no camera named {', '.join(missing)} in this config "
            f"(have: {', '.join(c.name for c in cameras)})")
    picked, out = set(), []
    for n in names:
        camera = by_key[n]
        if camera.name not in picked:
            picked.add(camera.name)
            out.append(camera)
    return out
