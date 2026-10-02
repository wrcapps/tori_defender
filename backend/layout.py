#!/usr/bin/env python3
"""Where every file of a site lives. The one module that knows the tree; see docs/DATA_LAYOUT.md.

    sites/<site>/live/<camera>/             ephemeral previews
    sites/<site>/inbox/<day>/<window>/      pending detections (capture writes here)
    sites/<site>/frames/<day>/<window>/     confirmed, permanent
    sites/<site>/trash/<purge-date>/<day>/<window>/
    sites/<site>/dataset/detections/<bucket>/   records

A window's LOCATION is its state. Moving one is a single os.rename inside the site (never a copy
across filesystems, which could leave two half-windows), so its identity -- <day>/<window> --
never changes and no record that mentions it has to be rewritten.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

LIVE, INBOX, FRAMES, TRASH = "live", "inbox", "frames", "trash"
NEGATIVES = "negatives"        # single frames with no detection, kept as hard negatives
RECORDS = Path("dataset") / "detections"
LIFECYCLE_LOG = "lifecycle.jsonl"

# Files a bucket (one model's records) may hold. `--check` reports anything else.
BUCKET_FILES = {
    "detections.jsonl", "live_events.jsonl", LIFECYCLE_LOG, "verdicts.json", "track_meta.json",
    "manual_boxes.jsonl", "box_edits.jsonl", "folder_status.json", "alerts.json", "crops",
    "proxies", "drone_summary.csv",
}
SITE_DIRS = {LIVE, INBOX, FRAMES, TRASH, NEGATIVES, "dataset", "backups"}

DEFAULT_INBOX_DAYS = 0      # 0 = unreviewed windows wait for a person, they never expire
DEFAULT_TRASH_DAYS = 0      # 0 = a rejection deletes the window right away (no trash period)


@dataclass(frozen=True)
class Retention:
    inbox_days: float = DEFAULT_INBOX_DAYS
    trash_days: float = DEFAULT_TRASH_DAYS

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Retention":
        block = (cfg or {}).get("retention") or {}
        return cls(float(block.get("inbox_days", DEFAULT_INBOX_DAYS)),
                   float(block.get("trash_days", DEFAULT_TRASH_DAYS)))


class SiteLayout:
    """`site_root` is the local, hot tree: capture and review read and write only here. `archive` (optional) is
    this site's folder on slow storage (the NAS): confirmed windows and old negatives are moved there by
    archive.py and are still found, read-only in effect, by window_dir() and the negatives lookups."""

    def __init__(self, site_root: Path, archive: Path | None = None):
        self.root = Path(site_root)
        self.archive = Path(archive) if archive else None
        self.archive_frames = self.archive / FRAMES if self.archive else None
        self.archive_negatives = self.archive / NEGATIVES if self.archive else None
        self.live = self.root / LIVE
        self.inbox = self.root / INBOX
        self.frames = self.root / FRAMES
        self.trash = self.root / TRASH
        self.negatives = self.root / NEGATIVES
        self.records = self.root / RECORDS

    def ensure(self) -> None:
        for p in (self.live, self.inbox, self.frames, self.trash, self.negatives, self.records):
            p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- windows
    def confirmed_roots(self) -> list[Path]:
        """Where confirmed windows live: local frames/, then the archive's."""
        return [self.frames] + ([self.archive_frames] if self.archive_frames else [])

    def window_dir(self, day: str, window: str, archive: bool = True) -> Path | None:
        """Where this window is right now (inbox, confirmed, then the archive), or None.
        archive=False skips the slow storage: for callers that only need to know about local state."""
        roots = (self.inbox, self.frames) + ((self.archive_frames,) if archive and self.archive_frames else ())
        for root in roots:
            candidate = root / day / window
            if candidate.is_dir():
                return candidate
        return None

    def is_archived(self, path: Path) -> bool:
        return bool(self.archive_frames) and self.archive_frames in path.parents

    def negative_file(self, day: str, name: str) -> Path | None:
        """A saved negative, local first, then archived."""
        for root in (self.negatives, self.archive_negatives):
            if root is not None and (root / day / name).is_file():
                return root / day / name
        return None

    def root_of(self, day: str, window: str) -> Path | None:
        found = self.window_dir(day, window)
        return found.parent.parent if found else None

    def state(self, day: str, window: str) -> str | None:
        found = self.window_dir(day, window)
        if found is None:
            return "trashed" if self.trashed_dir(day, window) else None
        return "pending" if found.parent.parent == self.inbox else "confirmed"   # frames/ or archived

    def trashed_dir(self, day: str, window: str) -> Path | None:
        if not self.trash.is_dir():
            return None
        for purge in sorted(self.trash.iterdir()):
            candidate = purge / day / window
            if candidate.is_dir():
                return candidate
        return None

    def exists_anywhere(self, day: str, window: str) -> bool:
        return self.window_dir(day, window) is not None or self.trashed_dir(day, window) is not None

    def unique_window(self, day: str, name: str) -> str:
        """`name`, or `name-2`, `name-3`... until no root (inbox, confirmed, trash) has it, so a
        repeated HHMMSS (restart in the same second, the autumn clock change) never reuses an identity."""
        candidate, n = name, 1
        while self.exists_anywhere(day, candidate):
            n += 1
            candidate = f"{name}-{n}"
        return candidate

    def iter_windows(self, root: Path):
        if not root.is_dir():
            return
        for day_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for win in sorted(p for p in day_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
                yield day_dir.name, win.name, win

    # ---------------------------------------------------------------- moves
    def move(self, day: str, window: str, dest_root: Path, purge_date: str | None = None) -> Path:
        """Rename a window into `dest_root` (inbox/frames, or trash/<purge-date>). One rename, nothing else."""
        src = self.window_dir(day, window, archive=False)      # a rename never crosses to the archive
        if src is None:
            raise FileNotFoundError(f"{day}/{window} is not in inbox or frames")
        dest = (dest_root / purge_date if purge_date else dest_root) / day / window
        if dest.exists():
            raise FileExistsError(f"{day}/{window} already exists at {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dest)          # OSError (e.g. across filesystems) propagates: never copy
        try:
            src.parent.rmdir()        # an emptied day folder
        except OSError:
            pass
        return dest

    # -------------------------------------------------------------- buckets
    def buckets(self) -> list[Path]:
        return [p for p in sorted(self.records.iterdir()) if p.is_dir()] if self.records.is_dir() else []


def is_closed(window_dir: Path) -> bool:
    """Capture writes window.json when (and only when) it closes the window."""
    return (window_dir / "window.json").is_file()


def closed_at(window_dir: Path) -> float | None:
    try:
        data = json.loads((window_dir / "window.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("closed_at")
    if isinstance(value, (int, float)):
        return float(value)
    return (window_dir / "window.json").stat().st_mtime   # imports have closed_at but no wall clock


def purge_date_for(retention: Retention, now: float | None = None) -> str:
    when = datetime.fromtimestamp(now if now is not None else time.time()) + timedelta(days=retention.trash_days)
    return when.date().isoformat()


def log_event(bucket: Path, **fields) -> None:
    """Append one line to <bucket>/lifecycle.jsonl: the audit trail of window moves."""
    bucket.mkdir(parents=True, exist_ok=True)
    row = {"ts": round(time.time(), 3), **fields}
    with (bucket / LIFECYCLE_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def read_events(bucket: Path) -> list[dict]:
    path = bucket / LIFECYCLE_LOG
    rows: list[dict] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


_EVENTS_CACHE: dict = {}


def _latest_states(bucket: Path) -> dict[str, str]:
    """{day/window: last `to`} from the bucket's lifecycle log, re-read only when the log changed."""
    path = bucket / LIFECYCLE_LOG
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    hit = _EVENTS_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    latest: dict[str, str] = {}
    for ev in read_events(bucket):
        if ev.get("day") and ev.get("window") and ev.get("to"):    # review notes carry no `to`
            latest[f"{ev['day']}/{ev['window']}"] = ev["to"]
    _EVENTS_CACHE[path] = (sig, latest)
    return latest


def gone_windows(layout: SiteLayout) -> set[str]:
    """`day/window` ids whose records must be hidden: trashed or purged and not back anywhere."""
    gone: set[str] = set()
    for bucket in layout.buckets():
        for wid, to in _latest_states(bucket).items():
            if to in ("trash", "purged"):
                day, _, window = wid.partition("/")
                if layout.window_dir(day, window, archive=False) is None:
                    gone.add(wid)
    return gone
