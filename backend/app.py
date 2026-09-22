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
import signal
import socketserver
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import date
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
from link_watch_read import summarize as summarize_link_watch
from model_infer import fit_width, save_crop, write_jpeg
from sitepaths import add_sites_root_argument, site_dir

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLIENT_DIST = ROOT / "client" / "dist"

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


def camera_for_window(site: Site, window: str) -> str:
    """Best-effort camera name for a window folder, for display only.

    live_capture.py names a window "<camera>-<HHMMSS>" once a site has more
    than one camera; older, single-camera runs (Corbu's whole history so
    far) wrote just "<HHMMSS>", carrying no camera name at all. A prefixed
    window's own name is authoritative; an unprefixed one only means
    "unknown" if the site actually has more than one camera to be ambiguous
    between -- with exactly one, there is nothing to guess.
    """
    match = WINDOW_NAME.match(window)
    if match and match.group("camera"):
        return match.group("camera")
    if len(site.cameras) == 1:
        return site.cameras[0].name
    return "unknown"


def confirmed_sightings(site: "Site") -> list[dict]:
    """One row per track with a "keep" verdict, most recent first.

    Reads the same three sources Review itself reads (detections, verdicts,
    track_meta) and does no writing of its own -- a sightings feed is a
    different view of Review's data, not a separate copy of it.
    """
    site.store.refresh()
    verdict_map = site.verdicts.snapshot()
    meta_map = site.track_meta.snapshot()

    rows: list[dict] = []
    for folder_id, boxes in site.store.snapshot_by_folder().items():
        day, _, window = folder_id.partition("/")
        by_track: dict[int, list[dict]] = defaultdict(list)
        for box in boxes:
            by_track[box["track"]].append(box)

        for track, track_boxes in by_track.items():
            key = track_key(day, window, track)
            if verdict_map.get(key) != "keep":
                continue
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
                "camera": camera_for_window(site, window),
                "track": track,
                "confidence": round(best["conf"], 3),
                "frames": len(track_boxes),
                "crop": best.get("crop"),
                "species": meta.get("species"),
                "distance": meta.get("distance"),
                "size": meta.get("size"),
            })

    rows.sort(key=lambda r: (r["day"], r["window"], r["track"]), reverse=True)
    return rows


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

    FIELDS = ("species", "distance", "size")

    def update(self, track_id: str, values: dict) -> dict:
        with self.lock:
            entry = dict(self.data.get(track_id) or {})
            for field in self.FIELDS:
                if field not in values:
                    continue
                value = values[field]
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

    def __init__(self, path: Path, frames_root: Path, crops_root: Path):
        self.path = path
        self.frames_root = frames_root
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
            frame = cv2.imread(str(self.frames_root / row["day"] / row["window"] / row["file"]))
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
            frame = cv2.imread(str(self.frames_root / day / window / file))
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


def list_folders(frames_root: Path) -> list[dict]:
    """Every capture session on disk, newest first, with whether it is still
    being written."""
    out = []
    if not frames_root.is_dir():
        return out
    now = time.time()
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
                "camera": (closed or {}).get("camera") or (match.group("camera") if match else None),
                "frames": len(frames),
                # No marker and something written very recently: still recording.
                # No marker and nothing written for a while: an older session
                # from before this app wrote markers -- finished.
                "recording": closed is None and (now - newest) < RECORDING_GRACE_SECONDS,
                "mtime": newest,
            })
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


class Site:
    """Everything scoped to one config: its cameras, its data tree, its stores.

    One of these per --config given on the command line. Review's session
    folders, verdicts and every other on-disk store live entirely under this
    site's own <sites-root>/<site>/ -- two sites never share a file.
    """

    def __init__(self, config_path: str, model_bucket: str, sites_root: Path | None,
                 dir_override: Path | None, frames_root_override: Path | None):
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
        self.frames_root = frames_root_override or site_dir(
            self.cfg, "frames", base=sites_root, source=config_path)
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
        self.folder_status = FolderStatusStore(self.out / "folder_status.json")
        self.manual_boxes = ManualBoxStore(self.out / "manual_boxes.jsonl",
                                          self.frames_root, self.out)
        self.box_edits = BoxEditStore(self.out / "box_edits.jsonl")
        self.frame_sizes = FrameSizeCache()

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
                 cameras_by_name: dict[str, Camera]):
    no_signal = no_signal_jpeg()
    min_interval = 1.0 / max(0.5, fps_cap)

    def resolve_site(name: str | None) -> Site | None:
        return sites.get(name or default_site)

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
            return sessions.get(token)

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
            self._serve_file(pick_root(site), rel)

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
            source = self._under(site.frames_root, rel)
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
                folders = list_folders(site.frames_root)
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
                frames = folder_frames(site.frames_root, day, window)
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
                size = site.frame_sizes.get(site.frames_root, day, window,
                                            frames[0] if frames else None)
                self._json(200, {
                    "id": folder_id, "day": day, "window": window,
                    "width": size[0] if size else None,
                    "height": size[1] if size else None,
                    "frames": [{"file": name, "boxes": len(per_frame.get(name, []))}
                               for name in frames],
                    "uncertain": uncertain,
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
            elif path.startswith("/proxy/"):
                self._serve_proxy(path)
            elif path.startswith("/frame/"):
                self._serve_site_file("/frame/", path, lambda s: s.frames_root)
            elif path.startswith("/img/"):
                self._serve_site_file("/img/", path, lambda s: s.out)
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

            site = resolve_site(payload.get("site")) if path != "/api/recording" else None

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
                    current = next((f for f in list_folders(site.frames_root)
                                    if f["id"] == folder_id), None)
                    if current and current["recording"]:
                        self._json(409, {"error": "this session is still recording"})
                        return
                self._json(200, {"ok": True,
                                 "status": site.folder_status.set_reviewed(folder_id, reviewed)})

            else:
                self._json(404, {"error": "not found"})

    return Handler


def main() -> int:
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
    add_sites_root_argument(ap)
    args = ap.parse_args()

    if len(args.config) > 1 and (args.dir or args.frames_root):
        print("--dir/--frames-root only make sense with a single --config; ignoring them",
              file=sys.stderr)
        args.dir = args.frames_root = None

    sites: dict[str, Site] = {}
    for config_path in args.config:
        site = Site(config_path, args.model, args.sites_root, args.dir, args.frames_root)
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

    manager = CaptureManager(args.capture_logs, args.gpu_fps, weights_by_site)
    for site in sites.values():
        for camera in site.cameras:
            manager.register(site, camera)
    manager.start()

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

    handler = make_handler(sites, camera_site, default_site, manager,
                           args.proxy_width, args.fps_cap, dataset, users, sessions,
                           cameras_by_name)

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
    signal.signal(signal.SIGTERM, on_term)

    for site in sites.values():
        print(f"site {site.name}: {len(list_folders(site.frames_root))} capture "
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
    print("Picking a camera in Live starts its capture process; it stops the "
          "moment the last viewer leaves, unless recording is on. Recording "
          "is off by default for every camera -- switch it on from the Live tab.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping capture processes this app started...")
        manager.shutdown()
        print("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
