#!/usr/bin/env python3
"""What happens to a window after capture: confirm, reject, expire, purge, restore. See docs/DATA_LAYOUT.md.

    lifecycle.py check   --sites-root sites --site babadag       # read-only report
    lifecycle.py migrate --sites-root sites --site babadag       # dry run; add --apply to write
    lifecycle.py sweep   --sites-root sites --site babadag       # dry run; add --apply to act
    lifecycle.py restore --sites-root sites --site babadag DAY WINDOW

The rules are deliberately conservative -- this code deletes footage:
  * only windows in inbox/ are ever decided; frames/ (confirmed, imported, grandfathered) is never touched;
  * a window is only decided once capture has CLOSED it (window.json exists);
  * hand work (manual boxes, box edits) and any verdict other than a plain reject protect it;
  * unreviewed windows expire into trash/ (recoverable for retention.trash_days), they are not deleted;
  * a move is one os.rename; any failure skips that window and is reported;
  * one run moves at most MAX_MOVES windows.
It reads the records straight from disk, so it never depends on what an app's caches believe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

from layout import (BUCKET_FILES, FRAMES, INBOX, LIFECYCLE_LOG, SITE_DIRS, TRASH, Retention, SiteLayout,
                    closed_at, is_closed, log_event, purge_date_for, read_events)

MAX_MOVES = 50
TRACK_ID = re.compile(r"^(?P<day>[^/]+)/(?P<window>[^/]+)/(?P<track>\w+)$")


class WindowFacts:
    """Everything the records say about one window, across every bucket."""

    def __init__(self):
        self.tracks: set[str] = set()
        self.verdicts: dict[str, str] = {}
        self.hand_work = False      # manual boxes or box edits: a person drew these
        self.has_meta = False       # species/distance/size typed in

    @property
    def keeps(self) -> bool:
        return any(v == "keep" for v in self.verdicts.values())

    @property
    def rejected(self) -> bool:
        """Every model track has been dropped, and there is at least one."""
        return bool(self.tracks) and all(self.verdicts.get(t) == "drop" for t in self.tracks)


def _jsonl(path: Path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


_PARSE_CACHE: dict = {}


def _signature(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cached(path: Path, parse):
    """parse(path), remembered until the file's mtime/size changes (the big files are append-only)."""
    sig = _signature(path)
    hit = _PARSE_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    value = parse(path)
    _PARSE_CACHE[path] = (sig, value)
    return value


def _parse_detections(path: Path) -> dict:
    tracks: dict[str, set] = {}
    for row in _jsonl(path):
        if row.get("day") and row.get("window") and row.get("track") is not None:
            tracks.setdefault(f"{row['day']}/{row['window']}", set()).add(
                f"{row['day']}/{row['window']}/t{int(row['track']):04d}")
    return tracks


def _parse_windows_of_rows(path: Path) -> set:
    return {f"{r['day']}/{r['window']}" for r in _jsonl(path) if r.get("day") and r.get("window")}


def load_facts(layout: SiteLayout) -> dict[str, WindowFacts]:
    facts: dict[str, WindowFacts] = {}

    def of(wid) -> WindowFacts:
        return facts.setdefault(wid, WindowFacts())

    for bucket in layout.buckets():
        for wid, tracks in _cached(bucket / "detections.jsonl", _parse_detections).items():
            of(wid).tracks |= tracks
        for tid, verdict in _json(bucket / "verdicts.json").items():
            m = TRACK_ID.match(tid)
            if m:
                of(f"{m['day']}/{m['window']}").verdicts[tid] = verdict
        for tid in _json(bucket / "track_meta.json"):
            m = TRACK_ID.match(tid)
            if m:
                of(f"{m['day']}/{m['window']}").has_meta = True
        for name in ("manual_boxes.jsonl", "box_edits.jsonl"):
            for wid in _cached(bucket / name, _parse_windows_of_rows):
                of(wid).hand_work = True
    return facts


