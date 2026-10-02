#!/usr/bin/env python3
"""Where site data lives when the chosen folder (the NAS) may be down, and getting it back there.

    python backend/datastore.py status  [--primary DIR]    # which root would be used, what is waiting
    python backend/datastore.py migrate [--primary DIR]    # move waiting data into the primary root

THE RULE
    The primary root is the folder picked in Settings (data_dir), e.g. /mnt/Tori/BirdReviewApp/sites.
    If it is missing or not writable at start, the app runs on FALLBACK_ROOT (<app>/sites-fallback)
    instead, so capture and review keep working. At the next start with the primary usable, everything
    in the fallback is moved into the primary FIRST, before any Site object is built, and the fallback
    is left empty. Never while running: every Site and the acquisition service already hold their root.

WHAT A MERGE NEVER DOES
    Overwrite. The fallback starts empty, so it only holds what was captured/decided during the outage:
      * file absent in the primary          -> moved
      * identical file in both              -> fallback copy dropped
      * *.jsonl in both (append-only logs)  -> fallback lines appended to the primary's
      * any other file that differs         -> primary kept, fallback kept beside it as
                                               <name>.from-fallback-<stamp> and reported
"""
from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FALLBACK_ROOT = ROOT / "sites-fallback"
PROBE_TIMEOUT_S = 8.0          # a stale CIFS mount can block a stat for minutes; do not wait for it
LOG_NAME = ".migrations.log"


def usable(root: Path, timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """Is root an existing, writable folder? Probed in a thread so a hung mount cannot hang startup."""
    result: list[tuple[bool, str]] = []

    def probe():
        try:
            if not root.is_dir():
                result.append((False, "does not exist"))
                return
            with tempfile.NamedTemporaryFile(dir=root, prefix=".write-test-"):
                pass
            result.append((True, ""))
        except OSError as exc:
            result.append((False, exc.strerror or str(exc)))

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if result else (False, f"no answer in {timeout:.0f}s (stale mount?)")


def pending(fallback: Path = FALLBACK_ROOT) -> list[Path]:
    """Files waiting in the fallback root."""
    if not fallback.is_dir():
        return []
    return [p for p in fallback.rglob("*") if p.is_file()]


def _same(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and filecmp.cmp(a, b, shallow=False)


def _move_in(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
    src.unlink()


def migrate(fallback: Path, primary: Path, log=print) -> dict:
    """Move everything from fallback into primary under the rules in the module docstring."""
    stats = {"moved": 0, "identical": 0, "appended": 0, "conflicts": [], "errors": []}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for src in sorted(pending(fallback)):
        rel = src.relative_to(fallback)
        dest = primary / rel
        try:
            if not dest.exists():
                _move_in(src, dest)
                stats["moved"] += 1
            elif _same(src, dest):
                src.unlink()
                stats["identical"] += 1
            elif src.suffix == ".jsonl":
                data = src.read_bytes()
                with open(dest, "ab") as out:
                    if dest.stat().st_size and not _ends_with_newline(dest):
                        out.write(b"\n")
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                src.unlink()
                stats["appended"] += 1
            else:
                side = dest.with_name(f"{dest.name}.from-fallback-{stamp}")
                _move_in(src, side)
                stats["conflicts"].append(str(side))
        except OSError as exc:
            stats["errors"].append(f"{rel}: {exc.strerror or exc}")
    for d in sorted((p for p in fallback.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()                    # only succeeds when empty
        except OSError:
            pass
    line = (f"{time.strftime('%F %T')} {fallback} -> {primary}: {stats['moved']} moved, "
            f"{stats['identical']} identical, {stats['appended']} appended, "
            f"{len(stats['conflicts'])} conflicts, {len(stats['errors'])} errors")
    log(line)
    for c in stats["conflicts"]:
        log(f"  CONFLICT kept both: {c}")
    for e in stats["errors"]:
        log(f"  ERROR {e}")
    try:
        with open(primary / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(line + "\n" + "".join(f"  CONFLICT {c}\n" for c in stats["conflicts"]))
    except OSError:
        pass
    return stats


def _ends_with_newline(path: Path) -> bool:
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def choose_root(primary: Path, fallback: Path = FALLBACK_ROOT, log=print) -> tuple[Path, str]:
    """(root to run on, source label). Migrates waiting fallback data when the primary is back."""
    ok, why = usable(primary)
    if not ok:
        fallback.mkdir(parents=True, exist_ok=True)
        log(f"data folder {primary} unavailable ({why}); running on temporary folder {fallback}. "
            f"Its contents move to {primary} at the first start after it is back.")
        return fallback, f"fallback ({primary} unavailable: {why})"
    if pending(fallback):
        log(f"data folder {primary} is back; moving data captured while it was down ...")
        migrate(fallback, primary, log)
    return primary, "settings"


_status_cache: dict = {"at": 0.0, "key": None, "value": None}


def storage_status(primary: Path | None, active: Path, fallback: Path = FALLBACK_ROOT,
                   max_age: float = 10.0) -> dict:
    """What the UI shows about the data folder: {state, path, active, on_fallback, pending_files, ...}.

    state: "up" | "down" for the primary root; "local" when no primary was chosen (nothing to lose).
    Cached for max_age seconds: every open tab polls this, and each probe writes a file to the share."""
    key = (str(primary), str(active))
    now = time.monotonic()
    if _status_cache["key"] == key and now - _status_cache["at"] < max_age:
        return _status_cache["value"]
    on_fallback = Path(active).resolve() == Path(fallback).resolve()
    if primary is None:
        value = {"state": "local", "path": str(active), "active": str(active), "on_fallback": False,
                 "pending_files": 0, "reason": ""}
    else:
        ok, why = usable(Path(primary), timeout=4.0)
        value = {"state": "up" if ok else "down", "path": str(primary), "active": str(active),
                 "on_fallback": on_fallback, "reason": why,
                 "pending_files": len(pending(fallback)) if on_fallback else 0}
    _status_cache.update(at=now, key=key, value=value)
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "migrate"])
    ap.add_argument("--primary", type=Path, default=Path("/mnt/Tori/BirdReviewApp/sites"))
    ap.add_argument("--fallback", type=Path, default=FALLBACK_ROOT)
    a = ap.parse_args(argv)
    ok, why = usable(a.primary)
    files = pending(a.fallback)
    print(f"primary  {a.primary}: {'usable' if ok else 'UNAVAILABLE - ' + why}")
    print(f"fallback {a.fallback}: {len(files)} file(s) waiting, "
          f"{sum(f.stat().st_size for f in files) / 2**20:.1f} MiB")
    if a.cmd == "migrate":
        if not ok:
            print("primary is not usable; nothing moved", file=sys.stderr)
            return 1
        return 1 if migrate(a.fallback, a.primary)["errors"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
