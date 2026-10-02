#!/usr/bin/env python3
"""The whole app on one port: watch the cameras live, and review what the model
found -- across as many sites (configs) as you point it at.

THREE VIEWS, ONE SERVER:
    /          review -- session folders, a frame scrubber, verdicts and notes
    /live      live    -- pick cameras across every configured site, watch
                          their detections as they happen, and switch each
                          one's recording on or off
    /dataset   dataset -- audit the training labels themselves (needs --dataset)

    They are one process because they are one job: you notice something on the
    live view and go and judge it, and a second port (and a second thing to
    start, forward and remember) only got in the way of that.

MULTIPLE SITES, ONE APP:
    Pass --config more than once (one per site, e.g. corbu and babadag) and
    every view spans all of them: Live groups cameras by site, Review gets a
    site switcher, and each site's data stays in its own
    <sites-root>/<site>/... tree, exactly as if you had run this once per site.
    Camera names must be unique across the configs given -- they are how a
    stream request and a recording toggle find their way back to one site.

CAPTURE RUNS ON DEMAND, ONE CAMERA AT A TIME:
    This process never opens an RTSP connection or runs the model itself --
    live_capture.py does that, as its own subprocess, one per camera. What this
    app owns is WHEN one runs: picking a camera in Live starts its process (see
    capture_manager.py); the open MJPEG connection that follows is what keeps
    it running, and losing the last such viewer stops it again a short grace
    period later, unless recording is on. So the fleet actually pulling RTSP
    and spending GPU time is always the fleet someone is watching or has asked
    to record -- never merely "every camera this config happens to list".

RECORDING IS A SWITCH, NOT A CONSEQUENCE OF WATCHING:
    live_capture.py never writes a frame to disk unless recording is on for
    that camera -- a marker file it polls, which this app creates or removes
    and which also keeps its process running regardless of viewers. Watching a
    feed costs GPU time and bandwidth the moment you pick it; it costs disk,
    and puts a camera's detections in front of Review, only once you also turn
    recording on.

WHAT IT WRITES PER SITE, AND WHAT IT DELIBERATELY DOES NOT TOUCH:
    verdicts.json      {track_id: keep|drop|unsure}   -- flat strings, because
                       the training-export tooling matches these values by
                       string equality; a richer shape here would silently
                       produce empty training splits rather than an error.
    track_meta.json    {track_id: {species, distance, size}} -- the optional
                       annotation fields, kept in their own file for that reason.
    manual_boxes.jsonl boxes a reviewer drew for something the model missed,
                       one row per box; boxes following one bird across frames
                       share a `track`.
    box_edits.jsonl    corrections to the MODEL's boxes -- moved or removed.
                       detections.jsonl itself is never rewritten: it is the
                       record of what the detector actually produced.
    folder_status.json {day/window: {reviewed: true}} -- a status tag only.
                       Nothing is ever moved on disk.
    live/<camera>/recording  a marker file: present means "capture, save it";
                       absent (the default) means preview only.

    ./app.py --config config.corbu.yaml
    ./app.py --config config.corbu.yaml --config config.yaml --model live
"""
from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import os
import re
import secrets
import signal
import socketserver
import struct
import sys
import threading
import subprocess
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import cv2
import numpy as np
import yaml

import auth
from camera_config import Camera, build_cameras
from capacity import MEASURED_GPU_FPS, budget_warning, gpu_free_mib, total_demand
from capture_manager import CaptureManager
from dataset_review import DatasetIndex
import lifecycle
from layout import Retention, SiteLayout, gone_windows
from link_watch_read import summarize as summarize_link_watch
from model_infer import fit_width, save_crop, write_jpeg
from risk import LEVELS, RiskPolicy, alert_message, decision_for
from settings import (DEFAULT_MODELS_DIR, DEFAULT_SETTINGS_PATH, SettingsStore, check_data_dir,
                      detect_devices, device_flag, discover_models, resolve_model)
import archive
import captured
from sitepaths import DEFAULT_SITES_ROOT, add_sites_root_argument, site_dir, site_root

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Frozen by PyInstaller, the built client sits in the bundle, not next to the sources.
CLIENT_DIST = Path(getattr(sys, "_MEIPASS", ROOT)) / "client" / "dist"
# Set by the desktop launcher: a one-time secret that /desktop-login trades for an
# operator session, so the local window needs no password. Unset everywhere else.
DESKTOP_TOKEN: str | None = None

# GET paths reachable with no session at all: the client SPA's own shell
# (index.html + its built JS/CSS) is public -- it is code, not data, and it
# has to load before it can show the login screen or redirect to it. Every
# API and every legacy page below still requires a session; the SPA's own
# calls to those will get a 401 and route the visitor to /login itself.
PUBLIC_GET_PREFIXES = ("/login", "/app")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
# frame_2 must sort before frame_10, which a plain string sort gets wrong.
FRAME_NUMBER = re.compile(r"(\d+)")
# A window folder written by live_capture.py is "<camera>-<HHMMSS>"; folders
# from older single-camera runs are just "<HHMMSS>".
WINDOW_NAME = re.compile(r"^(?:(?P<camera>.+)-)?(?P<time>\d{6})$")
# A folder with no window.json that nothing has written to for this long is
# finished, not recording -- otherwise every folder captured before this app
# existed would be permanently un-markable.
RECORDING_GRACE_SECONDS = 90.0

BOUNDARY = "frame"
# How long a latest.jpg can go untouched before the page calls it stale. Idle
# scanning writes a frame about once a second, so a few seconds of silence means
# the capture process is gone, not merely quiet.
STALE_AFTER = 6.0
# How long a stream may go without writing anything before it sends a placeholder
# instead. Writing is the only way a browser that has gone away is detected.
KEEPALIVE_SECONDS = 3.0
# A connection that reconnected 1 second ago can still hand back a frame --
# decode buffers don't empty instantly -- so a fresh frame alone doesn't mean
# the link is good. "live" is withheld until the CURRENT connection has held
# this long without dropping, however many frames arrived in the meantime.
STABLE_AFTER_SECONDS = 5.0


def safe_segment(value: str) -> str | None:
    """`value` if it is a single, ordinary path component, else None.

    day/window/file arrive from the page as plain strings and are joined onto
    the frames root by several code paths (listing a folder, measuring a frame,
    saving a hand-drawn box's crop). Without this, a request naming
    `../../../somewhere` would reach outside the tree -- to list it, to read an
    image from it, or to write a crop into it.
    """
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\0" in value:
        return None
    return value


def safe_folder(day: str, window: str) -> tuple[str, str] | None:
    if safe_segment(day) is None or safe_segment(window) is None:
        return None
    return day, window


def natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower()
            for part in FRAME_NUMBER.split(name)]


def track_key(day: str, window: str, track: int) -> str:
    return f"{day}/{window}/t{track:04d}"


_WINDOW_CAMERA_CACHE: dict[tuple[str, str, str], str | None] = {}


def _camera_from_marker(site: "Site", day: str, window: str) -> str | None:
    """The camera window.json names, if the window has closed -- authoritative, and the
    only source when the folder name carries extra tags (imported sessions do)."""
    key = (site.name, day, window)
    if key not in _WINDOW_CAMERA_CACHE:
        name = None
        marker = site.window_root(day, window) / day / window / "window.json"
        if safe_folder(day, window) is not None and marker.is_file():
            try:
                name = json.loads(marker.read_text(encoding="utf-8")).get("camera")
            except (OSError, json.JSONDecodeError):
                name = None
        if name is not None:               # an open window has no marker yet: ask again later
            _WINDOW_CAMERA_CACHE[key] = name
        return name
    return _WINDOW_CAMERA_CACHE[key]


def camera_for_window(site: Site, window: str, day: str | None = None) -> str:
    """Best-effort camera name for a window folder, for display only.

    live_capture.py names a window "<camera>-<HHMMSS>" once a site has more
    than one camera; older, single-camera runs (Corbu's whole history so
    far) wrote just "<HHMMSS>", carrying no camera name at all. A prefixed
    window's own name is authoritative; an unprefixed one only means
    "unknown" if the site actually has more than one camera to be ambiguous
    between -- with exactly one, there is nothing to guess.
    """
    if day:
        marked = _camera_from_marker(site, day, window)
        if marked:
            return marked
    match = WINDOW_NAME.match(window)
    if match and match.group("camera"):
        return match.group("camera")
    if len(site.cameras) == 1:
        return site.cameras[0].name
    return "unknown"


def _tracks_for_site(site: "Site") -> list[dict]:
    """Every track's raw detection history, grouped by (day, window, track).

    This is the one place that reads detections+verdicts+track_meta and
    groups boxes into tracks -- confirmed_sightings (verdict == "keep" only)
    and the live recent_detections/camera_activity endpoints (no verdict
    filter, since they surface the model's raw, unconfirmed output) both
    filter this same list rather than re-deriving it.
    """
    site.store.refresh()
    verdict_map = site.verdicts.snapshot()
    meta_map = site.track_meta.snapshot()

    rows: list[dict] = []
    gone = gone_windows(site.layout) if site.layout else set()   # rejected/expired: hide their records
    for folder_id, boxes in site.store.snapshot_by_folder().items():
        if folder_id in gone:
            continue
        day, _, window = folder_id.partition("/")
        by_track: dict[int, list[dict]] = defaultdict(list)
        for box in boxes:
            by_track[box["track"]].append(box)

        camera = camera_for_window(site, window, day)
        for track, track_boxes in by_track.items():
            key = track_key(day, window, track)
            ordered = sorted(track_boxes, key=lambda b: natural_key(b["file"]))
            # Prefer a frame that actually has a crop -- a track's single
            # highest-confidence detection can land on a carried (not
            # independently inferred) frame, which was never saved with one.
            # A gallery card is only as good as its picture.
            with_crop = [b for b in track_boxes if b.get("crop")]
            best = max(with_crop or track_boxes, key=lambda b: b["conf"])
            meta = meta_map.get(key) or {}
            rows.append({
                "id": key,
                "day": day,
                "window": window,
                "site": site.name,
                "camera": camera,
                "track": track,
                "verdict": verdict_map.get(key),
                "confidence": round(best["conf"], 3),
                "frames": len(track_boxes),
                "crop": best.get("crop"),
                "species": meta.get("species"),
                "distance": meta.get("distance"),
                "size": meta.get("size"),
                "risk": meta.get("risk"),
                "boxes": ordered,  # full frame-ordered history (direction, tracking page)
            })

    rows.sort(key=lambda r: (r["day"], r["window"], r["track"]), reverse=True)
    return rows


_SIGHTING_FIELDS = ("id", "day", "window", "camera", "track", "confidence",
                    "frames", "crop", "species", "distance", "size")


def confirmed_sightings(site: "Site") -> list[dict]:
    """One row per track with a "keep" verdict, most recent first.

    Reads the same three sources Review itself reads (detections, verdicts,
    track_meta) and does no writing of its own -- a sightings feed is a
    different view of Review's data, not a separate copy of it.
    """
    return [{k: r[k] for k in _SIGHTING_FIELDS} for r in _tracks_for_site(site)
            if r["verdict"] == "keep"]


def track_direction(boxes: list[dict]) -> str | None:
    """"up"/"down" (frame-relative) from a track's earliest to latest
    independently-inferred box, or None with fewer than two to compare --
    a carried box is an interpolated duplicate of the frame before it, not a
    new observation, so it can't tell you which way the bird is moving.
    """
    non_carried = [b for b in boxes if not b.get("carried")]
    if len(non_carried) < 2:
        return None
    y0 = (non_carried[0]["bbox"][1] + non_carried[0]["bbox"][3]) / 2
    y1 = (non_carried[-1]["bbox"][1] + non_carried[-1]["bbox"][3]) / 2
    if y1 < y0:
        return "up"
    if y1 > y0:
        return "down"
    return None


