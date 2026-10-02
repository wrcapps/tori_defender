#!/usr/bin/env python3
"""Reviewing the training set itself: every label, worst-looking first.

WHY THE ORDER MATTERS MORE THAN THE INTERFACE:
    A set of 15,000 labels cannot be read end to end, and read in file order the
    handful of broken ones sit invisible among thousands of correct ones. So
    labels are ranked by how unlike a real bird-at-distance they are -- out of
    bounds, a couple of pixels across, filling half the tile, absurd aspect --
    and that ranking is what a reviewer walks down. Stopping half way then still
    means "I have seen everything questionable", not "I have seen the first half
    of the alphabet".

WHY ONE CELL IS ONE BOX, NOT ONE TILE:
    A tile with three birds in it can be right about two of them. The unit that
    can be wrong is the box, so the box is what gets judged and what gets
    removed.

WHAT A REJECTED LABEL DOES TO THE SET:
    Nothing, until apply_label_review.py is run. It only records the decision.
    Removing the last box from a tile does not delete the tile: a tile whose one
    label was a false positive is a perfectly good negative, and keeping it
    teaches exactly the thing that was wrong.
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path

import cv2
import numpy as np
import yaml

# What a bird looks like in this data, measured: median 32x23px in a 640px tile,
# p95 under 80px. These thresholds are about what CANNOT be one.
TINY_PX = 4.0
HUGE_FRAC = 0.35
MAX_ASPECT = 4.0

# Worst first. A reviewer who stops early should have seen the real problems.
CATEGORY_ORDER = ("out of bounds", "tiny", "huge", "stretched", "ordinary")
CATEGORY_HELP = {
    "out of bounds": "the box leaves the tile -- it cannot be a correct label",
    "tiny": f"under {TINY_PX:.0f}px on an edge, smaller than any bird measured here",
    "huge": f"over {HUGE_FRAC:.0%} of the tile; usually a real bird close to the "
            f"camera, which is a different object from the distant ones",
    "stretched": f"longer than {MAX_ASPECT:.0f}:1",
    "ordinary": "nothing obviously wrong with its geometry",
}


def classify(cx: float, cy: float, w: float, h: float, tile: int) -> tuple[str, float]:
    """(category, severity) for one normalised box. Higher severity sorts first."""
    pw, ph = w * tile, h * tile
    if not (0 <= cx - w / 2 and cx + w / 2 <= 1 and 0 <= cy - h / 2 and cy + h / 2 <= 1):
        overflow = max(w / 2 - cx, cx + w / 2 - 1, h / 2 - cy, cy + h / 2 - 1)
        return "out of bounds", 1000 + overflow * 100
    if pw < TINY_PX or ph < TINY_PX:
        return "tiny", 900 - min(pw, ph)
    if w > HUGE_FRAC or h > HUGE_FRAC:
        return "huge", 800 + max(w, h) * 100
    ratio = max(pw / max(ph, 1e-6), ph / max(pw, 1e-6))
    if ratio > MAX_ASPECT:
        return "stretched", 700 + ratio
    # Among ordinary labels, the extremes of the size distribution are still the
    # likeliest to be wrong, so rank by distance from the typical bird.
    return "ordinary", -abs(math.log(max(pw, 1e-6) / 32.0))


class DatasetIndex:
    """Every label in a YOLO dataset, ranked, plus the reviewer's decisions.

    Built once by walking the label files -- 31,000 of them takes a few seconds,
    which is why it happens at startup rather than per request.
    """

    def __init__(self, data_yaml: Path, tile: int = 640):
        self.data_yaml = data_yaml
        self.tile = tile
        cfg = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
        self.root = Path(cfg.get("path") or data_yaml.parent)
        self.names = cfg.get("names") or {0: "object"}
        self.splits = {k: cfg[k] for k in ("train", "val", "test") if cfg.get(k)}
        self.thumbs = self.root / ".review_thumbs"
        self.decisions_path = self.root / "label_review.json"

        self.lock = threading.Lock()
        self.items: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.by_tile: dict[tuple[str, str], list[dict]] = {}
        self.counts: dict[str, int] = {}
        self.split_counts: dict[str, int] = {}
        self.tiles_with_labels = 0
        self.tiles_empty = 0
        self.decisions: dict = {}
        if self.decisions_path.exists():
            try:
                self.decisions = json.loads(self.decisions_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.decisions = {}

    def build(self, progress=None) -> None:
        items = []
        empty = with_labels = 0
        for split, rel in self.splits.items():
            labels_dir = self.root / rel.replace("images", "labels")
            images_dir = self.root / rel
            if not labels_dir.is_dir():
                continue
            for lp in sorted(labels_dir.glob("*.txt")):
                rows = [l.split() for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
                if not rows:
                    empty += 1
                    continue
                with_labels += 1
                for i, bits in enumerate(rows):
                    cls = int(bits[0])
                    cx, cy, w, h = (float(v) for v in bits[1:5])
                    category, severity = classify(cx, cy, w, h, self.tile)
                    items.append({
                        "id": f"{split}/{lp.stem}#{i}",
                        "split": split, "stem": lp.stem, "index": i,
                        "cls": cls, "box": [cx, cy, w, h],
                        "category": category, "severity": severity,
                        "px": [round(w * self.tile), round(h * self.tile)],
                        "image": str((images_dir / f"{lp.stem}.jpg").relative_to(self.root)),
                    })
                if progress and with_labels % 2000 == 0:
                    progress(f"  indexed {with_labels} labelled tiles...")
        order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
        items.sort(key=lambda it: (order.get(it["category"], 99), -it["severity"]))
        counts: dict[str, int] = {}
        split_counts: dict[str, int] = {}
        for it in items:
            counts[it["category"]] = counts.get(it["category"], 0) + 1
            split_counts[it["split"]] = split_counts.get(it["split"], 0) + 1
        by_tile: dict[tuple[str, str], list[dict]] = {}
        for it in items:
            by_tile.setdefault((it["split"], it["stem"]), []).append(it)
        with self.lock:
            self.items = items
            self.by_id = {it["id"]: it for it in items}
            self.by_tile = by_tile
            self.counts = counts
            self.split_counts = split_counts
            self.tiles_with_labels = with_labels
            self.tiles_empty = empty

    # ------------------------------------------------------------------ query
    def summary(self) -> dict:
        with self.lock:
            marked = sum(1 for v in self.decisions.values() if v.get("bad"))
            return {
                "dataset": str(self.root),
                "classes": {str(k): v for k, v in self.names.items()},
                "tiles_with_labels": self.tiles_with_labels,
                "tiles_empty": self.tiles_empty,
                "labels": len(self.items),
                "rejected": marked,
                "reviewed": len(self.decisions),
                "categories": [
                    {"name": c, "count": self.counts.get(c, 0), "help": CATEGORY_HELP[c]}
                    for c in CATEGORY_ORDER if self.counts.get(c)],
                "splits": [
                    {"name": s, "count": self.split_counts.get(s, 0)}
                    for s in self.splits if self.split_counts.get(s)],
            }

    def page(self, category: str | None, split: str | None, offset: int, limit: int, hide_done: bool) -> dict:
        with self.lock:
            items = self.items
            if category and category != "all":
                items = [it for it in items if it["category"] == category]
            if split and split != "all":
                items = [it for it in items if it["split"] == split]
            if hide_done:
                items = [it for it in items if it["id"] not in self.decisions]
            total = len(items)
            window = items[offset:offset + limit]
            return {
                "total": total, "offset": offset,
                "items": [{**it, "decision": self.decisions.get(it["id"], {})} for it in window],
            }

    def mark(self, label_id: str, bad: bool, note: str | None = None) -> dict:
        with self.lock:
            if label_id not in self.by_id:
                return {}
            entry = {"bad": bool(bad)}
            if note:
                entry["note"] = note
            if bad or note:
                self.decisions[label_id] = entry
            else:
                self.decisions.pop(label_id, None)
                entry = {}
            self._flush()
            return entry

    def mark_many(self, ids: list[str], bad: bool) -> int:
        with self.lock:
            n = 0
            for label_id in ids:
                if label_id not in self.by_id:
                    continue
                if bad:
                    self.decisions[label_id] = {"bad": True}
                else:
                    self.decisions.pop(label_id, None)
                n += 1
            self._flush()
            return n

    def _flush(self) -> None:
        tmp = self.decisions_path.with_name(f".{self.decisions_path.name}.tmp")
        tmp.write_text(json.dumps(self.decisions, indent=1), encoding="utf-8")
        tmp.replace(self.decisions_path)

    # ------------------------------------------------------------------ thumbs
    def thumb(self, label_id: str, size: int = 240) -> bytes | None:
        """A square window around the box, with EVERY label on that tile drawn.

        Two things this has to get right, both learned from staring at the grid:

        The window is square and the image is pasted into a square canvas, never
        stretched. A window shaped to the box and then resized to a square cell
        scales the axes differently and slides the drawn rectangle off the thing
        it is meant to be around -- which a reviewer reads as a mislabelled box.

        Neighbouring labels are drawn too, dimmer. A crop around one bird in a
        flock otherwise shows five birds with one box on it, which looks exactly
        like five missing labels when in fact the others have boxes of their own
        in their own tiles.

        The window is also allowed to run past the edge of the tile, padded
        rather than clamped, so a box that leaves the image LOOKS like it leaves
        the image instead of being dragged into frame over a patch of empty sky.
        """
        with self.lock:
            item = self.by_id.get(label_id)
            siblings = list(self.by_tile.get((item["split"], item["stem"]), [])) if item else []
        if not item:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label_id)
        cached = self.thumbs / item["split"] / f"{safe}.jpg"
        if cached.is_file():
            return cached.read_bytes()

        image = cv2.imread(str(self.root / item["image"]))
        if image is None:
            return None
        H, W = image.shape[:2]

        def corners(box):
            cx, cy, w, h = box
            return (cx - w / 2) * W, (cy - h / 2) * H, (cx + w / 2) * W, (cy + h / 2) * H

        x0, y0, x1, y1 = corners(item["box"])

        # Centre on the part of the box that is actually inside the tile. An
        # out-of-bounds box has its centre outside the image, and centring on
        # that put 40% of every cell in the worst category on dead padding with
        # the bird shoved into a corner.
        vx0, vy0 = max(0.0, x0), max(0.0, y0)
        vx1, vy1 = min(float(W), x1), min(float(H), y1)
        if vx1 <= vx0 or vy1 <= vy0:      # nothing of it is inside at all
            vx0, vy0, vx1, vy1 = x0, y0, x1, y1

        # 2.6x the box shows enough context; never wider than the tile, so the
        # window can always be slid fully inside it.
        side = max(max(x1 - x0, y1 - y0) * 2.6, 90.0)
        side = min(side, float(max(W, H)))
        ccx, ccy = (vx0 + vx1) / 2, (vy0 + vy1) / 2
        sx = min(max(0.0, ccx - side / 2), max(0.0, W - side))
        sy = min(max(0.0, ccy - side / 2), max(0.0, H - side))

        wx, wy = int(round(sx)), int(round(sy))
        span = int(round(side))
        canvas = np.full((span, span, 3), 26, np.uint8)
        src_x0, src_y0 = max(0, wx), max(0, wy)
        src_x1, src_y1 = min(W, wx + span), min(H, wy + span)
        if src_x1 > src_x0 and src_y1 > src_y0:
            canvas[src_y0 - wy:src_y1 - wy, src_x0 - wx:src_x1 - wx] = \
                image[src_y0:src_y1, src_x0:src_x1]

        k = size / span
        # a smooth upscale is easier to judge a 20px bird on than a blocky one
        canvas = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_LANCZOS4)

        # the tile's own edge, where the window runs past it
        tx0, ty0 = int(-wx * k), int(-wy * k)
        tx1, ty1 = int((W - wx) * k), int((H - wy) * k)
        if tx0 > 0 or ty0 > 0 or tx1 < size or ty1 < size:
            cv2.rectangle(canvas, (tx0, ty0), (tx1 - 1, ty1 - 1), (90, 90, 90), 1)

        # The box is drawn clipped to the window, so a box that leaves the tile
        # would otherwise look like one that merely touches the edge. Mark the
        # edges it actually runs past.
        for over, pts in ((x0 < 0, ((1, 0), (1, size))), (y0 < 0, ((0, 1), (size, 1))),
                          (x1 > W, ((size - 2, 0), (size - 2, size))),
                          (y1 > H, ((0, size - 2), (size, size - 2)))):
            if over:
                cv2.line(canvas, pts[0], pts[1], (60, 60, 255), 3)

        for sib in siblings:
            bx0, by0, bx1, by1 = corners(sib["box"])
            if bx1 < wx or bx0 > wx + span or by1 < wy or by0 > wy + span:
                continue
            mine = sib["id"] == label_id
            cv2.rectangle(canvas,
                          (int((bx0 - wx) * k), int((by0 - wy) * k)),
                          (int((bx1 - wx) * k), int((by1 - wy) * k)),
                          (0, 230, 255) if mine else (255, 160, 80), 2 if mine else 1)

        ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return None
        payload = buf.tobytes()
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(f".{cached.name}.tmp")
        tmp.write_bytes(payload)
        tmp.replace(cached)
        return payload

    def full_tile(self, label_id: str) -> bytes | None:
        """The whole tile, every box on it drawn, so a reviewer can see what else
        is in the frame -- including birds nobody labelled."""
        with self.lock:
            item = self.by_id.get(label_id)
            siblings = [it for it in self.items
                        if it["split"] == item["split"] and it["stem"] == item["stem"]] if item else []
        if not item:
            return None
        image = cv2.imread(str(self.root / item["image"]))
        if image is None:
            return None
        H, W = image.shape[:2]
        for it in siblings:
            cx, cy, w, h = it["box"]
            colour = (0, 230, 255) if it["id"] == label_id else (255, 160, 80)
            cv2.rectangle(image, (int((cx - w / 2) * W), int((cy - h / 2) * H)),
                          (int((cx + w / 2) * W), int((cy + h / 2) * H)), colour, 2)
        image = cv2.resize(image, (W * 2, H * 2), interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        return buf.tobytes() if ok else None
