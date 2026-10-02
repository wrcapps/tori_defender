#!/usr/bin/env python3
"""Everything captured, grouped for the Dataset page, and deleting from it. See docs/DATA_LAYOUT.md.

GROUPS (what the operator sees)
    unreviewed       windows nobody has judged: no verdict, no drawn box, no species -- is it a true
                     or a false positive? Inbox and confirmed alike. Imported (Excel) windows are not listed.
    with_detections  windows a person has judged or drawn on.
    no_detections    hard negatives: single frames saved while the detector found nothing (negatives/).

DELETING
    Direct and permanent -- the operator picked it in this page. A window loses its folder, its crops and
    proxies in every bucket, and a line in lifecycle.jsonl; a negative loses its file. A window that capture
    is still writing (inbox, no window.json) is never offered or deleted.

SEND TO REVIEW
    A negative where the operator can see a bird becomes a one-frame inbox window (`from_negative`), so it
    shows up in Review where a box can be drawn. It never expires on its own.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import lifecycle
from layout import SiteLayout, log_event

GROUPS = ("unreviewed", "with_detections", "no_detections")
NEGATIVE_NAME = re.compile(r"^(?P<camera>[\w.\-]+?)-(?P<hms>\d{6})(?:-\d+)?\.jpg$")
SAFE = re.compile(r"^[\w.\-]+$")


def negative_path(layout: SiteLayout, camera: str, now: datetime) -> Path:
    """negatives/<day>/<camera>-<HHMMSS>.jpg, with -2, -3... if that second is taken."""
    day = layout.negatives / now.strftime("%Y-%m-%d")
    base = f"{camera}-{now.strftime('%H%M%S')}"
    path, n = day / f"{base}.jpg", 1
    while path.exists():
        n += 1
        path = day / f"{base}-{n}.jpg"
    return path


# ------------------------------------------------------------------------------ listing
def _windows(layout: SiteLayout):
    """(day, window, path, state) for every window the Dataset page may show."""
    for root, state in ((layout.inbox, "pending"), (layout.frames, "confirmed")):
        for day, window, path in layout.iter_windows(root):
            if lifecycle._is_import(path):
                continue
            if state == "pending" and not lifecycle.is_closed(path):
                continue                                   # capture is still writing it
            yield day, window, path, state


def _meta(path: Path) -> dict:
    try:
        return json.loads((path / "window.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _window_group(facts: lifecycle.WindowFacts) -> str:
    judged = bool(facts.verdicts or facts.hand_work or facts.has_meta)
    return "with_detections" if judged else "unreviewed"


def window_items(layout: SiteLayout, group: str) -> list[dict]:
    facts = lifecycle.load_facts(layout)
    out = []
    for day, window, path, state in _windows(layout):
        f = facts.get(f"{day}/{window}", lifecycle.WindowFacts())
        if _window_group(f) != group:
            continue
        meta = _meta(path)
        out.append({"id": f"{day}/{window}", "kind": "window", "day": day, "window": window,
                    "camera": meta.get("camera") or window.rsplit("-", 1)[0], "state": state,
                    "frames": meta.get("frames"), "tracks": len(f.tracks), "judged": len(f.verdicts),
                    "ts": meta.get("opened_at") or meta.get("closed_at")})
    return sorted(out, key=lambda i: (i["day"], i["window"]), reverse=True)


def negative_items(layout: SiteLayout) -> list[dict]:
    out = []
    if layout.negatives.is_dir():
        for day_dir in sorted((p for p in layout.negatives.iterdir() if p.is_dir()), reverse=True):
            for img in sorted(day_dir.glob("*.jpg"), reverse=True):
                m = NEGATIVE_NAME.match(img.name)
                out.append({"id": f"{day_dir.name}/{img.name}", "kind": "negative", "day": day_dir.name,
                            "file": img.name, "camera": m["camera"] if m else "?",
                            "time": f"{m['hms'][:2]}:{m['hms'][2:4]}:{m['hms'][4:]}" if m else ""})
    return out


def items(layout: SiteLayout, group: str) -> list[dict]:
    if group == "no_detections":
        return negative_items(layout)
    if group in GROUPS:
        return window_items(layout, group)
    raise ValueError(f"unknown group {group!r}")


def summary(layout: SiteLayout) -> dict:
    groups = {g: 0 for g in GROUPS}
    facts = lifecycle.load_facts(layout)
    for day, window, _path, _state in _windows(layout):
        groups[_window_group(facts.get(f"{day}/{window}", lifecycle.WindowFacts()))] += 1
    groups["no_detections"] = sum(1 for _ in layout.negatives.glob("*/*.jpg")) if layout.negatives.is_dir() else 0
    return {"groups": groups}


# ---------------------------------------------------------------------------- deleting
def delete_windows(layout: SiteLayout, ids: list[str], log_bucket: Path, by: str) -> dict:
    deleted, skipped = [], []
    allowed = {f"{d}/{w}" for d, w, _p, _s in _windows(layout)}
    for wid in ids:
        day, _, window = str(wid).partition("/")
        if wid not in allowed or not (SAFE.match(day) and SAFE.match(window)):
            skipped.append(wid)
            continue
        path = layout.window_dir(day, window)
        if path is None:
            skipped.append(wid)
            continue
        shutil.rmtree(path, ignore_errors=True)
        for bucket in layout.buckets():
            for sub in ("crops", "proxies"):
                shutil.rmtree(bucket / sub / day / window, ignore_errors=True)
        try:
            path.parent.rmdir()                            # an emptied day folder
        except OSError:
            pass
        log_event(log_bucket, day=day, window=window, to="deleted", reason="dataset", by=by)
        deleted.append(wid)
    return {"deleted": deleted, "skipped": skipped}


def _negative_file(layout: SiteLayout, nid: str) -> Path | None:
    day, _, name = str(nid).partition("/")
    if not (SAFE.match(day) and SAFE.match(name)) or not NEGATIVE_NAME.match(name):
        return None
    path = layout.negatives / day / name
    return path if path.is_file() else None


def delete_negatives(layout: SiteLayout, ids: list[str]) -> dict:
    deleted, skipped = [], []
    for nid in ids:
        path = _negative_file(layout, nid)
        if path is None:
            skipped.append(nid)
            continue
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        deleted.append(nid)
    return {"deleted": deleted, "skipped": skipped}


def negative_to_review(layout: SiteLayout, nid: str, log_bucket: Path, by: str) -> str:
    """Turn a negative into a one-frame inbox window; returns its `day/window` id."""
    src = _negative_file(layout, nid)
    if src is None:
        raise FileNotFoundError(f"{nid} is not a saved negative")
    day = src.parent.name
    camera = NEGATIVE_NAME.match(src.name)["camera"]
    name = layout.unique_window(day, src.stem)
    dest = layout.inbox / day / name
    dest.mkdir(parents=True)
    os.replace(src, dest / "frame_0.jpg")
    now = time.time()
    (dest / "window.json").write_text(json.dumps({
        "camera": camera, "frames": 1, "opened_at": now, "closed_at": now, "from_negative": True}, indent=1),
        encoding="utf-8")
    try:
        src.parent.rmdir()
    except OSError:
        pass
    log_event(log_bucket, day=day, window=name, **{"from": "negative", "to": "pending"}, reason="sent to review", by=by)
    return f"{day}/{name}"