def _is_import(window_dir: Path) -> bool:
    try:
        return "excel_row" in json.loads((window_dir / "window.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False


def _age_days(window_dir: Path, now: float) -> float | None:
    try:
        data = json.loads((window_dir / "window.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stamp = max(float(data.get("closed_at") or 0), float(data.get("restored_at") or 0))
    return (now - stamp) / 86400 if stamp else None


def decide(layout: SiteLayout, day: str, window: str, window_dir: Path, facts: WindowFacts,
           retention: Retention, now: float) -> tuple[str, str] | None:
    """('confirmed'|'trash', reason) for an inbox window, or None to leave it where it is."""
    if not is_closed(window_dir) or _is_import(window_dir):
        return None
    if facts.keeps:
        return "confirmed", "keep"
    if facts.hand_work:
        return None
    if facts.rejected:
        return "trash", "rejected"
    if not facts.verdicts and not facts.has_meta:
        age = _age_days(window_dir, now)
        if age is not None and age > retention.inbox_days:
            return "trash", "expired"
    return None


def reconcile(layout: SiteLayout, retention: Retention, log_bucket: Path, by: str = "system",
              apply: bool = True, now: float | None = None, max_moves: int = MAX_MOVES) -> list[dict]:
    """Decide every inbox window and (if `apply`) move it. Returns what it did / would do.

    The records are read once for the pass, but each move is re-decided from a fresh read just
    before it happens: a verdict saved by another process in between cancels the move."""
    now = time.time() if now is None else now
    facts = load_facts(layout)
    actions: list[dict] = []
    moved = 0
    for day, window, path in list(layout.iter_windows(layout.inbox)):
        if moved >= max_moves:
            break
        if layout.trashed_dir(day, window) or (layout.frames / day / window).exists():
            actions.append({"day": day, "window": window, "error": "identity exists in two roots; left alone"})
            continue
        wid = f"{day}/{window}"
        verdict = decide(layout, day, window, path, facts.get(wid, WindowFacts()), retention, now)
        if verdict is None:
            continue
        if apply:
            verdict = decide(layout, day, window, path, load_facts(layout).get(wid, WindowFacts()), retention, now)
            if verdict is None:
                continue
        to, reason = verdict
        action = {"day": day, "window": window, "from": "pending", "to": to, "reason": reason}
        if apply:
            try:
                if to == "confirmed":
                    layout.move(day, window, layout.frames)
                else:
                    layout.move(day, window, layout.trash, purge_date_for(retention, now))
                    action["to"] = "trash"
                log_event(log_bucket, day=day, window=window, **{"from": "pending", "to": action["to"]},
                          reason=reason, by=by)
                moved += 1
            except (OSError, FileNotFoundError, FileExistsError) as exc:
                action["error"] = str(exc)
        else:
            moved += 1
        actions.append(action)
    return actions


def purge(layout: SiteLayout, log_bucket: Path, today: date | None = None, apply: bool = True,
          max_moves: int = MAX_MOVES) -> list[dict]:
    """Delete trash whose purge-date has passed, with the window's crops and proxies in every bucket."""
    today = today or date.today()
    out: list[dict] = []
    if not layout.trash.is_dir():
        return out
    for purge_dir in sorted(p for p in layout.trash.iterdir() if p.is_dir()):
        try:
            due = date.fromisoformat(purge_dir.name) <= today
        except ValueError:
            continue
        if not due:
            continue
        for day, window, path in list(layout.iter_windows(purge_dir)):
            if len(out) >= max_moves:
                return out
            if layout.trash not in path.parents:
                continue                                  # never delete outside trash/
            if layout.window_dir(day, window) is not None:
                out.append({"day": day, "window": window, "error": "also present in inbox/frames; not purged"})
                continue
            action = {"day": day, "window": window, "to": "purged"}
            if apply:
                shutil.rmtree(path, ignore_errors=True)
                for bucket in layout.buckets():
                    for sub in ("crops", "proxies"):
                        shutil.rmtree(bucket / sub / day / window, ignore_errors=True)
                log_event(log_bucket, day=day, window=window, **{"from": "trash", "to": "purged"}, reason="retention", by="system")
            out.append(action)
        if apply:
            for day_dir in list(purge_dir.iterdir()):
                try:
                    day_dir.rmdir()
                except OSError:
                    pass
            try:
                purge_dir.rmdir()
            except OSError:
                pass
    return out


def restore(layout: SiteLayout, day: str, window: str, log_bucket: Path, by: str = "operator",
            clear_verdicts: bool = True) -> Path:
    """Bring a trashed window back to inbox with a fresh expiry clock.

    Its `drop` verdicts are what trashed it, so they are cleared too -- otherwise the next sweep would
    reject it again. Pass clear_verdicts=False when the running app clears them itself through its own
    in-memory store (writing verdicts.json behind its back would be overwritten)."""
    src = layout.trashed_dir(day, window)
    if src is None:
        raise FileNotFoundError(f"{day}/{window} is not in trash")
    dest = layout.inbox / day / window
    if dest.exists() or (layout.frames / day / window).exists():
        raise FileExistsError(f"{day}/{window} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    import os
    os.rename(src, dest)
    marker = dest / "window.json"
    data = _json(marker)
    data["restored_at"] = time.time()
    tmp = marker.with_name(".window.json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(marker)
    if clear_verdicts:
        for bucket in layout.buckets():
            path = bucket / "verdicts.json"
            current = _json(path)
            kept = {t: v for t, v in current.items() if not (t.startswith(f"{day}/{window}/") and v == "drop")}
            if kept != current:
                tmp = path.with_name(".verdicts.json.tmp")
                tmp.write_text(json.dumps(kept, indent=1), encoding="utf-8")
                tmp.replace(path)
    log_event(log_bucket, day=day, window=window, **{"from": "trash", "to": "pending"}, reason="restored", by=by)
    return dest


# ------------------------------------------------------------------- check / migrate
def check(layout: SiteLayout) -> list[tuple[str, str]]:
    """[(severity, message)] -- 'error' means data is at risk, 'warn' means look at it."""
    issues: list[tuple[str, str]] = []
    if layout.root.is_dir():
        for entry in sorted(layout.root.iterdir()):
            if entry.name not in SITE_DIRS:
                issues.append(("warn", f"unexpected entry at site level: {entry.name}"))
    for bucket in layout.buckets():
        for entry in sorted(bucket.iterdir()):
            if entry.name not in BUCKET_FILES and not entry.name.startswith(("verdicts.json.", "manual_boxes.jsonl.", ".")):
                issues.append(("warn", f"unexpected file in bucket {bucket.name}: {entry.name}"))
    seen: dict[str, str] = {}
    for root_name, root in ((INBOX, layout.inbox), (FRAMES, layout.frames)):
        for day, window, path in layout.iter_windows(root):
            wid = f"{day}/{window}"
            if wid in seen:
                issues.append(("error", f"{wid} exists in both {seen[wid]} and {root_name}"))
            seen[wid] = root_name
            if not is_closed(path):
                issues.append(("warn", f"{root_name}/{wid} has no window.json (open, or capture died)"))
    if layout.trash.is_dir():
        for purge_dir in layout.trash.iterdir():
            for day, window, _ in layout.iter_windows(purge_dir):
                if f"{day}/{window}" in seen:
                    issues.append(("error", f"{day}/{window} exists in trash and in {seen[f'{day}/{window}']}"))
    facts = load_facts(layout)
    missing = [wid for wid in facts if wid not in seen and not layout.trashed_dir(*wid.split("/", 1))]
    for wid in sorted(missing):
        f = facts[wid]
        sev = "error" if f.hand_work or f.verdicts else "warn"
        issues.append((sev, f"records mention {wid} but no such window folder exists"
                            f"{' (has hand work / verdicts!)' if sev == 'error' else ''}"))
    return issues


def migrate(layout: SiteLayout, log_bucket: Path, apply: bool) -> dict:
    """Bring an existing site under the layout. Moves NO footage: every window already in frames/ is
    confirmed by definition (grandfathered) -- putting old footage in inbox/ would let it expire."""
    plan = {"create": [], "grandfather": [], "backup": None}
    for p in (layout.inbox, layout.trash, layout.records):
        if not p.is_dir():
            plan["create"].append(str(p))
    logged = {(e.get("day"), e.get("window")) for b in layout.buckets() for e in read_events(b)}
    for day, window, _ in layout.iter_windows(layout.frames):
        if (day, window) not in logged:
            plan["grandfather"].append(f"{day}/{window}")
    if apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = layout.root / "backups" / stamp
        manifest = {}
        for bucket in layout.buckets():
            for f in sorted(bucket.iterdir()):
                if f.is_file():
                    dest = backup / bucket.name / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    manifest[f"{bucket.name}/{f.name}"] = hashlib.sha256(f.read_bytes()).hexdigest()
        if manifest:
            (backup / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
            plan["backup"] = str(backup)
        layout.ensure()
        for wid in plan["grandfather"]:
            day, _, window = wid.partition("/")
            log_event(log_bucket, day=day, window=window, **{"from": None, "to": "confirmed"},
                      reason="grandfathered", by="migration")
    return plan


def main() -> int:
    from sitepaths import DEFAULT_SITES_ROOT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "migrate", "sweep", "restore"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--sites-root", type=Path, default=DEFAULT_SITES_ROOT)
    ap.add_argument("--site", required=True)
    ap.add_argument("--bucket", default="live", help="bucket that receives the lifecycle log lines")
    ap.add_argument("--apply", action="store_true", help="without it, migrate and sweep only print what they would do")
    ap.add_argument("--inbox-days", type=float, default=None)
    ap.add_argument("--trash-days", type=float, default=None)
    a = ap.parse_args()

    layout = SiteLayout(a.sites_root / a.site)
    log_bucket = layout.records / a.bucket
    retention = Retention(a.inbox_days if a.inbox_days is not None else Retention().inbox_days,
                          a.trash_days if a.trash_days is not None else Retention().trash_days)
    if a.command == "check":
        issues = check(layout)
        for sev, msg in issues:
            print(f"{sev.upper():5} {msg}")
        print(f"{len(issues)} issue(s)")
        return 1 if any(s == "error" for s, _ in issues) else 0
    if a.command == "migrate":
        print(json.dumps(migrate(layout, log_bucket, a.apply), indent=1))
        return 0
    if a.command == "sweep":
        for act in reconcile(layout, retention, log_bucket, by="cli", apply=a.apply) + purge(layout, log_bucket, apply=a.apply):
            print(("" if a.apply else "WOULD ") + json.dumps(act))
        return 0
    if a.command == "restore":
        if len(a.args) != 2:
            ap.error("restore needs DAY WINDOW")
        print(restore(layout, a.args[0], a.args[1], log_bucket))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
