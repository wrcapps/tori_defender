#!/usr/bin/env python3
"""Re-render the zoom crop of every hand-drawn box whose crop file is missing.

A manual box's crop lives under <bucket>/crops/<day>/<window>/, the same folder the
model's crops go to, so wiping that folder to regenerate model crops also wipes these.
The box row (manual_boxes.jsonl) still names its crop; this redraws it from the frame
and the box. Touches nothing but missing crop files.

    ./backend/rebuild_manual_crops.py sites/babadag/dataset/detections/drone sites/babadag/frames
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from model_infer import save_crop


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bucket", type=Path, help="dataset/detections/<bucket> (holds manual_boxes.jsonl)")
    ap.add_argument("frames_root", type=Path)
    args = ap.parse_args()
    path = args.bucket / "manual_boxes.jsonl"
    if not path.is_file():
        print(f"no {path}")
        return 1
    made = missing_frame = present = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        crop = row.get("crop")
        if not crop:
            continue
        target = args.bucket / crop
        if target.exists():
            present += 1
            continue
        frame = cv2.imread(str(args.frames_root / row["day"] / row["window"] / row["file"]))
        if frame is None:
            missing_frame += 1
            continue
        made += save_crop(frame, row["bbox"], None, target)
    print(f"{made} crop(s) rebuilt, {present} already present, {missing_frame} without a frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
