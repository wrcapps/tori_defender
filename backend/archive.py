#!/usr/bin/env python3
"""Moves finished data from the local (hot) tree to the archive (the NAS), in the background.

    python backend/archive.py --config config.yaml --sites-root sites --archive-root /mnt/nas/BirdReviewApp \\
        [--status logs/archive/status.json] [--interval 60] [--once] [--stop-on-stdin-eof]

WHY
    The NAS reads at ~22 MB/s and writes a frame in ~90 ms; twelve cameras recording would need four
    times that. So capture and review work ONLY on the local disk, and this process, separate from the app,
    carries finished data over whenever the NAS is reachable. If it is not, nothing stops: data waits here.

WHAT MOVES (per site, <archive>/<site>/ mirrors the local tree)
    frames/<day>/<window>      confirmed windows (a person said keep). Copied to a temporary name on the NAS,
                               renamed into place there, checked file by file (name and size), and only then
                               removed locally -- so a crash at any point leaves the window whole in at least
                               one place, and running again finishes the job.
    negatives/<day>/<file>     single frames with no detection, once older than NEGATIVE_AFTER_S (24 h).
    dataset/detections/<bucket>/*.json[l] / *.csv     BACKUP copies of the records, when changed. The records
                               stay local (the app reads and appends to them constantly); crops and proxies
                               stay local too (small, and rebuilt from the frames).

STATUS
    <status>.json: {state: up|down, pending_windows, pending_negatives, moved_windows, moved_negatives,
    last_cycle, last_error}. The app shows it in the header; it never probes the NAS itself.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import yaml

from datastore import usable
from layout import RECORDS, SiteLayout
from sitepaths import add_sites_root_argument, site_root, site_of

NEGATIVE_AFTER_S = 24 * 3600
BACKUP_SUFFIXES = {".json", ".jsonl", ".csv"}


def _tree_files(path: Path) -> dict[str, int]:
    return {str(p.relative_to(path)): p.stat().st_size for p in path.rglob("*") if p.is_file()}


def _remove_empty_parents(path: Path, stop: Path) -> None:
    while path != stop:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def archive_window(src: Path, dest_root: Path) -> bool:
    """Move one window folder src (…/frames/<day>/<window>) under dest_root (<archive>/<site>/frames).
    Returns True when the local copy was removed."""
    day, window = src.parent.name, src.name
    final = dest_root / day / window
    expected = _tree_files(src)
    if not final.is_dir():
        tmp = dest_root / day / f".{window}.partial"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        os.rename(tmp, final)                  # one rename, on the NAS
    if _tree_files(final) != expected:         # name and size, file by file, as read back from the NAS
        raise OSError(f"{day}/{window}: the copy on the archive does not match; kept locally")
    shutil.rmtree(src)
    _remove_empty_parents(src.parent, src.parent.parent)
    return True


def archive_negative(src: Path, dest_root: Path) -> bool:
    final = dest_root / src.parent.name / src.name
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.partial")
    shutil.copyfile(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink()
        raise OSError(f"{src.name}: short copy; kept locally")
    os.replace(tmp, final)
    src.unlink()
    _remove_empty_parents(src.parent, src.parent.parent)
    return True


def backup_records(layout: SiteLayout) -> int:
    """Copy changed record files to <archive>/<site>/dataset/detections/<bucket>/. Returns files copied."""
    copied = 0
    dest_base = layout.archive / RECORDS
    for bucket in layout.buckets():
        for f in bucket.iterdir():
            if not f.is_file() or f.suffix not in BACKUP_SUFFIXES or f.name.startswith("."):
                continue
            dest = dest_base / bucket.name / f.name
            st = f.stat()
            if dest.is_file() and dest.stat().st_size == st.st_size and dest.stat().st_mtime >= st.st_mtime:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.partial")
            shutil.copyfile(f, tmp)
            os.replace(tmp, dest)
            copied += 1
    return copied


def pending(layout: SiteLayout, now: float | None = None) -> dict:
    """What is waiting locally: {windows: [Path], negatives: [Path]}. Local listing only, never the NAS."""
    now = time.time() if now is None else now
    windows = [p for _d, _w, p in layout.iter_windows(layout.frames) if (p / "window.json").is_file()]
    negatives = []
    if layout.negatives.is_dir():
        negatives = [p for p in layout.negatives.glob("*/*.jpg") if now - p.stat().st_mtime > NEGATIVE_AFTER_S]
    return {"windows": windows, "negatives": negatives}


def run_once(layout: SiteLayout, now: float | None = None, log=print) -> dict:
    """One pass over one site. Errors on one item are logged and the pass continues."""
    todo = pending(layout, now)
    stats = {"windows": 0, "negatives": 0, "backed_up": 0, "errors": []}
    for path in todo["windows"]:
        try:
            archive_window(path, layout.archive_frames)
            stats["windows"] += 1
        except OSError as exc:
            stats["errors"].append(f"{path.parent.name}/{path.name}: {exc.strerror or exc}")
    for path in todo["negatives"]:
        try:
            archive_negative(path, layout.archive_negatives)
            stats["negatives"] += 1
        except OSError as exc:
            stats["errors"].append(f"{path.name}: {exc.strerror or exc}")
    try:
        stats["backed_up"] = backup_records(layout)
    except OSError as exc:
        stats["errors"].append(f"records backup: {exc.strerror or exc}")
    if stats["windows"] or stats["negatives"] or stats["errors"]:
        log(f"[archive] moved {stats['windows']} window(s), {stats['negatives']} negative(s); "
            f"records backed up: {stats['backed_up']}; errors: {len(stats['errors'])}")
        for e in stats["errors"][:5]:
            log(f"[archive]   {e}")
    return stats


def read_status(path: Path | None) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


class Mover:
    def __init__(self, layouts: list[SiteLayout], archive_root: Path, status_path: Path | None):
        self.layouts, self.archive_root, self.status_path = layouts, archive_root, status_path
        self.moved_windows = self.moved_negatives = 0
        self.last_error: str | None = None

    def cycle(self) -> dict:
        ok, why = usable(self.archive_root)
        waiting = [pending(lay) for lay in self.layouts]
        if ok:
            self.last_error = None
            for lay in self.layouts:
                stats = run_once(lay)
                self.moved_windows += stats["windows"]
                self.moved_negatives += stats["negatives"]
                if stats["errors"]:
                    self.last_error = stats["errors"][0]
            waiting = [pending(lay) for lay in self.layouts]
        else:
            self.last_error = why
        status = {"state": "up" if ok else "down", "reason": "" if ok else why,
                  "pending_windows": sum(len(w["windows"]) for w in waiting),
                  "pending_negatives": sum(len(w["negatives"]) for w in waiting),
                  "moved_windows": self.moved_windows, "moved_negatives": self.moved_negatives,
                  "last_cycle": time.time(), "last_error": self.last_error, "archive": str(self.archive_root)}
        self._write(status)
        return status

    def _write(self, status: dict) -> None:
        if self.status_path is None:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_name(f".{self.status_path.name}.tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(self.status_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", action="append", required=True, type=Path)
    add_sites_root_argument(ap)
    ap.add_argument("--archive-root", required=True, type=Path, help="the NAS folder that holds <site>/ trees")
    ap.add_argument("--status", type=Path, default=None)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--stop-on-stdin-eof", action="store_true",
                    help="exit when the parent closes our stdin (the app started us and is gone)")
    a = ap.parse_args(argv)

    layouts = []
    for config in a.config:
        cfg = yaml.safe_load(Path(config).read_text(encoding="utf-8")) or {}
        layouts.append(SiteLayout(site_root(cfg, a.sites_root, config), a.archive_root / site_of(cfg, config)))
    mover = Mover(layouts, a.archive_root, a.status)

    stop = threading.Event()
    if a.stop_on_stdin_eof:
        def watch():
            sys.stdin.buffer.read()
            stop.set()
        threading.Thread(target=watch, daemon=True).start()
    while not stop.is_set():
        status = mover.cycle()
        if a.once:
            print(json.dumps(status))
            return 0
        stop.wait(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