def _manual_tracks_for_site(site: "Site") -> list[dict]:
    """Hand-drawn / followed boxes grouped into tracks, shaped like model tracks.

    Boxes following one bird share a `track` (mt0001...); a lone drawn box is its own
    track. species/distance/risk ride on the box rows, first non-empty value wins.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in site.manual_boxes.snapshot():
        groups[(row["day"], row["window"], row.get("track") or row["id"])].append(row)
    out = []
    for (day, window, track), rows in groups.items():
        rows.sort(key=lambda r: natural_key(r["file"]))
        meta: dict = {}
        for row in rows:
            for field in TrackMetaStore.FIELDS:
                if field not in meta and row.get(field) not in (None, ""):
                    meta[field] = row[field]
        out.append({"id": f"{day}/{window}/{track}", "kind": "manual", "day": day,
                    "window": window, "track": track, "files": [r["file"] for r in rows],
                    "species": meta.get("species"), "distance": meta.get("distance"),
                    "risk": meta.get("risk"), "camera": camera_for_window(site, window, day)})
    return out


def alerts_for_site(site: "Site", model_rows: list[dict] | None = None) -> list[dict]:
    """One alert per track that has a distance (or a reviewer's risk), decided and
    recorded in alerts.json. Idempotent: a poll that changes nothing writes nothing.
    """
    candidates = []
    for r in (model_rows if model_rows is not None else _tracks_for_site(site)):
        if not r["boxes"]:
            continue
        candidates.append({"id": r["id"], "kind": "model", "day": r["day"], "window": r["window"],
                           "track": r["track"], "camera": r["camera"], "species": r.get("species"),
                           "distance": r.get("distance"), "risk": r.get("risk"),
                           "files": [b["file"] for b in r["boxes"]]})
    candidates.extend(_manual_tracks_for_site(site))
    return site.alerts.sync(site.name, site.risk, candidates)


def recent_detections(sites: dict[str, "Site"], limit: int = 30) -> list[dict]:
    """Newest raw (unconfirmed) tracks across every site, for the live
    notification feed. Deliberately not filtered by verdict -- see
    _tracks_for_site's docstring -- and capped the same way /api/sightings
    caps its page size, so this can never grow into an unbounded response.

    A track that has a distance also carries its risk, the decision taken and the
    alert sentence, plus the first/last frame of its interval so the bell can open
    Review exactly there. A hand-followed track with a distance is listed too.
    """
    rows: list[dict] = []
    for site in sites.values():
        model_rows = _tracks_for_site(site)
        alerts = {a["id"]: a for a in alerts_for_site(site, model_rows)}
        for r in model_rows:
            if not r["boxes"]:
                continue
            row = {
                "id": r["id"], "site": r["site"], "camera": r["camera"],
                "track": r["track"], "confidence": r["confidence"],
                "direction": track_direction(r["boxes"]), "crop": r["crop"],
                "day": r["day"], "window": r["window"], "frames": r["frames"],
                "kind": "model", "species": r.get("species"),
            }
            row.update(_alert_fields(alerts.get(r["id"])))
            rows.append(row)
        model_ids = {r["id"] for r in model_rows}
        for aid, alert in alerts.items():
            if aid in model_ids:
                continue
            rows.append({
                "id": aid, "site": site.name, "camera": alert["camera"],
                "track": alert["track"], "confidence": 1.0, "direction": None, "crop": None,
                "day": alert["day"], "window": alert["window"], "frames": alert["frames"],
                "kind": "manual", "species": alert.get("species"), **_alert_fields(alert)})
    # Newest alert activity first, so a decision that just changed is never pushed out of
    # the cap by folder-name order; rows with no alert fall back to day/window order.
    rows.sort(key=lambda r: (r.get("updated_at") or 0, r["day"], r["window"], str(r["track"])),
              reverse=True)
    return rows[:limit]


def _alert_fields(alert: dict | None) -> dict:
    keys = ("distance_m", "risk", "risk_label", "decision", "decision_label", "actuated",
            "message", "turbine", "first_file", "last_file", "policy_configured", "updated_at")
    if not alert:
        return {k: None for k in keys}
    return {k: alert.get(k) for k in keys}


def _window_started_near(day: str, window: str, ts: float, slack: float = 1800.0) -> bool:
    """Does <camera>-HHMMSS[-n] begin shortly before `ts`? (matches a live event to its window)"""
    m = re.search(r"-(\d{6})(?:-\d+)?$", window)
    if not m:
        return False
    try:
        start = datetime.strptime(f"{day[:10]} {m.group(1)}", "%Y-%m-%d %H%M%S").timestamp()
    except ValueError:
        return False
    return -10.0 <= ts - start <= slack


def live_events(sites: dict[str, "Site"], since: float, limit: int = 40) -> list[dict]:
    """Notifications the acquisition service raised, newest first.

    One row per NEW track on a camera that is enabled or recording (see
    acquisition_service.py), whether or not anything was saved -- so a bird seen while
    recording is off still reaches the bell. Only the tail of each file is read, and the
    result is capped, like every other feed here.
    """
    out: list[dict] = []
    for site in sites.values():
        path = site.out / "live_events.jsonl"
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in tail[-200:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ts", 0) <= since:
                continue
            row["id"] = f"live:{site.name}:{row['camera']}:{row['track']}:{row['ts']:.0f}"
            out.append(row)
    out.sort(key=lambda r: r["ts"], reverse=True)
    out = out[:limit]
    # A recorded event can be opened on the review page once its detections have landed:
    # best-effort match on (camera, track) among the site's still-open or newest windows.
    for site in sites.values():
        for ev in out:                         # saved windows say exactly which track they are
            if ev.get("site") == site.name and ev.get("day") and ev.get("window") and ev.get("file"):
                ev["track_id"] = track_key(ev["day"], ev["window"], ev["track"])
        wanted = [r for r in out if r.get("site") == site.name and r.get("recording") and not r.get("track_id")]
        if not wanted:
            continue
        rows = _tracks_for_site(site)
        for ev in wanted:
            cands = [t for t in rows if t["camera"] == ev["camera"] and t["track"] == ev["track"]
                     and _window_started_near(t["day"], t["window"], ev["ts"])]
            if cands:       # rows are newest-first
                ev["track_id"], ev["day"], ev["window"] = cands[0]["id"], cands[0]["day"], cands[0]["window"]
    return out


def camera_activity(site: "Site", camera: str, limit: int = 20) -> dict:
    """On-demand detail for one camera: recent raw detections plus a
    confirmed-count/species tally -- fetched only when that camera's own
    page is open, not folded into the 3s-polled /api/cameras so that poll
    stays a cheap mtime check regardless of how much detections.jsonl has
    grown.
    """
    rows = [r for r in _tracks_for_site(site) if r["camera"] == camera]
    confirmed = [r for r in rows if r["verdict"] == "keep"]
    species_tally: dict[str, int] = defaultdict(int)
    for r in confirmed:
        if r.get("species"):
            species_tally[r["species"]] += 1
    return {
        "camera": camera,
        "confirmed_count": len(confirmed),
        "species": dict(species_tally),
        "recent": [{
            "id": r["id"], "track": r["track"], "confidence": r["confidence"],
            "direction": track_direction(r["boxes"]), "crop": r["crop"],
            "day": r["day"], "window": r["window"],
            "species": r.get("species"), "verdict": r.get("verdict"),
        } for r in rows[:limit]],
    }


def window_lifecycle(site: "Site", day: str, window: str) -> dict:
    """Where a window stands and when it expires, for the review page and the inbox."""
    if site.layout is None:
        return {"state": "confirmed", "expires_at": None}
    state = site.layout.state(day, window)
    info = {"state": state, "expires_at": None, "protected": False}
    if state == "pending":
        path = site.layout.window_dir(day, window)
        facts = lifecycle.load_facts(site.layout).get(f"{day}/{window}", lifecycle.WindowFacts())
        info["protected"] = bool(facts.hand_work or facts.verdicts or facts.has_meta)
        info["closed"] = (path / "window.json").is_file()
        age = lifecycle._age_days(path, time.time())
        if age is not None and not info["protected"]:
            info["expires_at"] = time.time() + (site.retention.inbox_days - age) * 86400
    return info


def inbox_listing(site: "Site") -> dict:
    """Pending windows with their tracks, oldest expiry first -- what still needs a decision."""
    if site.layout is None:
        return {"windows": [], "retention": None}
    rows = _tracks_for_site(site)
    by_window: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_window[f"{r['day']}/{r['window']}"].append(r)
    facts = lifecycle.load_facts(site.layout)
    now = time.time()
    out = []
    for day, window, path in site.layout.iter_windows(site.layout.inbox):
        wid = f"{day}/{window}"
        f = facts.get(wid, lifecycle.WindowFacts())
        protected = bool(f.hand_work or f.verdicts or f.has_meta)
        age = lifecycle._age_days(path, now)
        tracks = sorted(by_window.get(wid, []), key=lambda r: r["track"])
        out.append({
            "id": wid, "day": day, "window": window,
            "camera": camera_for_window(site, window, day),
            "closed": (path / "window.json").is_file(),
            "tracks": [{"id": t["id"], "track": t["track"], "confidence": t["confidence"],
                        "verdict": t["verdict"], "crop": t["crop"], "frames": t["frames"]} for t in tracks],
            "undecided": sum(1 for t in tracks if not t["verdict"]),
            "protected": protected,
            "expires_at": None if (age is None or protected)
                          else now + (site.retention.inbox_days - age) * 86400,
        })
    out.sort(key=lambda w: (w["expires_at"] is None, w["expires_at"] or 0, w["id"]))
    out = out[:300]                     # the soonest-to-expire first; never an unbounded response
    return {"windows": out, "retention": {"inbox_days": site.retention.inbox_days,
                                          "trash_days": site.retention.trash_days}}


def species_summary(site: Site) -> dict:
    """One card per tagged species, plus the fleet-level numbers Species
    page's header states. Species tagging happens in Review (the optional
    field next to a verdict) -- this is a read-only aggregation over
    whatever has been tagged so far, not a classifier of its own. A
    confirmed sighting with no species tag yet is real data, not an error;
    it is counted in `untagged_count` rather than silently dropped, so the
    header total always equals what Sightings itself shows.
    """
    rows = confirmed_sightings(site)
    tagged = [r for r in rows if r.get("species")]

    by_species: dict[str, list[dict]] = defaultdict(list)
    for row in tagged:
        by_species[row["species"]].append(row)

    species_list = []
    for name, items in by_species.items():
        best = max(items, key=lambda r: r["confidence"])
        species_list.append({
            "name": name,
            "count": len(items),
            "best_crop": best["crop"],
            "best_confidence": best["confidence"],
            "last_seen": max(r["day"] for r in items),
        })
    species_list.sort(key=lambda s: s["count"], reverse=True)

    this_month = date.today().strftime("%Y-%m")
    camera_counts = Counter(r["camera"] for r in rows)
    most_active = None
    if camera_counts:
        camera, count = camera_counts.most_common(1)[0]
        most_active = {"camera": camera, "count": count}

    return {
        "total_species": len(species_list),
        "total_sightings": len(rows),
        "sightings_this_month": sum(1 for r in rows if r["day"].startswith(this_month)),
        "untagged_count": len(rows) - len(tagged),
        "most_active_camera": most_active,
        "species": species_list,
    }


def no_signal_jpeg() -> bytes:
    """The frame shown for a camera whose capture process is not running."""
    canvas = np.zeros((360, 640, 3), dtype=np.uint8)
    canvas[:] = (18, 18, 22)
    cv2.putText(canvas, "no signal", (230, 190), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (110, 110, 120), 2)
    return cv2.imencode(".jpg", canvas)[1].tobytes()


class JsonStore:
    """A small JSON dict on disk, written atomically.

    One reviewer at a time is the assumption: writes are last-writer-wins, with
    the lock only keeping two near-simultaneous saves from the same browser from
    dropping one.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8")) or {}
            except json.JSONDecodeError:
                self.data = {}

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def _flush(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class VerdictStore(JsonStore):
    """verdicts.json -- {track_id: "keep"|"drop"|"unsure"}.

    The value stays a bare string: the training-export tooling reads this file
    with `verdicts.get(id) == "keep"`, so anything richer here would be read as
    "no verdict" by every downstream consumer, silently.
    """

    def set(self, track_id: str, verdict: str) -> None:
        with self.lock:
            self.data[track_id] = verdict
            self._flush()

    def clear(self, track_id: str) -> None:
        with self.lock:
            self.data.pop(track_id, None)
            self._flush()


class TrackMetaStore(JsonStore):
    """track_meta.json -- the optional species/distance/size per track.

    Separate from verdicts.json for the reason above. Keys for tracks that no
    longer exist (a re-run renumbered them) are kept and ignored rather than
    treated as an error.
    """

    FIELDS = ("species", "distance", "size", "risk")

    def update(self, track_id: str, values: dict) -> dict:
        with self.lock:
            entry = dict(self.data.get(track_id) or {})
            for field in self.FIELDS:
                if field not in values:
                    continue
                value = values[field]
                if field == "risk" and value not in (None, "", []) and value not in LEVELS:
                    continue  # an override must be low|medium|high; anything else is ignored
                if value in (None, "", []):
                    entry.pop(field, None)
                else:
                    entry[field] = value
            if entry:
                self.data[track_id] = entry
            else:
                self.data.pop(track_id, None)
            self._flush()
            return entry

    def species_seen(self) -> list[str]:
        with self.lock:
            return sorted({str(e["species"]) for e in self.data.values()
                           if isinstance(e, dict) and e.get("species")})


class AlertStore(JsonStore):
    """alerts.json -- {track_id: alert}: the decision taken for each track that has a risk.

    The decision is written down here, not carried out: `actuated` is always False
    (see risk.py). An alert is created the first time a track has a distance, and
    updated in place when its risk-relevant facts change, keeping `created_at` and a
    short `history` of earlier decisions. Polling an unchanged track writes nothing.
    """

    TRACKED = ("risk", "distance_m", "species", "camera", "first_file", "last_file",
               "frames", "turbine")

    def sync(self, site_name: str, policy: RiskPolicy, candidates: list[dict]) -> list[dict]:
        out, changed = [], False
        now = time.time()
        with self.lock:
            for c in candidates:
                level = policy.classify(c["distance"], c.get("risk"))
                if level is None:
                    continue
                files = sorted(c["files"], key=natural_key)
                fresh = {
                    "id": c["id"], "kind": c["kind"], "site": site_name, "camera": c["camera"],
                    "day": c["day"], "window": c["window"], "track": c["track"],
                    "species": c.get("species") or None,
                    "distance_m": float(c["distance"]) if c.get("distance") not in (None, "") else None,
                    "risk": level, "risk_label": level.capitalize(),
                    **decision_for(level),
                    "turbine": policy.turbine, "policy_configured": policy.configured,
                    "first_file": files[0], "last_file": files[-1], "frames": len(set(files)),
                }
                fresh["message"] = alert_message(
                    site_name, c["camera"], fresh["species"],
                    fresh["distance_m"] if fresh["distance_m"] is not None else 0,
                    policy.turbine, level) if fresh["distance_m"] is not None else (
                    f"{site_name}, camera {c['camera']} detected "
                    f"{'a ' + fresh['species'] if fresh['species'] else 'a bird'} "
                    f"near {policy.turbine} ({level.capitalize()} risk, set by reviewer)")
                old = self.data.get(c["id"])
                if old is None:
                    fresh.update(created_at=now, updated_at=now, history=[])
                    self.data[c["id"]] = fresh
                    changed = True
                elif any(old.get(k) != fresh.get(k) for k in self.TRACKED):
                    history = list(old.get("history", []))
                    if old.get("risk") != fresh["risk"]:
                        history.append({"at": old.get("updated_at"), "risk": old.get("risk"),
                                        "decision": old.get("decision"),
                                        "distance_m": old.get("distance_m")})
                    fresh.update(created_at=old.get("created_at", now), updated_at=now,
                                 history=history[-20:])
                    self.data[c["id"]] = fresh
                    changed = True
                out.append(self.data[c["id"]])
            if changed:
                self._flush()
            return json.loads(json.dumps(out))


class FolderStatusStore(JsonStore):
    """folder_status.json -- {"<day>/<window>": {"reviewed": bool, "at": ts}}.

    A tag, not a move: the frames stay exactly where they are, so every path
    already recorded in detections.jsonl keeps resolving.
    """

    def set_reviewed(self, folder_id: str, reviewed: bool) -> dict:
        with self.lock:
            if reviewed:
                entry = {"reviewed": True, "at": time.time()}
                self.data[folder_id] = entry
            else:
                self.data.pop(folder_id, None)
                entry = {"reviewed": False}
            self._flush()
            return entry


class ManualBoxStore:
    """One row per hand-drawn box: something a reviewer added because the model
    missed it, kept out of detections.jsonl, which is the model's own output and
    not this tool's to rewrite.

    Each box also gets a zoomed crop rendered for it, so a manual addition is
    reviewable as an image like every model detection, not just four numbers.
    The optional species/distance/size ride along on the row; the tooling that
    reads this file only looks at day/window/file/bbox, so extra keys are safe.
    """

    def __init__(self, path: Path, window_root, crops_root: Path):
        self.path = path
        self.window_root = window_root      # (day, window) -> the root that holds it now
        self.crops_root = crops_root
        self.lock = threading.Lock()
        self.rows: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self.rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a row half-written when a previous run was killed
        self._next_id = len(self.rows)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.rows)

    def _rewrite(self) -> None:
        """The file is one row per box, not an event log.

        It used to be append-only, which was fine when a box could only be
        created. Now that a box can be moved or removed, an appended correction
        would leave the old position in the file too -- and the exporter reads
        every row as a positive, so a bird moved once would be taught twice, at
        two different places.
        """
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, row_id: str, bbox: list[float]) -> dict | None:
        with self.lock:
            row = next((r for r in self.rows if r["id"] == row_id), None)
            if row is None:
                return None
            row["bbox"] = bbox
            row["moved_at"] = time.time()
            frame = cv2.imread(str(self.window_root(row["day"], row["window"]) / row["day"] / row["window"] / row["file"]))
            if frame is not None and row.get("crop"):
                # the crop is a zoom on the old position; re-render it or the
                # strip would keep showing where the box used to be
                save_crop(frame, bbox, None, self.crops_root / row["crop"])
            self._rewrite()
            return row

    def delete(self, row_id: str) -> bool:
        with self.lock:
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["id"] != row_id]
            if len(self.rows) == before:
                return False
            self._rewrite()
            return True

    def add(self, day: str, window: str, file: str, bbox: list[float], meta: dict) -> dict | None:
        # The frame this box belongs to is named by the request, and is joined
        # onto two roots below -- one read, one written. Anything that is not a
        # plain folder/file name is refused rather than sanitised.
        if safe_folder(day, window) is None or safe_segment(file) is None:
            return None
        with self.lock:
            row_id = f"m{self._next_id:06d}"
            self._next_id += 1

            crop_rel = None
            frame = cv2.imread(str(self.window_root(day, window) / day / window / file))
            if frame is not None:
                crop_path = (self.crops_root / "crops" / day / window /
                             f"{Path(file).stem}-{row_id}.jpg")
                if save_crop(frame, bbox, None, crop_path):
                    crop_rel = str(crop_path.relative_to(self.crops_root))

            row = {"id": row_id, "day": day, "window": window, "file": file,
                   "bbox": bbox, "crop": crop_rel, "added_at": time.time()}
            # boxes following one bird across frames share a track, so the
            # exporter and the reviewer can both see them as one animal
            if meta.get("track"):
                row["track"] = str(meta["track"])
            for field in TrackMetaStore.FIELDS:
                if meta.get(field) not in (None, ""):
                    row[field] = meta[field]
            self.rows.append(row)
            self._rewrite()
            return row

    def next_track(self) -> str:
        with self.lock:
            used = {r["track"] for r in self.rows if r.get("track")}
            n = 1
            while f"mt{n:04d}" in used:
                n += 1
            return f"mt{n:04d}"


