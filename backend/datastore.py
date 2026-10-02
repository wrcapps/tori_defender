#!/usr/bin/env python3
"""Is a folder on slow or removable storage (the NAS) reachable and writable right now?

The app never depends on the archive being up: capture and review use the local tree only, and
archive.py moves finished data over when it can. This probe is what archive.py (and Settings) use to tell
"up" from "down" without hanging on a stale mount.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

PROBE_TIMEOUT_S = 8.0          # a stale CIFS mount can block a stat for minutes; do not wait for it


def usable(root: Path, timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """(ok, reason). Probed in a thread so a hung mount cannot hang the caller."""
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
