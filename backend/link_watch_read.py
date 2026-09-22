#!/usr/bin/env python3
"""A small, read-only JSON view over link_watch.py's CSV output.

WHY NOT JUST IMPORT link_watch.py:
    bird-review-app is its own, independently git-inited, independently
    deployable subproject (see README.md) -- one that can run on a machine
    that never has the research pipeline's root-level scripts checked out at
    all. So this reads the same CSV *format* without a cross-repo import.

    That means this module's FIELDS below is a hand-kept copy of
    link_watch.py's own FIELDS list, not a shared source of truth -- if
    link_watch.py's schema ever changes, this needs a matching manual update.
    An honest, small, documented duplication, not a silent trap.
"""
from __future__ import annotations

import csv
from pathlib import Path

# Kept in sync by hand with link_watch.py's own FIELDS (repo root). Only the
# columns this summary actually uses are named here.
FIELDS = ["timestamp", "camera", "host", "probe", "ok", "bytes", "seconds",
          "kbit_s", "wg_rx_bytes", "wg_rx_errors", "wg_rx_errors_delta", "detail"]

# Bound the read: link_watch.py runs "for as long as you leave it running",
# so its CSV can grow to many thousands of rows over weeks -- this API is a
# recent-window summary, not a full-history export, so only the tail is read.
MAX_ROWS = 20000


def summarize(csv_path: Path, window_hours: float = 6.0) -> dict:
    """A JSON-shaped version of link_watch.py's own summarise(), read-only."""
    if not csv_path.exists():
        return {"rows": 0, "message": f"{csv_path} does not exist yet"}

    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) > MAX_ROWS:
        rows = rows[-MAX_ROWS:]
    if not rows:
        return {"rows": 0, "message": "no rows yet"}

    cutoff = rows[-1]["timestamp"][:13]  # same "YYYY-MM-DDTHH" bucketing as link_watch.py
    by_hour: dict[str, list[float]] = {}
    fails = 0
    for row in rows:
        if row.get("ok") != "True":
            fails += 1
            continue
        if row.get("probe") != "throughput":
            continue
        by_hour.setdefault(row["timestamp"][:13], []).append(float(row["kbit_s"]))

    hourly = [{"hour": hour, "mean_kbit_s": round(sum(v) / len(v), 1),
              "max_kbit_s": round(max(v), 1), "samples": len(v)}
             for hour, v in sorted(by_hour.items())]

    latencies = sorted(float(r["seconds"]) * 1000 for r in rows
                       if r.get("probe") == "latency" and r.get("ok") == "True")
    latency_summary = None
    if latencies:
        latency_summary = {"median_ms": round(latencies[len(latencies) // 2], 1),
                           "worst_ms": round(latencies[-1], 1)}

    errors = [int(r["wg_rx_errors"]) for r in rows
             if r.get("wg_rx_errors") not in (None, "") and int(r["wg_rx_errors"]) >= 0]
    wg_rx_errors_delta = (errors[-1] - errors[0]) if len(errors) > 1 else None

    return {
        "rows": len(rows),
        "first_timestamp": rows[0]["timestamp"],
        "last_timestamp": rows[-1]["timestamp"],
        "hourly_throughput": hourly,
        "latency": latency_summary,
        "failed_probes": fails,
        "wg_rx_errors_delta": wg_rx_errors_delta,
    }