class BoxEditStore:
    """box_edits.jsonl -- corrections to the MODEL's own boxes.

    WHY NOT JUST EDIT detections.jsonl:
        That file is the detector's output. Rewriting it would destroy the
        record of what the model actually produced, which is the thing every
        later comparison of one model against another is measured against. So a
        correction lives beside it and is applied on the way out; the original
        stays exactly as the model wrote it.

    A row is keyed by the one box it corrects -- day/window/file#track -- and
    either moves it or removes it. Removing one box is a different statement
    from a "drop" verdict: the verdict judges the whole track, this says the
    box on THIS frame is wrong.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.edits: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.edits[row["target"]] = row

    @staticmethod
    def key(day: str, window: str, file: str, track: int) -> str:
        return f"{day}/{window}/{file}#{track}"

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.edits))

    def set(self, day: str, window: str, file: str, track: int,
            bbox: list[float] | None, deleted: bool) -> dict:
        with self.lock:
            target = self.key(day, window, file, track)
            row = {"target": target, "day": day, "window": window, "file": file,
                   "track": track, "deleted": bool(deleted), "at": time.time()}
            if bbox is not None:
                row["bbox"] = bbox
            self.edits[target] = row
            self._flush()
            return row

    def clear(self, day: str, window: str, file: str, track: int) -> None:
        with self.lock:
            self.edits.pop(self.key(day, window, file, track), None)
            self._flush()

    def apply(self, day: str, window: str, file: str, boxes: list[dict]) -> list[dict]:
        """The model's boxes for one frame, with the reviewer's corrections."""
        with self.lock:
            if not self.edits:
                return boxes
            out = []
            for b in boxes:
                edit = self.edits.get(self.key(day, window, file, b["track"]))
                if edit is None:
                    out.append(b)
                    continue
                if edit.get("deleted"):
                    continue
                out.append({**b, "bbox": edit.get("bbox", b["bbox"]), "edited": True})
            return out

    def _flush(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in self.edits.values()),
                       encoding="utf-8")
        tmp.replace(self.path)


class DetectionStore:
    """detections.jsonl, re-read from disk on demand rather than cached once.

    live_capture.py keeps appending to this file while a review session is open,
    and a folder being reviewed as it fills is exactly the case that must work --
    so every folder listing refreshes it, and the page re-fetches the current
    frame's boxes on each poll.
    """

    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        self.lock = threading.Lock()
        self.boxes_by_file: dict[tuple[str, str, str], list[dict]] = {}
        self.by_folder: dict[str, list[dict]] = {}
        self.refresh()

    def refresh(self) -> None:
        entries = []
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a row half-written by a live capture

        boxes_by_file: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        by_folder: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            day, window, file = e.get("day"), e.get("window"), e.get("file")
            if not (day and window and file):
                continue
            box = {"bbox": e["bbox"], "conf": e["conf"], "track": e["track"],
                   "file": file, "carried": bool(e.get("carried")),
                   "crop": e.get("crop")}
            boxes_by_file[(day, window, file)].append(box)
            by_folder[f"{day}/{window}"].append(box)

        with self.lock:
            self.boxes_by_file = dict(boxes_by_file)
            self.by_folder = dict(by_folder)

    def boxes_for(self, key: tuple[str, str, str]) -> list[dict]:
        with self.lock:
            return self.boxes_by_file.get(key, [])

    def folder_boxes(self, folder_id: str) -> list[dict]:
        with self.lock:
            return self.by_folder.get(folder_id, [])

    def snapshot_by_folder(self) -> dict[str, list[dict]]:
        with self.lock:
            return dict(self.by_folder)


class FrameSizeCache:
    """Native pixel size per session folder.

    The box overlay is drawn in native frame coordinates while the scrubber
    usually displays a downscaled proxy, so the page needs the native size even
    when it never loads a native frame. Every frame in one window comes from the
    same stream, so one read per folder answers it -- cached because a folder
    that is still recording gets re-polled every few seconds.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.sizes: dict[str, tuple[int, int]] = {}

    def get(self, frames_root: Path, day: str, window: str, first_frame: str | None):
        if safe_folder(day, window) is None or safe_segment(first_frame or "") is None:
            return None
        folder_id = f"{day}/{window}"
        with self.lock:
            if folder_id in self.sizes:
                return self.sizes[folder_id]
        if not first_frame:
            return None
        image = cv2.imread(str(frames_root / day / window / first_frame))
        if image is None:
            return None
        size = (int(image.shape[1]), int(image.shape[0]))
        with self.lock:
            self.sizes[folder_id] = size
        return size


