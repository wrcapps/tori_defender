#!/usr/bin/env python3
"""Structured, append-only metrics logging shared by live_capture.py and
capture_manager.py -- the Phase 0 instrumentation baseline (PERFORMANCE.md).

WHY ROTATED BY SIZE, NOT TRUNCATED ON PROCESS START:
    capture_manager.py routinely stops and restarts an idle camera's process
    (no viewers, not recording). Truncating this file every time a process
    starts would repeatedly destroy the very sample this instrumentation
    exists to accumulate -- a camera that idles-and-restarts several times
    overnight would lose its own history right before anyone collects it.
    So a metrics file is appended to for as long as it lives, and only
    rotated once it grows past MAX_BYTES, keeping exactly one prior file.

WHY ONE FILE PER CAMERA, NEVER SHARED:
    Two cameras' processes writing the same file could interleave a partial
    line, and one camera's rotation could clip data another is still
    appending. Every call site below is scoped to a single camera (or, for
    capacity, to one process-global file -- see capture_manager.py's own
    comment on why that one is process-wide, not per-camera).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024  # rotate past ~20MB; keep exactly one prior file


def log_metric(path: Path, kind: str, *, print_line: bool = True, **fields) -> None:
    """Append one JSON line ({"ts", "kind", **fields}) to `path`.

    `print_line=False` skips the stdout echo for high-frequency call sites
    (e.g. once per sampled frame) where a JSONL record already captures
    everything and a matching console line would just be log noise --
    matches this codebase's own precedent of deliberately dropping a
    high-frequency, low-value message (model_infer.py's `half=` deprecation
    filter).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_BYTES:
        rotated = path.with_suffix(path.suffix + ".1")
        rotated.unlink(missing_ok=True)
        path.rename(rotated)
    record = {"ts": time.time(), "kind": kind, **fields}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    if print_line:
        details = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[metrics] {kind} {details}", flush=True)
