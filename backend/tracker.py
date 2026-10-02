#!/usr/bin/env python3
"""Link detections across frames so one bird keeps one track id.

WHY THIS EXISTS:
    The detector gives every box it measures a fresh id, so a bird crossing seven
    frames was seven unrelated "tracks": nothing to draw a trail through, and a
    verdict or a distance had to be entered seven times. This links a new frame's
    boxes to the tracks already open in the same window.

HOW (small on purpose -- a handful of birds per window, frames 0.25-1 s apart):
    constant-velocity prediction of each open track, then greedy nearest-first
    matching on predicted centre distance, gated by max(min_gate_px, gate_diag * the
    box diagonal). A track that goes unmatched for more than `max_missed` linked
    frames is closed. Greedy is enough here; Hungarian only changes the answer when
    two birds cross inside each other's gate, which a reviewer can fix with move.

    The state is per window (one `Linker` per capture window / imported session).
    A Linker never reaches into other windows, so ids stay unique per detection run
    through the caller's counter.
"""
from __future__ import annotations

import math


def _centre(box):
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _diag(box):
    return math.hypot(box[2] - box[0], box[3] - box[1])


class Linker:
    def __init__(self, next_id, min_gate_px: float = 150.0, gate_diag: float = 6.0,
                 max_missed: int = 3):
        """next_id: zero-arg callable returning a fresh track id (the caller's counter)."""
        self.next_id = next_id
        self.min_gate_px = min_gate_px
        self.gate_diag = gate_diag
        self.max_missed = max_missed
        self.tracks: dict[int, dict] = {}

    def update(self, boxes: list[dict]) -> list[dict]:
        """Give every box in this frame a `track`, reusing an open track where it fits.

        `boxes` are dicts with a `bbox`; each gets `track` set in place. Returns them.
        """
        pairs = []
        for i, box in enumerate(boxes):
            cx, cy = _centre(box["bbox"])
            for tid, tr in self.tracks.items():
                px, py = tr["cx"] + tr["vx"], tr["cy"] + tr["vy"]
                dist = math.hypot(cx - px, cy - py)
                gate = max(self.min_gate_px, self.gate_diag * max(_diag(box["bbox"]), tr["diag"]))
                if dist <= gate:
                    pairs.append((dist, i, tid))
        pairs.sort()
        taken_box: set[int] = set()
        taken_track: set[int] = set()
        for dist, i, tid in pairs:
            if i in taken_box or tid in taken_track:
                continue
            taken_box.add(i)
            taken_track.add(tid)
            box = boxes[i]
            cx, cy = _centre(box["bbox"])
            tr = self.tracks[tid]
            tr.update(vx=0.6 * (cx - tr["cx"]) + 0.4 * tr["vx"],
                      vy=0.6 * (cy - tr["cy"]) + 0.4 * tr["vy"],
                      cx=cx, cy=cy, diag=_diag(box["bbox"]), missed=0)
            box["track"] = tid
        for i, box in enumerate(boxes):
            if i in taken_box:
                continue
            tid = self.next_id()
            cx, cy = _centre(box["bbox"])
            self.tracks[tid] = dict(cx=cx, cy=cy, vx=0.0, vy=0.0, diag=_diag(box["bbox"]), missed=0)
            box["track"] = tid
            taken_track.add(tid)
        for tid in list(self.tracks):
            if tid not in taken_track:
                self.tracks[tid]["missed"] += 1
                if self.tracks[tid]["missed"] > self.max_missed:
                    del self.tracks[tid]
        return boxes