def list_folders(roots: "list[tuple[Path, str]]") -> list[dict]:
    """Every capture session on disk, newest first, with whether it is still
    being written and where it stands (`pending` in the inbox, `confirmed` in frames)."""
    out = []
    now = time.time()
    for frames_root, state in roots:
        if not frames_root.is_dir():
            continue
        for day_dir in sorted((p for p in frames_root.iterdir() if p.is_dir()), reverse=True):
            for window_dir in sorted((p for p in day_dir.iterdir() if p.is_dir()), reverse=True):
                frames = [p for p in window_dir.iterdir()
                          if p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")]
                if not frames:
                    continue
                marker = window_dir / "window.json"
                closed = None
                if marker.exists():
                    try:
                        closed = json.loads(marker.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        closed = {}
                newest = max(p.stat().st_mtime for p in frames)
                match = WINDOW_NAME.match(window_dir.name)
                out.append({
                    "id": f"{day_dir.name}/{window_dir.name}",
                    "day": day_dir.name,
                    "window": window_dir.name,
                    "state": state,
                    "camera": (closed or {}).get("camera") or (match.group("camera") if match else None),
                    "frames": len(frames),
                    # No marker and something written very recently: still recording.
                    # No marker and nothing written for a while: an older session
                    # from before this app wrote markers -- finished.
                    "recording": closed is None and (now - newest) < RECORDING_GRACE_SECONDS,
                    "mtime": newest,
                })
    out.sort(key=lambda f: (f["day"], f["window"]), reverse=True)
    return out


def folder_frames(frames_root: Path, day: str, window: str) -> list[str]:
    if safe_folder(day, window) is None:
        return []
    folder = frames_root / day / window
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir()
                   if p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")),
                  key=natural_key)


ARCHIVE_LIST_TTL_S = 15.0
ARCHIVE_INTERVAL_S = 60.0       # how often archive.py looks for finished data to move


class Site:
    """Everything scoped to one config: its cameras, its data tree, its stores.

    One of these per --config given on the command line. Review's session
    folders, verdicts and every other on-disk store live entirely under this
    site's own <sites-root>/<site>/ -- two sites never share a file.
    """

    def __init__(self, config_path: str, model_bucket: str, sites_root: Path | None,
                 dir_override: Path | None, frames_root_override: Path | None,
                 archive_root: Path | None = None):
        self.config_path = config_path
        self.cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        self.name = self.cfg.get("site", "site")
        # Kept so the capture processes this app spawns on demand (see
        # capture_manager.py) are told the same --sites-root/--bucket this Site
        # itself resolved its paths from -- otherwise a capture process quietly
        # writes under its own default location while Review looks at this one.
        self.sites_root = sites_root
        self.model_bucket = model_bucket

        # A config with no usable camera list still serves Review: going through
        # already-captured frames is a job of its own, often on a machine that
        # can reach none of the cameras.
        try:
            self.cameras = build_cameras(self.cfg)
        except (ValueError, KeyError) as exc:
            self.cameras = []
            print(f"[{self.name}] no cameras in this config ({exc}); "
                  f"Live will show none for this site")

        self.out = dir_override or (site_dir(self.cfg, "dataset", "detections",
                                             base=sites_root, source=config_path) / model_bucket)
        self.out.mkdir(parents=True, exist_ok=True)
        # Confirmed windows live in frames/, pending ones in inbox/ (docs/DATA_LAYOUT.md). An explicit
        # --frames-root reviews some other tree as-is: no inbox, no lifecycle.
        self.layout = None if (frames_root_override or dir_override) else SiteLayout(
            site_root(self.cfg, sites_root, config_path),
            archive=(archive_root / self.name) if archive_root else None)
        self._archive_rows: list[dict] | None = None      # the archive's session list, refreshed in the background
        self._archive_rows_at = 0.0
        self._archive_refreshing = threading.Lock()
        if self.layout:
            self.layout.ensure()
            self.frames_root = self.layout.frames
            self.local_roots = [(self.layout.inbox, "pending"), (self.layout.frames, "confirmed")]
            self.folder_roots = self.local_roots + (
                [(self.layout.archive_frames, "confirmed")] if self.layout.archive_frames else [])
        else:
            self.frames_root = frames_root_override
            self.local_roots = self.folder_roots = [(frames_root_override, "confirmed")]
        self.retention = Retention.from_cfg(self.cfg)
        self.lifecycle_lock = threading.RLock()     # one decision/move/purge pass at a time per site
        self.live_root = site_dir(self.cfg, "live", base=sites_root, source=config_path)
        # Optional: a site whose tunnel is worth watching separately
        # (Babadag's WireGuard link, per link_watch.py) names its CSV here.
        # Absent for a site with no tunnel to watch (Corbu) -- purely opt-in,
        # zero effect on any site that doesn't set it.
        link_watch_csv = self.cfg.get("link_watch_csv")
        self.link_watch_path = (
            (Path(config_path).parent / link_watch_csv).resolve()
            if link_watch_csv else None)

        self.store = DetectionStore(self.out / "detections.jsonl")
        self.verdicts = VerdictStore(self.out / "verdicts.json")
        self.track_meta = TrackMetaStore(self.out / "track_meta.json")
        self.risk = RiskPolicy.from_cfg(self.cfg)
        if not self.risk.configured:
            print(f"[{self.name}] no risk: zones in the config -- using the PLACEHOLDER "
                  f"{self.risk.high_below_m:g} m / {self.risk.medium_below_m:g} m "
                  f"(set risk.high_below_m and risk.medium_below_m)")
        self.alerts = AlertStore(self.out / "alerts.json")
        self.folder_status = FolderStatusStore(self.out / "folder_status.json")
        self.manual_boxes = ManualBoxStore(self.out / "manual_boxes.jsonl",
                                          self.window_root, self.out)
        self.box_edits = BoxEditStore(self.out / "box_edits.jsonl")
        self.frame_sizes = FrameSizeCache()

    def list_folders(self) -> list[dict]:
        """Every session: the local ones fresh, the archive's from a list refreshed in the background
        (walking the NAS costs ~5 ms per window; a Review poll must not wait for that)."""
        rows = list_folders(self.local_roots)
        if self.layout is not None and self.layout.archive_frames:
            if self._archive_rows is None:
                self._refresh_archive_rows()                       # the first call has nothing to show yet
            elif time.monotonic() - self._archive_rows_at > ARCHIVE_LIST_TTL_S:
                threading.Thread(target=self._refresh_archive_rows, daemon=True).start()
            have = {r["id"] for r in rows}
            rows += [r for r in (self._archive_rows or []) if r["id"] not in have]
            rows.sort(key=lambda f: (f["day"], f["window"]), reverse=True)
        return rows

    def _refresh_archive_rows(self) -> None:
        if not self._archive_refreshing.acquire(blocking=False):
            return
        try:
            self._archive_rows = list_folders([(self.layout.archive_frames, "confirmed")])
        except OSError:
            self._archive_rows = self._archive_rows or []          # NAS down: show what we last saw
        finally:
            self._archive_rows_at = time.monotonic()
            self._archive_refreshing.release()

    def window_root(self, day: str, window: str) -> Path:
        """The root that holds <day>/<window> right now (inbox, confirmed, else the archive)."""
        for root, _ in self.folder_roots:
            if (root / day / window).is_dir():
                return root
        return self.frames_root

    def reconcile(self, by: str = "system", only: str | None = None) -> list[dict]:
        """Apply the lifecycle rules (confirm / reject / expire / purge). No-op without a layout.
        `only` ("day/window") limits the check of confirmed windows to the one just decided."""
        if self.layout is None:
            return []
        with self.lifecycle_lock:
            done = lifecycle.reconcile(self.layout, self.retention, self.out, by=by, only=only)
            done += lifecycle.purge(self.layout, self.out)
        if done:
            self.store.refresh()
        return [d for d in done if d.get("to") or d.get("error")]

    def recording_flag(self, camera: str) -> Path:
        return self.live_root / camera / "recording"

    def is_recording(self, camera: str) -> bool:
        return self.recording_flag(camera).exists()

    def set_recording(self, camera: str, enabled: bool) -> None:
        flag = self.recording_flag(camera)
        if enabled:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        else:
            flag.unlink(missing_ok=True)


def make_handler(sites: dict[str, Site], camera_site: dict[str, Site], default_site: str,
                 manager: CaptureManager, proxy_width: int,
                 fps_cap: float, dataset: DatasetIndex | None,
                 users: auth.UserStore, sessions: auth.SessionStore,
                 cameras_by_name: dict[str, Camera],
                 settings: SettingsStore | None = None, runtime: dict | None = None):
    no_signal = no_signal_jpeg()
    min_interval = 1.0 / max(0.5, fps_cap)

    def resolve_site(name: str | None) -> Site | None:
        return sites.get(name or default_site)

    def settings_state() -> dict:
        """Everything the Settings page and the first-run setup show, in one read."""
        now = settings.get()
        models = discover_models(settings.models_dir)
        chosen = resolve_model(now["model"], settings.models_dir)
        config_weights = (runtime or {}).get("config_weights")
        if chosen:
            source, effective = "settings", chosen
        elif config_weights:
            source, effective = "config", {"weights": config_weights, "file": Path(config_weights).name}
        else:
            source, effective = None, None
        active_dir = (runtime or {}).get("data_dir")
        wanted_dir = now["data_dir"]
        source_dir = (runtime or {}).get("data_dir_source")
        return {
            "settings": now,
            "setup_needed": not now["setup_done"],
            "models_dir": str(settings.models_dir),
            "models": models,
            "model_missing": bool(now["model"]) and chosen is None,   # chosen file was deleted
            "effective_model": dict(effective, source=source) if effective else None,
            "config_model": config_weights,
            "data_dir": {"active": active_dir, "source": source_dir,
                         "restart_needed": source_dir != "command line"
                                           and (wanted_dir or None) != (active_dir or None),
                         "overridden": bool(wanted_dir) and source_dir == "command line"},
            "sites": list(sites),
            "platform": "windows" if os.name == "nt" else "linux",
        }

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "bird-review/1.0"

        def log_message(self, fmt, *a):  # keep the terminal for real problems
            pass

        def _send(self, code: int, body: bytes, content_type: str,
                 extra_headers: list[tuple[str, str]] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra_headers or ():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the reviewer scrubbed on before this image arrived

        def _json(self, code: int, obj, extra_headers: list[tuple[str, str]] | None = None) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json", extra_headers)

        def _session(self) -> dict | None:
            token = auth.parse_cookie(self.headers.get("Cookie"), auth.SESSION_COOKIE)
            session = sessions.get(token)
            if session is not None:
                manager.touch()          # a signed-in page is open: Acquisition is not abandoned
            return session

        def _set_session_cookie(self, token: str) -> list[tuple[str, str]]:
            # No `Secure` attribute: this server binds to 127.0.0.1 and is
            # normally reached over a plain-HTTP SSH/VS Code port-forward, not
            # TLS -- `Secure` would silently stop the browser from ever
            # sending the cookie back. If this is ever put behind a real
            # hostname, put it behind a TLS-terminating reverse proxy and add
            # `Secure` here at the same time.
            cookie = (f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; "
                     f"SameSite=Lax; Max-Age={auth.SESSION_TTL_SECONDS}")
            return [("Set-Cookie", cookie)]

        def _clear_session_cookie(self) -> list[tuple[str, str]]:
            return [("Set-Cookie", f"{auth.SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0")]

        def _site_or_404(self, name: str | None) -> Site | None:
            site = resolve_site(name)
            if site is None:
                self._json(404, {"error": f"unknown site {name!r}"})
            return site

        def _under(self, root: Path, rel: str) -> Path | None:
            """`rel` resolved under `root`, or None if it tries to climb out.

            The `..` collapsing is lexical, deliberately: resolving symlinks
            instead would reject a frames tree whose day folders are symlinks
            into a NAS mount or a moved dataset, which is a normal way for this
            data to be stored. Traversal is still blocked, because normpath
            removes `..` segments before anything is opened.
            """
            root_norm = Path(os.path.normpath(root))
            full = Path(os.path.normpath(root_norm / rel))
            if full != root_norm and root_norm not in full.parents:
                return None
            return full

        def _serve_file(self, root: Path, rel: str) -> None:
            full = self._under(root, rel)
            if full is None or not full.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
            self._send(200, full.read_bytes(), ctype)

        def _serve_app_or_login(self, path: str) -> None:
            """Serves the client SPA's built bundle for /login and every /app/*
            path. Anything that isn't a real file on disk (i.e. every client-side
            route -- /app/live, /app/sightings, ...) falls back to index.html;
            the SPA's own router decides what the path means, not this server.
            """
            if not CLIENT_DIST.is_dir():
                self._send(503, b"client app not built -- run `npm install && npm run "
                                b"build` in bird-review-app/client/", "text/plain")
                return
            if path in ("/login", "/app", "/app/"):
                rel = "index.html"
            elif path.startswith("/app/"):
                rel = path[len("/app/"):]
            else:
                rel = "index.html"
            candidate = self._under(CLIENT_DIST, rel)
            if candidate is not None and candidate.is_file():
                ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                self._send(200, candidate.read_bytes(), ctype)
                return
            index = CLIENT_DIST / "index.html"
            if index.is_file():
                self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def _serve_site_file(self, prefix: str, path: str, pick_root) -> None:
            """Routes shaped /prefix/<site>/<rest...> -- a site name, then a path
            resolved against whichever root `pick_root(site)` names."""
            rest = path[len(prefix):]
            site_name, sep, rel = rest.partition("/")
            if not sep:
                self._send(404, b"not found", "text/plain")
                return
            site = resolve_site(unquote(site_name))
            if site is None:
                self._send(404, b"unknown site", "text/plain")
                return
            self._serve_file(pick_root(site, rel), rel)

        def _serve_proxy(self, path: str) -> None:
            """A width-limited JPEG of a native frame, rendered once and cached.

            Scrubbing pulls a new frame on every keypress, and a native 4K frame
            is several MB -- unusable over the SSH port-forward this tool is
            normally reached through. The full frame is still served, by /frame/,
            when the reviewer zooms or draws a box.
            """
            rest = path[len("/proxy/"):]
            site_name, sep, rel = rest.partition("/")
            if not sep:
                self._send(404, b"not found", "text/plain")
                return
            site = resolve_site(unquote(site_name))
            if site is None:
                self._send(404, b"unknown site", "text/plain")
                return
            _day, _, _rest = rel.partition("/")
            _window = _rest.partition("/")[0]
            source = self._under(site.window_root(_day, _window), rel)
            if source is None or not source.is_file():
                self._send(404, b"not found", "text/plain")
                return
            cached = (site.out / "proxies" / Path(rel)).with_suffix(".jpg")
            if not cached.is_file() or cached.stat().st_mtime < source.stat().st_mtime:
                image = cv2.imread(str(source))
                if image is None:
                    self._send(404, b"unreadable frame", "text/plain")
                    return
                if not write_jpeg(cached, fit_width(image, proxy_width), 85):
                    self._send(500, b"cannot write proxy", "text/plain")
                    return
            self._send(200, cached.read_bytes(), "image/jpeg")

        def _serve_negative(self, path: str) -> None:
            """/negative/<site>/<day>/<file> (full frame) or /negthumb/... (width-limited, cached)."""
            thumb = path.startswith("/negthumb/")
            rest = path[len("/negthumb/" if thumb else "/negative/"):]
            site_name, _, rel = rest.partition("/")
            site = resolve_site(unquote(site_name))
            if site is None or site.layout is None:
                self._send(404, b"unknown site", "text/plain")
                return
            day, _, name = rel.partition("/")
            found = site.layout.negative_file(day, name) if safe_folder(day, name.rsplit(".", 1)[0]) else None
            if found is None:
                self._send(404, b"not found", "text/plain")
                return
            source = found
            if not thumb:
                self._send(200, found.read_bytes(), "image/jpeg")
                return
            cached = site.out / "proxies" / "negatives" / Path(rel)
            if not cached.is_file() or cached.stat().st_mtime < source.stat().st_mtime:
                image = cv2.imread(str(source))
                if image is None or not write_jpeg(cached, fit_width(image, 480), 80):
                    self._send(404, b"unreadable frame", "text/plain")
                    return
            self._send(200, cached.read_bytes(), "image/jpeg")

        def _thumb(self, camera: str) -> None:
            """The camera's last-saved frame, as a plain still image.

            Deliberately does NOT call manager.acquire() -- unlike /stream/,
            requesting this must never start a capture process. It exists so
            a grid of many cameras (a site's mast layout, a "watch all" view)
            can show *something* for every tile without opening a live
            connection to every camera just because its tile is on screen;
            only /stream/ -- an explicit "watch this one" action -- does that.
            latest.jpg is written by whatever capture process happens to
            already be running for another reason (a real viewer elsewhere,
            or recording); if none is, this is stale or missing, which is
            the honest answer, not something to paper over.
            """
            site = camera_site.get(camera)
            if site is None:
                self._send(404, b"unknown camera", "text/plain")
                return
            latest = site.live_root / camera / "latest.jpg"
            if latest.is_file():
                try:
                    self._send(200, latest.read_bytes(), "image/jpeg")
                    return
                except OSError:
                    pass
            self._send(200, no_signal, "image/jpeg")

        def _stream(self, camera: str) -> None:
            """One long-lived multipart response, a JPEG part per new frame.

            The open connection itself is what tells the capture manager a
            viewer is here: acquire() before the first byte goes out (so a
            camera with no process yet gets one started before anything is
            promised to the browser), release() no matter how the loop ends,
            so a closed tab always gives its viewer slot back.

            No Content-Length on the outer response -- it never ends. Each part
            is flushed immediately, because the socket's write buffer would
            otherwise hold frames back and the stream would move in lurches.
            A disconnect only ever shows up as a write failure, so that is what
            ends the loop.
            """
            site = camera_site[camera]
            error = manager.acquire(camera)
            if error:
                # acquire() already incremented the viewer count before
                # reporting the refusal -- release() must still run, or a
                # refused attempt (no weights, VRAM) leaks a viewer forever,
                # inflating every later capacity/running check for this
                # camera even after the real reason gets fixed.
                manager.release(camera)
                self._send(503, error.encode("utf-8"), "text/plain")
                return
            try:
                latest = site.live_root / camera / "latest.jpg"
                self.send_response(200)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                self.end_headers()

                def part(payload: bytes) -> None:
                    self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
                    self.wfile.write(payload)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

                last_sent = 0.0
                last_write = time.monotonic()
                try:
                    while True:
                        # No process yet (just starting), or one that has never
                        # written a frame, never updates this file -- without a
                        # keepalive the loop would never write anything, and a
                        # write is the only way a closed connection is noticed.
                        # The thread and its socket would then leak, one per viewer.
                        if time.monotonic() - last_write > KEEPALIVE_SECONDS:
                            part(no_signal)
                            last_write = time.monotonic()
                        try:
                            mtime = latest.stat().st_mtime
                        except FileNotFoundError:
                            time.sleep(0.5)
                            continue
                        if mtime <= last_sent:
                            time.sleep(0.05)
                            continue
                        try:
                            payload = latest.read_bytes()
                        except OSError:
                            time.sleep(0.05)
                            continue
                        if not payload:
                            time.sleep(0.05)
                            continue
                        last_sent = mtime
                        part(payload)
                        last_write = time.monotonic()
                        time.sleep(min_interval)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the tab was closed or the camera unchecked
            finally:
                manager.release(camera)

        def _stream_multi(self, cameras: list[str]) -> None:
            """Every requested camera over ONE connection.

            Why: a browser allows ~6 simultaneous connections per host, and an MJPEG
            <img> never finishes, so 12 tiles = 12 permanently-open connections. The
            ones beyond the sixth never got a frame, and every /api call the page
            makes queued behind them -- the UI froze. One long-lived response for
            the whole wall leaves the other connections free for the API.

            Wire format, repeated forever (the page demultiplexes with fetch +
            ReadableStream and draws each JPEG on its tile's canvas):
                uint16 BE  index into the `cams=` list
                uint32 BE  JPEG byte length
                bytes      the JPEG
            Same lifecycle as /stream/<cam>.mjpg: every camera is acquire()d before
            the first byte and release()d however the loop ends, and a camera that
            has not produced a frame for KEEPALIVE_SECONDS gets the placeholder, so
            a closed tab is still noticed by a failing write.
            """
            acquired: list[str] = []
            try:
                for name in cameras:
                    error = manager.acquire(name)
                    if error:
                        # One camera that cannot start (no weights, VRAM) must not take
                        # the whole wall down: it just shows the placeholder, and the
                        # status pill from /api/cameras says why. acquire() counted the
                        # viewer even though it refused, so give it back right away.
                        manager.release(name)
                    else:
                        acquired.append(name)
                self.send_response(200)
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                latest = [camera_site[n].live_root / n / "latest.jpg" for n in cameras]
                last_sent = [0.0] * len(cameras)
                last_write = [time.monotonic()] * len(cameras)
                next_due = [0.0] * len(cameras)
                try:
                    while True:
                        now = time.monotonic()
                        wrote = False
                        for i, path in enumerate(latest):
                            if now < next_due[i]:
                                continue
                            payload = None
                            try:
                                mtime = path.stat().st_mtime
                                if mtime > last_sent[i]:
                                    payload = path.read_bytes() or None
                                    if payload:
                                        last_sent[i] = mtime
                            except OSError:
                                pass
                            if payload is None and now - last_write[i] > KEEPALIVE_SECONDS:
                                payload = no_signal
                            if payload is None:
                                continue
                            self.wfile.write(struct.pack(">HI", i, len(payload)) + payload)
                            last_write[i] = now
                            next_due[i] = now + min_interval
                            wrote = True
                        if wrote:
                            self.wfile.flush()
                        else:
                            time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                for name in acquired:
                    manager.release(name)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = unquote(url.path)
            query = parse_qs(url.query)
            site = resolve_site(query.get("site", [None])[0])

            if path == "/favicon.ico":
                self._send(404, b"", "image/x-icon")
                return

            if path.startswith(PUBLIC_GET_PREFIXES):
                self._serve_app_or_login(path)
                return

            if path == "/api/whoami":
                session = self._session()
                self._json(200, {"username": session["username"], "role": session["role"]}
                          if session else {"username": None, "role": None})
                return

            if path == "/desktop-login":
                token = query.get("t", [""])[0]
                if DESKTOP_TOKEN and secrets.compare_digest(token, DESKTOP_TOKEN):
                    self._send(302, b"", "text/plain", [
                        ("Location", "/app/live"),
                        *self._set_session_cookie(sessions.create("desktop", "operator"))])
                else:
                    self._send(403, b"forbidden", "text/plain")
                return

            session = self._session()
            if session is None:
                if path.startswith("/api/"):
                    self._json(401, {"error": "authentication required"})
                else:
                    self._send(302, b"", "text/plain",
                              [("Location", f"/login?next={quote(path)}")])
                return

            # M4b: Review, Dataset and Live are now the React app's own SPA
            # routes (/app/review, /app/dataset, /app/live) -- the legacy
            # static pages that used to live here are retired. Old bookmarks
            # to any of these paths still land somewhere, rather than falling
            # through to a bare 404.
            if path in ("/", "/index.html", "/review"):
                self._send(302, b"", "text/plain", [("Location", "/app/review")])
                return
            elif path in ("/live", "/live.html"):
                self._send(302, b"", "text/plain", [("Location", "/app/live")])
                return
            elif path in ("/dataset", "/dataset.html"):
                self._send(302, b"", "text/plain", [("Location", "/app/dataset")])
                return

            # ---- training set review (not site-scoped: one dataset, --dataset)
            elif path == "/api/dataset/summary":
                self._json(200, dataset.summary() if dataset else {"dataset": None})
            elif path == "/api/dataset/items":
                if not dataset:
                    self._json(404, {"error": "no dataset configured"})
                    return
                self._json(200, dataset.page(
                    query.get("category", ["all"])[0],
                    query.get("split", ["all"])[0],
                    int(query.get("offset", ["0"])[0]),
                    min(200, int(query.get("limit", ["48"])[0])),
                    query.get("hide_done", ["0"])[0] == "1"))
            elif path.startswith("/dsthumb/"):
                payload = dataset.thumb(unquote(path[len("/dsthumb/"):])) if dataset else None
                self._send(200, payload, "image/jpeg") if payload else \
                    self._send(404, b"not found", "text/plain")
            elif path.startswith("/dstile/"):
                payload = dataset.full_tile(unquote(path[len("/dstile/"):])) if dataset else None
                self._send(200, payload, "image/jpeg") if payload else \
                    self._send(404, b"not found", "text/plain")

            # ---- live view, across every site
            elif path == "/api/cameras":
                now = time.time()
                out = []
                for name, cam_site in camera_site.items():
                    latest = cam_site.live_root / name / "latest.jpg"
                    mtime = latest.stat().st_mtime if latest.exists() else None
                    connected_at = None
                    status_path = cam_site.live_root / name / "status.json"
                    if status_path.is_file():
                        try:
                            connected_at = json.loads(
                                status_path.read_text(encoding="utf-8")).get("connected_at")
                        except (json.JSONDecodeError, OSError):
                            connected_at = None
                    stable = connected_at is not None and (now - connected_at) >= STABLE_AFTER_SECONDS
                    # Phase 0: the true capture-to-now clock, distinct from
                    # `age` (an mtime-based proxy that already has
                    # decode/inference/encode baked in) -- PERFORMANCE.md §2
                    # says never blend the two into one number, so both are
                    # kept, separately named.
                    capture_ts = None
                    frame_ts_path = cam_site.live_root / name / "frame_ts.json"
                    if frame_ts_path.is_file():
                        try:
                            capture_ts = json.loads(
                                frame_ts_path.read_text(encoding="utf-8")).get("capture_ts")
                        except (json.JSONDecodeError, OSError):
                            capture_ts = None
                    row = {"name": name, "site": cam_site.name, "mtime": mtime,
                          "live": mtime is not None and (now - mtime) < STALE_AFTER,
                          "stable": stable,
                          "age": None if mtime is None else round(now - mtime, 1),
                          "capture_ts": capture_ts,
                          "capture_age": None if capture_ts is None else round(now - capture_ts, 1),
                          "recording": cam_site.is_recording(name),
                          "pair": cameras_by_name[name].pair if name in cameras_by_name else None}
                    row.update(manager.status(name))
                    out.append(row)
                self._json(200, out)
            elif path == "/stream/multi":
                cams = [c for c in query.get("cams", [""])[0].split(",") if c]
                unknown = [c for c in cams if c not in camera_site]
                if not cams or unknown or len(set(cams)) != len(cams):
                    self._send(404, f"unknown or empty camera list {unknown}".encode(), "text/plain")
                    return
                self._stream_multi(cams)
            elif path.startswith("/stream/") and path.endswith(".mjpg"):
                camera = path[len("/stream/"):-len(".mjpg")]
                if camera not in camera_site:
                    self._send(404, b"unknown camera", "text/plain")
                    return
                self._stream(camera)
            elif path.startswith("/thumb/") and path.endswith(".jpg"):
                camera = path[len("/thumb/"):-len(".jpg")]
                self._thumb(camera)
            elif path == "/api/capacity":
                site = resolve_site(query.get("site", [None])[0])
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                names = query.get("cameras", [None])[0]
                selected = ([cameras_by_name[n] for n in names.split(",") if n in cameras_by_name]
                           if names else [c for c in site.cameras])
                idle, peak = total_demand(selected)
                self._json(200, {
                    "cameras": len(selected),
                    "idle_demand": round(idle, 2),
                    "peak_demand": round(peak, 2),
                    "gpu_fps": manager.gpu_fps,
                    "warning": budget_warning(selected, manager.gpu_fps),
                    "vram_free_mib": gpu_free_mib(),
                })
            elif path == "/api/link_watch":
                site = resolve_site(query.get("site", [None])[0])
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                if site.link_watch_path is None:
                    self._json(200, {"available": False})
                    return
                self._json(200, {"available": True, **summarize_link_watch(site.link_watch_path)})

            # ---- review, one site at a time
            elif path == "/api/folders":
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                site.store.refresh()
                status = site.folder_status.snapshot()
                folders = site.list_folders()
                verdict_map = site.verdicts.snapshot()
                for f in folders:
                    boxes = site.store.folder_boxes(f["id"])
                    tracks = {b["track"] for b in boxes}
                    undecided = [t for t in tracks
                                 if not verdict_map.get(track_key(f["day"], f["window"], t))]
                    f["detections"] = len(boxes)
                    f["tracks"] = len(tracks)
                    f["undecided"] = len(undecided)
                    f["reviewed"] = bool((status.get(f["id"]) or {}).get("reviewed"))
                self._json(200, {"folders": folders, "species": site.track_meta.species_seen()})
            elif path == "/api/folder":
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                site.store.refresh()
                folder_id = query.get("id", [""])[0]
                day, _, window = folder_id.partition("/")
                frames = folder_frames(site.window_root(day, window), day, window)
                index_of = {name: i for i, name in enumerate(frames)}
                boxes = site.store.folder_boxes(folder_id)
                per_frame = defaultdict(list)
                for b in boxes:
                    per_frame[b["file"]].append(b)
                verdict_map = site.verdicts.snapshot()
                # One entry per detection, not per frame it appears on: a box is
                # carried across the frames between inferences, and offering the
                # same bird a dozen times would make the queue mostly repeats.
                # The frame kept is the one where it looked weakest, which is
                # where a reviewer most needs to start looking.
                best: dict[int, dict] = {}
                for b in boxes:
                    key = track_key(day, window, b["track"])
                    if verdict_map.get(key) or b["file"] not in index_of:
                        continue
                    current = best.get(b["track"])
                    if current is None or b["conf"] < current["conf"]:
                        best[b["track"]] = {"index": index_of[b["file"]], "track": b["track"],
                                            "conf": b["conf"], "id": key}
                uncertain = sorted(best.values(), key=lambda u: u["conf"])
                # Where each track was, frame by frame: the trail drawn behind a bird.
                # A carried box is a copy of the previous position, not a new fix.
                paths: dict[int, list] = defaultdict(list)
                for b in boxes:
                    if b.get("carried") or b["file"] not in index_of:
                        continue
                    x0, y0, x1, y1 = b["bbox"]
                    paths[b["track"]].append([index_of[b["file"]],
                                              round((x0 + x1) / 2, 1), round((y0 + y1) / 2, 1)])
                paths = {t: sorted(p) for t, p in paths.items() if len(p) >= 2}
                size = site.frame_sizes.get(site.window_root(day, window), day, window,
                                            frames[0] if frames else None)
                self._json(200, {
                    "id": folder_id, "day": day, "window": window,
                    "width": size[0] if size else None,
                    "height": size[1] if size else None,
                    "frames": [{"file": name, "boxes": len(per_frame.get(name, []))}
                               for name in frames],
                    "uncertain": uncertain,
                    "paths": paths,
                })
            elif path == "/api/boxes":
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                day = query.get("day", [""])[0]
                window = query.get("window", [""])[0]
                file = query.get("file", [""])[0]
                self._json(200, site.box_edits.apply(
                    day, window, file, site.store.boxes_for((day, window, file))))
            elif path == "/api/verdicts":
                self._json(200, site.verdicts.snapshot() if site else {})
            elif path == "/api/track_meta":
                self._json(200, site.track_meta.snapshot() if site else {})
            elif path == "/api/manual_boxes":
                self._json(200, site.manual_boxes.snapshot() if site else [])
            elif path == "/api/sightings":
                # Read-only, deliberately: this is a display over Review's
                # own confirmed verdicts, not a second workflow. There is no
                # POST counterpart -- confirming a sighting still happens in
                # Review, by the operator role, the same as it always has.
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                all_rows = confirmed_sightings(site)
                rows = all_rows
                camera_filter = query.get("camera", [None])[0]
                species_filter = query.get("species", [None])[0]
                day_filter = query.get("day", [None])[0]
                if camera_filter:
                    rows = [r for r in rows if r["camera"] == camera_filter]
                if species_filter:
                    rows = [r for r in rows if r["species"] == species_filter]
                if day_filter:
                    rows = [r for r in rows if r["day"] == day_filter]
                try:
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    limit = min(120, max(1, int(query.get("limit", ["40"])[0])))
                except ValueError:
                    self._json(400, {"error": "offset/limit must be integers"})
                    return
                self._json(200, {
                    "total": len(rows),
                    "sightings": rows[offset:offset + limit],
                    "cameras": sorted({r["camera"] for r in all_rows}),
                    "species": site.track_meta.species_seen(),
                    "days": sorted({r["day"] for r in all_rows}, reverse=True),
                })
            elif path == "/api/species":
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                self._json(200, species_summary(site))
            elif path == "/api/recent_detections":
                # Global, like /api/cameras -- the notification feed watches
                # every site the session can see, not just whichever one is
                # currently selected in the UI.
                self._json(200, recent_detections(sites))
            elif path == "/api/acquisition":
                self._json(200, manager.acquisition_status())
            elif path.startswith("/api/captured/"):
                # Operator-only: it lists and (via POST) deletes footage.
                if session["role"] != "operator":
                    self._json(403, {"error": "operator role required"})
                elif site is None or site.layout is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                elif path == "/api/captured/summary":
                    self._json(200, captured.summary(site.layout))
                elif path == "/api/captured/items":
                    group = query.get("group", ["unreviewed"])[0]
                    if group not in captured.GROUPS:
                        self._json(400, {"error": f"group must be one of {', '.join(captured.GROUPS)}"})
                        return
                    rows = captured.items(site.layout, group)
                    offset = max(0, int(query.get("offset", ["0"])[0] or 0))
                    limit = min(200, max(1, int(query.get("limit", ["48"])[0] or 48)))
                    page = rows[offset:offset + limit]
                    for row in page:
                        if row["kind"] == "negative":
                            row["thumb"] = f"/negthumb/{quote(site.name)}/{row['day']}/{row['file']}"
                        else:
                            n = max(0, int(row["frames"] or 1) // 2)
                            row["thumb"] = f"/proxy/{quote(site.name)}/{row['day']}/{row['window']}/frame_{n}.jpg"
                    self._json(200, {"total": len(rows), "items": page})
                else:
                    self._json(404, {"error": "not found"})
            elif path.startswith("/negthumb/") or path.startswith("/negative/"):
                self._serve_negative(path)
            elif path == "/api/storage":
                # Any signed-in user: the banner is for whoever is looking at the screen. Reads the mover's
                # status file only; the request never touches the NAS.
                rt = runtime or {}
                if not rt.get("archive_dir"):
                    self._json(200, {"state": "local"})
                else:
                    st = archive.read_status(rt.get("archive_status"))
                    fresh = st and time.time() - st.get("last_cycle", 0) < 4 * rt.get("archive_interval", 60)
                    self._json(200, dict(st, archive=rt["archive_dir"]) if fresh
                               else {"state": "unknown", "archive": rt["archive_dir"]})
            elif path.startswith("/api/captured/"):
                # Operator-only: it lists and (via POST) deletes footage.
                if session["role"] != "operator":
                    self._json(403, {"error": "operator role required"})
                elif site is None or site.layout is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                elif path == "/api/captured/summary":
                    self._json(200, captured.summary(site.layout))
                elif path == "/api/captured/items":
                    group = query.get("group", ["unreviewed"])[0]
                    if group not in captured.GROUPS:
                        self._json(400, {"error": f"group must be one of {', '.join(captured.GROUPS)}"})
                        return
                    rows = captured.items(site.layout, group)
                    offset = max(0, int(query.get("offset", ["0"])[0] or 0))
                    limit = min(200, max(1, int(query.get("limit", ["48"])[0] or 48)))
                    page = rows[offset:offset + limit]
                    for row in page:
                        if row["kind"] == "negative":
                            row["thumb"] = f"/negthumb/{quote(site.name)}/{row['day']}/{row['file']}"
                        else:
                            n = max(0, int(row["frames"] or 1) // 2)
                            row["thumb"] = f"/proxy/{quote(site.name)}/{row['day']}/{row['window']}/frame_{n}.jpg"
                    self._json(200, {"total": len(rows), "items": page})
                else:
                    self._json(404, {"error": "not found"})
            elif path.startswith("/negthumb/") or path.startswith("/negative/"):
                self._serve_negative(path)
            elif path == "/api/storage":
                # Any signed-in user: the banner is for whoever is looking at the screen.
                rt = runtime or {}
                self._json(200, storage_status(Path(rt["primary_dir"]) if rt.get("primary_dir") else None,
                                               Path(rt.get("data_dir") or DEFAULT_SITES_ROOT)))
            elif path in ("/api/settings", "/api/devices", "/api/settings/check_dir"):
                # Operator-only even to read: it names folders on this machine.
                if session["role"] != "operator":
                    self._json(403, {"error": "operator role required"})
                elif settings is None:
                    self._json(404, {"error": "settings are not enabled"})
                elif path == "/api/settings":
                    self._json(200, settings_state())
                elif path == "/api/devices":
                    self._json(200, detect_devices(refresh=query.get("refresh", ["0"])[0] == "1"))
                else:
                    self._json(200, check_data_dir(query.get("path", [""])[0], list(sites)))
            elif path == "/api/live_events":
                try:
                    since = float(query.get("since", ["0"])[0])
                except ValueError:
                    since = 0.0
                self._json(200, live_events(sites, since))
            elif path == "/api/alerts":
                # Every site's alerts with the decision taken, newest first.
                alerts = []
                for st in sites.values():
                    alerts.extend(alerts_for_site(st))
                alerts.sort(key=lambda a: a.get("created_at", 0), reverse=True)
                self._json(200, alerts[:200])
            elif path == "/api/risk_policy":
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                self._json(200, site.risk.describe())
            elif path == "/api/camera_activity":
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                camera = query.get("camera", [None])[0]
                if not camera:
                    self._json(400, {"error": "camera is required"})
                    return
                self._json(200, camera_activity(site, camera))
            elif path == "/api/track":
                # Backs the tracking page a notification opens: one track's
                # full frame-ordered box history, read-only.
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                track_id = query.get("id", [None])[0]
                row = next((r for r in _tracks_for_site(site) if r["id"] == track_id), None)
                if row is None:
                    self._json(404, {"error": f"unknown track {track_id!r}"})
                    return
                self._json(200, {**row, "direction": track_direction(row["boxes"]),
                                 # every frame of the window, so the page also shows the seconds around the bird
                                 "files": folder_frames(site.window_root(row["day"], row["window"]),
                                                        row["day"], row["window"]),
                                 "lifecycle": window_lifecycle(site, row["day"], row["window"])})
            elif path == "/api/inbox":
                # ?site=* : every site, each window tagged with its site (the nav badge uses this)
                if query.get("site", [""])[0] == "*":
                    windows = [{**w, "site": st.name} for st in sites.values() for w in inbox_listing(st)["windows"]]
                    windows.sort(key=lambda w: (w["expires_at"] is None, w["expires_at"] or 0, w["id"]))
                    self._json(200, {"windows": windows})
                    return
                if site is None:
                    self._json(404, {"error": f"unknown site {query.get('site')!r}"})
                    return
                self._json(200, inbox_listing(site))
            elif path.startswith("/proxy/"):
                self._serve_proxy(path)
            elif path.startswith("/frame/"):
                self._serve_site_file("/frame/", path, lambda s, rel: s.window_root(*(rel.split("/") + ["", ""])[:2]))
            elif path.startswith("/img/"):
                self._serve_site_file("/img/", path, lambda s, rel: s.out)
            else:
                self._send(404, b"not found", "text/plain")

        def _same_origin(self) -> bool:
            """Reject a POST that some other web page made on the reviewer's behalf.

            These are all simple cross-origin requests -- no preflight protects
            them -- so any site open in the same browser could otherwise rewrite
            verdicts.json through this server. A browser always sends Origin on a
            cross-origin POST; curl and the page's own fetches send either the
            server's own origin or none.
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlparse(origin).netloc == self.headers.get("Host")

        def _restore_window(self, site: "Site", day: str, window: str, who: str) -> None:
            """Trash -> inbox with a fresh clock. Clears its drop verdicts through the app's own store (the
            file alone would be overwritten by that store's in-memory copy). Caller holds site.lifecycle_lock."""
            lifecycle.restore(site.layout, day, window, site.out, by=who, clear_verdicts=False)
            for tid, verdict in list(site.verdicts.snapshot().items()):
                if tid.startswith(f"{day}/{window}/") and verdict == "drop":
                    site.verdicts.clear(tid)
            site.store.refresh()

        def do_POST(self) -> None:
            if not self._same_origin():
                self._json(403, {"error": "cross-origin request refused"})
                return
            path = unquote(urlparse(self.path).path)
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return

            if path == "/api/login":
                username, password = payload.get("username"), payload.get("password")
                role = users.check(username or "", password or "") if username and password else None
                if role is None:
                    self._json(401, {"error": "invalid username or password"})
                    return
                token = sessions.create(username, role)
                self._json(200, {"ok": True, "username": username, "role": role},
                          self._set_session_cookie(token))
                return

            if path == "/api/logout":
                sessions.destroy(auth.parse_cookie(self.headers.get("Cookie"), auth.SESSION_COOKIE))
                self._json(200, {"ok": True}, self._clear_session_cookie())
                return

            # Every remaining POST endpoint mutates something -- recording
            # state, a verdict, a box, a label decision -- so one check here
            # covers every route below and every route added to this dispatcher
            # later: a client-role session cannot reach any of them, regardless
            # of what the page in front of them does or doesn't render.
            session = self._session()
            if session is None:
                self._json(401, {"error": "authentication required"})
                return
            if session["role"] != "operator":
                self._json(403, {"error": "read-only session -- operator role required"})
                return

            site = (resolve_site(payload.get("site"))
                    if path not in ("/api/recording", "/api/acquisition", "/api/settings") else None)

            if path == "/api/acquisition":
                # The header's enable button. It starts/stops acquisition for every camera and
                # nothing else: no recording flag is read or written here.
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    self._json(400, {"error": "expected {enabled: bool}"})
                    return
                error = manager.set_enabled(enabled)
                if error:
                    self._json(502, {"error": error})
                    return
                self._json(200, manager.acquisition_status())
                return

            if path == "/api/settings":
                if settings is None:
                    self._json(404, {"error": "settings are not enabled"})
                    return
                clean, errors = settings.validate(payload)
                if errors:
                    self._json(400, {"error": "; ".join(errors.values()), "fields": errors})
                    return
                settings.update(clean)
                apply_error = None
                if "model" in clean:
                    now = settings.get()
                    apply_error = manager.reconfigure(resolve_model(now["model"], settings.models_dir), "auto")
                state = settings_state()
                if apply_error:
                    state["apply_error"] = apply_error
                self._json(200, state)
                return

            if path == "/api/recording":
                cam_name, enabled = payload.get("name"), payload.get("enabled")
                if not cam_name or not isinstance(enabled, bool):
                    self._json(400, {"error": "expected {name, enabled: bool}"})
                    return
                cam_site = camera_site.get(cam_name)
                if cam_site is None:
                    self._json(404, {"error": f"unknown camera {cam_name!r}"})
                    return
                cam_site.set_recording(cam_name, enabled)
                error = manager.note_recording(cam_name, enabled)
                if error and enabled:
                    # The flag is already written -- a real capture process
                    # will pick it up the moment one starts -- but the click
                    # that was supposed to start one right now failed, and the
                    # person clicking needs to know why rather than just
                    # seeing "no signal" forever.
                    cam_site.set_recording(cam_name, False)
                    self._json(502, {"error": error})
                    return
                self._json(200, {"ok": True, "name": cam_name, "recording": enabled})
                return

            if site is None:
                self._json(404, {"error": f"unknown site {payload.get('site')!r}"})
                return

            if path == "/api/verdict":
                track_id, verdict = payload.get("id"), payload.get("verdict")
                if not track_id or verdict not in ("keep", "drop", "unsure", "clear"):
                    self._json(400, {"error": "expected {id, verdict: keep|drop|unsure|clear}"})
                    return
                if verdict == "clear":
                    site.verdicts.clear(track_id)
                else:
                    site.verdicts.set(track_id, verdict)
                self._json(200, {"ok": True})

            elif path == "/api/review":
                # The review page's one action: a verdict on a track, why, and what became of its window.
                track_id, decision = payload.get("id"), payload.get("decision")
                m = lifecycle.TRACK_ID.match(track_id or "")
                if not m or decision not in ("keep", "drop", "unsure"):
                    self._json(400, {"error": "expected {id: day/window/tNNNN, decision: keep|drop|unsure, reason?, note?}"})
                    return
                day, window = m["day"], m["window"]
                if safe_folder(day, window) is None:
                    self._json(400, {"error": "bad track id"})
                    return
                who = (self._session() or {}).get("username") or "operator"
                with site.lifecycle_lock:
                    if site.layout is not None:
                        state = site.layout.state(day, window)
                        if state is None:
                            self._json(404, {"error": "that window no longer exists"})
                            return
                        if state == "trashed":
                            # A decision on a rejected window (the page was left open): bring it back first,
                            # or the verdict would be saved on a window that is still purged on schedule.
                            self._restore_window(site, day, window, who)
                    site.verdicts.set(track_id, decision)
                    if site.layout is not None:
                        from layout import log_event
                        log_event(site.out, event="review", day=day, window=window, track=track_id,
                                  decision=decision, reason=str(payload.get("reason") or "")[:80],
                                  note=str(payload.get("note") or "")[:500], by=who)
                    moved = [a for a in site.reconcile(by=who, only=f"{day}/{window}") if a.get("day") == day and a.get("window") == window]
                self._json(200, {"ok": True, "verdict": decision, "moved": moved,
                                 "lifecycle": window_lifecycle(site, day, window)})

            elif path in ("/api/captured/delete", "/api/captured/to_review"):
                if site.layout is None:
                    self._json(404, {"error": "this site has no layout"})
                    return
                who = (self._session() or {}).get("username") or "operator"
                group, ids = payload.get("group"), payload.get("ids")
                if group not in captured.GROUPS or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    self._json(400, {"error": "expected {site, group, ids: [..]}"})
                    return
                with site.lifecycle_lock:
                    if path == "/api/captured/to_review":
                        if group != "no_detections" or len(ids) != 1:
                            self._json(400, {"error": "only a single negative can be sent to review"})
                            return
                        try:
                            wid = captured.negative_to_review(site.layout, ids[0], site.out, who)
                        except FileNotFoundError as exc:
                            self._json(404, {"error": str(exc)})
                            return
                        site.store.refresh()
                        self._json(200, {"ok": True, "window": wid})
                        return
                    result = (captured.delete_negatives(site.layout, ids) if group == "no_detections"
                              else captured.delete_windows(site.layout, ids, site.out, who))
                    if result["deleted"] and group != "no_detections":
                        site.store.refresh()
                self._json(200, {"ok": True, **result})

            elif path == "/api/review/undo":
                # Take back a rejection: the window returns to the inbox, its `drop` verdicts are cleared.
                day, window = payload.get("day"), payload.get("window")
                if site.layout is None or safe_folder(day or "", window or "") is None:
                    self._json(400, {"error": "expected {site, day, window}"})
                    return
                with site.lifecycle_lock:
                    if site.layout.state(day, window) != "trashed":
                        self._json(409, {"error": "that window is not in the trash"})
                        return
                    self._restore_window(site, day, window,
                                         (self._session() or {}).get("username") or "operator")
                self._json(200, {"ok": True, "lifecycle": window_lifecycle(site, day, window)})

            elif path == "/api/track_meta":
                track_id = payload.get("id")
                if not track_id:
                    self._json(400, {"error": "expected {id, species?, distance?, size?}"})
                    return
                entry = site.track_meta.update(track_id, payload)
                self._json(200, {"ok": True, "meta": entry,
                                 "species": site.track_meta.species_seen()})

            elif path == "/api/manual_box":
                day, window = payload.get("day"), payload.get("window")
                file, bbox = payload.get("file"), payload.get("bbox")
                if not (day and window and file and isinstance(bbox, list) and len(bbox) == 4):
                    self._json(400, {"error": "expected {day, window, file, bbox:[x0,y0,x1,y1]}"})
                    return
                row = site.manual_boxes.add(day, window, file, [float(v) for v in bbox], payload)
                if row is None:
                    self._json(400, {"error": "day/window/file must be plain names"})
                    return
                self._json(200, row)

            elif path == "/api/dataset/mark":
                if not dataset:
                    self._json(404, {"error": "no dataset configured"})
                    return
                if isinstance(payload.get("ids"), list):
                    n = dataset.mark_many(payload["ids"], bool(payload.get("bad")))
                    self._json(200, {"ok": True, "marked": n})
                    return
                label_id, bad = payload.get("id"), payload.get("bad")
                if not label_id or not isinstance(bad, bool):
                    self._json(400, {"error": "expected {id, bad: bool} or {ids: [...], bad}"})
                    return
                self._json(200, {"ok": True,
                                 "decision": dataset.mark(label_id, bad, payload.get("note"))})

            elif path == "/api/manual_box/update":
                row_id, bbox = payload.get("id"), payload.get("bbox")
                if not row_id or not isinstance(bbox, list) or len(bbox) != 4:
                    self._json(400, {"error": "expected {id, bbox:[x0,y0,x1,y1]}"})
                    return
                row = site.manual_boxes.update(row_id, [float(v) for v in bbox])
                if row is None:
                    self._json(404, {"error": "no such box"})
                    return
                self._json(200, row)

            elif path == "/api/manual_box/delete":
                row_id = payload.get("id")
                if not row_id:
                    self._json(400, {"error": "expected {id}"})
                    return
                self._json(200, {"ok": site.manual_boxes.delete(row_id)})

            elif path == "/api/manual_track":
                self._json(200, {"track": site.manual_boxes.next_track()})

            elif path == "/api/box_edit":
                day, window = payload.get("day"), payload.get("window")
                file, track = payload.get("file"), payload.get("track")
                if not (day and window and file) or not isinstance(track, int):
                    self._json(400, {"error": "expected {day, window, file, track, bbox?|deleted?}"})
                    return
                if safe_folder(day, window) is None or safe_segment(file) is None:
                    self._json(400, {"error": "day/window/file must be plain names"})
                    return
                if payload.get("reset"):
                    site.box_edits.clear(day, window, file, track)
                    self._json(200, {"ok": True, "reset": True})
                    return
                bbox = payload.get("bbox")
                if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4):
                    self._json(400, {"error": "bbox must be [x0,y0,x1,y1]"})
                    return
                self._json(200, site.box_edits.set(
                    day, window, file, track,
                    [float(v) for v in bbox] if bbox else None,
                    bool(payload.get("deleted"))))

            elif path == "/api/folder_status":
                folder_id, reviewed = payload.get("id"), payload.get("reviewed")
                if not folder_id or not isinstance(reviewed, bool):
                    self._json(400, {"error": "expected {id, reviewed: bool}"})
                    return
                # A window still being written cannot be "done": its remaining
                # frames have not been looked at yet, by definition.
                if reviewed:
                    current = next((f for f in site.list_folders()
                                    if f["id"] == folder_id), None)
                    if current and current["recording"]:
                        self._json(409, {"error": "this session is still recording"})
                        return
                self._json(200, {"ok": True,
                                 "status": site.folder_status.set_reviewed(folder_id, reviewed)})

            else:
                self._json(404, {"error": "not found"})

    return Handler


def main(argv: list[str] | None = None, on_ready=None) -> int:
    """`argv` / `on_ready(httpd, manager)` exist for the desktop launcher, which runs
    this in a thread and needs to stop it again when its window closes."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, action="append",
                    help="a site's config file; pass more than once for several sites "
                         "(e.g. --config config.corbu.yaml --config config.yaml)")
    ap.add_argument("--model", default="live",
                    help="reads <sites-root>/<site>/dataset/detections/<model>/ for every "
                         "site (default: live)")
    ap.add_argument("--dir", type=Path, default=None,
                    help="override the detections directory entirely (only with one --config)")
    ap.add_argument("--frames-root", type=Path, default=None,
                    help="default: <sites-root>/<site>/frames (only with one --config)")
    ap.add_argument("--proxy-width", type=int, default=1600,
                    help="longest edge of the downscaled frames the scrubber pulls "
                         "(default 1600; the native frame is still used for zoom "
                         "and for drawing boxes)")
    ap.add_argument("--dataset", type=Path, default=None,
                    help="a YOLO data.yaml to review the training labels of "
                         "(enables the Dataset view)")
    ap.add_argument("--fps-cap", type=float, default=10.0,
                    help="most frames per second to push down one live stream (default 10)")
    ap.add_argument("--weights", default=None,
                    help="finetuned YOLO weights, used for any site whose config has no "
                         "model.weights of its own -- needed before Live can start a "
                         "camera's capture process on demand")
    ap.add_argument("--gpu-fps", type=float, default=MEASURED_GPU_FPS,
                    help=f"tiled inferences per second this GPU can sustain, for the "
                         f"soft capacity warning when starting one more camera on demand "
                         f"(default {MEASURED_GPU_FPS:g}, measured at FP16 on 4K frames)")
    ap.add_argument("--capture-logs", type=Path, default=ROOT / "logs",
                    help=f"where on-demand capture processes log to (default: {ROOT / 'logs'})")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="127.0.0.1 by default; forward the port rather than widening this")
    ap.add_argument("--users", type=Path, default=ROOT / "users.yaml",
                    help="users file (default: users.yaml at the repo root) -- "
                         "see users.example.yaml; every page and API now requires a "
                         "login, and every mutating API requires the operator role")
    ap.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH,
                    help="settings.yaml: the model, CPU/GPU and data folder chosen in the app "
                         f"(default: {DEFAULT_SETTINGS_PATH}); absent = nothing chosen")
    ap.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR,
                    help=f"where model files (.pt YOLO, .pth RF-DETR) are picked from "
                         f"(default: {DEFAULT_MODELS_DIR})")
    add_sites_root_argument(ap)
    ap.add_argument("--archive-root", type=Path, default=None,
                    help="the NAS folder (holding <site>/ trees) finished data is moved to; overrides the "
                         "data folder chosen in Settings")
    args = ap.parse_args(argv)

    settings = SettingsStore(args.settings, args.models_dir)
    chosen = settings.get()
    # --sites-root on the command line wins; otherwise the folder picked in Settings; otherwise
    # the default. Resolved here, once: every Site and the acquisition service are built from it.
    # --sites-root (default <app>/sites) is the LOCAL tree capture and review work on. The data folder from
    # Settings, or --archive-root, is the ARCHIVE (the NAS): archive.py moves finished data there.
    if args.archive_root:
        archive_dir, data_dir_source = args.archive_root, "command line"
    elif chosen["data_dir"]:
        archive_dir, data_dir_source = Path(chosen["data_dir"]), "settings"
    else:
        archive_dir, data_dir_source = None, "default"

    if len(args.config) > 1 and (args.dir or args.frames_root):
        print("--dir/--frames-root only make sense with a single --config; ignoring them",
              file=sys.stderr)
        args.dir = args.frames_root = None

    sites: dict[str, Site] = {}
    for config_path in args.config:
        site = Site(config_path, args.model, args.sites_root, args.dir, args.frames_root, archive_dir)
        if site.name in sites:
            raise SystemExit(f"two configs both name site {site.name!r} "
                             f"({sites[site.name].config_path} and {config_path}) -- "
                             f"each site needs a distinct `site:` key")
        sites[site.name] = site
    default_site = next(iter(sites))

    # Camera names key /stream/<name>.mjpg and the recording toggle, so one name
    # must not mean two different cameras on two different sites.
    camera_site: dict[str, Site] = {}
    cameras_by_name: dict[str, Camera] = {}
    for site in sites.values():
        for camera in site.cameras:
            if camera.name in camera_site:
                other = camera_site[camera.name].name
                raise SystemExit(f"camera {camera.name!r} is in both {other!r} and "
                                 f"{site.name!r} -- camera names must be unique across "
                                 f"every --config given")
            camera_site[camera.name] = site
            cameras_by_name[camera.name] = camera

    weights_by_site = {}
    for site in sites.values():
        own = (site.cfg.get("model") or {}).get("weights")
        weights_by_site[site.name] = own or args.weights
        if not weights_by_site[site.name] and site.cameras:
            print(f"[{site.name}] no weights configured (neither model.weights in "
                  f"{site.config_path} nor --weights) -- Live cannot start capture for "
                  f"this site's cameras until one is set")

    manager = CaptureManager(args.capture_logs, args.gpu_fps, weights_by_site,
                             idle_stop_s=0)     # once a person starts acquisition it runs until they stop it
    manager.model_choice = resolve_model(chosen["model"], args.models_dir)
    manager.device = "auto"          # GPU when CUDA is usable, otherwise CPU; not a setting
    if chosen["model"] and manager.model_choice is None:
        print(f"settings: model {chosen['model']!r} is not in {args.models_dir} any more -- using the "
              f"configured model instead", file=sys.stderr)
    for site in sites.values():
        for camera in site.cameras:
            manager.register(site, camera)
    manager.start()
    mover = None
    archive_status = args.capture_logs / "archive" / "status.json"
    if archive_dir:
        archive_status.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve().parent / "archive.py"), "--archive-root", str(archive_dir),
               "--status", str(archive_status), "--interval", str(ARCHIVE_INTERVAL_S), "--stop-on-stdin-eof"]
        for config_path in args.config:
            cmd += ["--config", str(config_path)]
        if args.sites_root:
            cmd += ["--sites-root", str(args.sites_root)]
        if getattr(sys, "frozen", False):
            # No interpreter to hand a script to in the packaged exe: the same loop runs as a thread.
            threading.Thread(target=archive.main, daemon=True, name="archive",
                             args=([a for a in cmd[2:] if a != "--stop-on-stdin-eof"],)).start()
            print(f"archive: finished data moves to {archive_dir} in the background")
        else:
            mover = subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=ROOT,
                                     stdout=open(args.capture_logs / "archive" / "archive.log", "ab"),
                                     stderr=subprocess.STDOUT)
            print(f"archive: finished data moves to {archive_dir} in the background (pid {mover.pid})")

    dataset = None
    if args.dataset:
        dataset = DatasetIndex(args.dataset)
        print(f"indexing labels in {dataset.root} ...")
        dataset.build(progress=lambda m: print(m, flush=True))
        s = dataset.summary()
        print(f"  {s['labels']} labels across {s['tiles_with_labels']} labelled tiles "
              f"({s['tiles_empty']} empty), {s['rejected']} already rejected")

    users = auth.UserStore(args.users)
    sessions = auth.SessionStore()
    if not CLIENT_DIST.is_dir():
        print(f"note: {CLIENT_DIST} not built yet -- /login and /app will 503 until "
              f"`npm install && npm run build` has been run in {CLIENT_DIST.parent}")

    runtime = {"data_dir": str(archive_dir) if archive_dir else None, "data_dir_source": data_dir_source,
               "archive_dir": str(archive_dir) if archive_dir else None, "archive_status": archive_status,
               "archive_interval": ARCHIVE_INTERVAL_S,
               "config_weights": next((w for w in weights_by_site.values() if w), None)}
    handler = make_handler(sites, camera_site, default_site, manager,
                           args.proxy_width, args.fps_cap, dataset, users, sessions,
                           cameras_by_name, settings, runtime)

    # allow_reuse_address must be a class attribute: TCPServer.__init__ binds
    # before an instance attribute set afterwards could take effect, so a restart
    # right after a stop would otherwise sit in TIME_WAIT and refuse to rebind.
    class ReusableServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        # An open MJPEG stream is a thread that never returns, so without this
        # the process would refuse to exit until every viewer closed their tab.
        daemon_threads = True

    httpd = ReusableServer((args.bind, args.port), handler)

    # Ctrl-C already raises KeyboardInterrupt on its own; a plain `kill <pid>`
    # (SIGTERM -- what a process manager or a stop script sends) does not,
    # by default, and would otherwise leave every camera process this app
    # started running forever, orphaned, still holding GPU memory and an RTSP
    # connection open with nothing left to ask it to stop.
    def on_term(signum, frame):
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_term)

    for site in sites.values():
        print(f"site {site.name}: {len(site.list_folders())} capture "
              f"session(s), {len(site.cameras)} camera(s) "
              f"({', '.join(c.name for c in site.cameras) or 'none'})")
        print(f"  writes -> {site.out}")
    print(f"\n  review    http://{args.bind}:{args.port}/")
    print(f"  live      http://{args.bind}:{args.port}/live")
    if dataset:
        print(f"  dataset   http://{args.bind}:{args.port}/dataset")
    print()
    print("On a remote machine: forward this port (VS Code's PORTS panel, or "
          "ssh -L) and open that URL locally -- this binds to 127.0.0.1 and is "
          "not otherwise reachable.")
    print("ONE acquisition service (acquisition_service.py) runs every camera in "
          "synchronised rounds with a single model. It runs while acquisition is enabled "
          "(header button), a stream is open, or a camera's recording flag is set. "
          "Recording is off by default for every camera -- switch it on from the Live tab; "
          "enabling acquisition never changes it.")
    def lifecycle_loop():
        # Decide what can be decided (a window capture has since closed, an expiry that came due)
        # and purge trash past its date. Every action is logged to the bucket's lifecycle.jsonl.
        while True:
            for site in sites.values():
                try:
                    for act in site.reconcile(by="system"):
                        print(f"[{site.name}] lifecycle {act}", flush=True)
                except Exception as exc:        # a bad window must not stop the loop
                    print(f"[{site.name}] lifecycle error: {exc}", flush=True)
            time.sleep(600)
    threading.Thread(target=lifecycle_loop, name="lifecycle", daemon=True).start()

    if on_ready:
        on_ready(httpd, manager)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping capture processes this app started...")
    finally:
        manager.shutdown()   # also when the desktop launcher calls httpd.shutdown()
        if mover is not None:
            mover.stdin.close()                       # it exits when its stdin closes
            try:
                mover.wait(timeout=10)
            except subprocess.TimeoutExpired:
                mover.terminate()
    print("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
