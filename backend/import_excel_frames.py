#!/usr/bin/env python3
"""Run the detector over frames pulled for an Excel sheet and load them into Review.

INPUT:
    A folder written by fetch_excel_frames.py (it holds manifest.json): one subfolder
    per sheet row, 7 frames per camera, plus the row's Distance [m] / Altitude [m].

WHAT IT WRITES, UNDER <sites-root>/<site>/ :
    frames/<day>-<tag>/<camera>-d<dist>m-r<row>-<HHMMSS>/frame_0..N.jpg  + window.json
    dataset/detections/<bucket>/detections.jsonl   every box, with distance_m on it
    dataset/detections/<bucket>/track_meta.json    {track: {distance, species}} -> Review
    dataset/detections/<bucket>/crops/...          zoomed crop per detection
    dataset/detections/<bucket>/drone_summary.csv  one line per row x camera

WHY A SEPARATE BUCKET (default `drone`), NOT `live`:
    detections/live is what the research tooling reads when it builds training sets.
    Frames of a drone at a known range are not bird labels, and one 'keep' verdict
    would put them there. Review is pointed at this bucket with  app.py --model drone .
    verdicts.json / manual_boxes.jsonl in the bucket are the reviewer's and are never
    touched; detections.jsonl and track_meta.json are regenerated on each run.

WHAT `distance` MEANS HERE:
    It is the sheet's range of the DRONE from the tower -- ground truth for the
    drone, copied onto every box in that frame. If the model also fires on a bush or
    a bird in the same frame, that box carries the same number but it is not the
    drone's distance. `species` is set to "drone" on the same basis.

    ./backend/import_excel_frames.py ../sites/babadag/excel_frames/cadre_drona \\
        --weights ../sites/combined/dataset/runs/finetune-2026-09-11-reviewed-balanced/weights/best.pt
    ./backend/app.py --config config.yaml --model drone --port 8767
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import time
from pathlib import Path

import cv2
import yaml

from model_infer import build_infer, detect_frame, save_crop
from sitepaths import site_dir
from tracker import Linker

ROOT = Path(__file__).resolve().parent.parent
FRAME_FILE = re.compile(r"^cam(?P<octet>\d+)_(?P<k>[+-]?\d+)_")


def camera_names(cfg: dict) -> dict[str, str]:
    """last octet -> the camera's name in the app's config."""
    return {str(c["host"]).rsplit(".", 1)[-1]: c["name"] for c in cfg.get("cameras", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frames_dir", type=Path, help="folder holding manifest.json")
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    ap.add_argument("--sites-root", type=Path, default=None)
    ap.add_argument("--weights", required=True, help="YOLO weights (tiled inference)")
    ap.add_argument("--bucket", default="drone")
    ap.add_argument("--tag", default="drona",
                    help="suffix of the frames day folder, keeps these sessions apart "
                         "from live captures of the same day")
    ap.add_argument("--conf", type=float, default=0.25,
                    help="same default as live capture; lower it to see weaker boxes")
    ap.add_argument("--tile", type=int, default=640)
    ap.add_argument("--overlap", type=int, default=128)
    ap.add_argument("--nms-iou", type=float, default=0.5)
    ap.add_argument("--link-px", type=float, default=500.0,
                    help="smallest distance a box may move between frames and still be the "
                         "same track (frames are ~1 s apart; 0 = no linking, every box its "
                         "own track as before)")
    args = ap.parse_args()

    manifest = json.loads((args.frames_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    names = camera_names(cfg)
    frames_root = site_dir(cfg, "frames", base=args.sites_root, source=args.config)
    bucket_root = site_dir(cfg, "dataset", "detections", base=args.sites_root,
                           source=args.config) / args.bucket
    crops_root = bucket_root / "crops"
    bucket_root.mkdir(parents=True, exist_ok=True)

    infer = build_infer(args.weights, tile=args.tile, conf=args.conf)
    print(f"weights {args.weights}\n{sum(len(e['cameras']) for e in manifest)} sessions "
          f"to run, conf>={args.conf}, tile {args.tile}/{args.overlap}")

    rows_out, meta, summary = [], {}, []
    next_track, started = 1, time.time()
    for entry in manifest:
        stamp = entry["timestamp"]                 # 2026-09-30T14:03:30+03:00
        day = f"{stamp[:10]}-{args.tag}"
        hhmmss = stamp[11:19].replace(":", "")
        distance = entry["distance_m"]
        row_dir = next(args.frames_dir.glob(f"row{entry['row']:03d}_*"))
        for cam in entry["cameras"]:
            octet = cam["camera"]
            camera = names.get(octet, f"cam{octet}")
            window = f"{camera}-d{int(distance):03d}m-r{entry['row']:03d}-{hhmmss}"
            out_dir = frames_root / day / window
            if out_dir.exists():
                shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True)

            sources = sorted(
                (p for p in row_dir.glob(f"cam{octet}_*.jpg")),
                key=lambda p: int(FRAME_FILE.match(p.name).group("k")))
            best, count = None, 0
            tracks_in_window: set[int] = set()
            counter = [next_track]

            def fresh_id():
                counter[0] += 1
                return counter[0] - 1
            linker = Linker(fresh_id, min_gate_px=args.link_px, max_missed=3) if args.link_px > 0 else None
            for index, source in enumerate(sources):
                name = f"frame_{index}.jpg"
                shutil.copyfile(source, out_dir / name)
                image = cv2.imread(str(source))
                if image is None:
                    continue
                k = int(FRAME_FILE.match(source.name).group("k"))
                found = detect_frame(image, args.tile, infer, args.overlap, args.nms_iou)
                if linker is not None:
                    linker.update(found)
                else:
                    for b in found:
                        b["track"] = fresh_id()
                for b in found:
                    track = b["track"]
                    crop = crops_root / day / window / f"frame_{index}-t{track:04d}.jpg"
                    crop_rel = (str(crop.relative_to(crops_root.parent))
                                if save_crop(image, b["bbox"], b["conf"], crop) else None)
                    rows_out.append({
                        "day": day, "window": window, "file": name, "bbox": b["bbox"],
                        "conf": b["conf"], "track": track, "camera": camera,
                        "carried": False, "vx": 0.0, "vy": 0.0, "saved": True,
                        "crop": crop_rel, "capture_ts": None,
                        "distance_m": distance, "altitude_m": entry["altitude_m"],
                        "excel_row": entry["row"], "k": k, "source_file": source.name,
                    })
                    # Review keys track_meta/verdicts as <day>/<window>/t<NNNN>, not the bare id.
                    meta[f"{day}/{window}/t{track:04d}"] = {"distance": distance,
                                                            "species": "drone"}
                    count += 1
                    tracks_in_window.add(track)
                    if best is None or b["conf"] > best["conf"]:
                        best = dict(b, k=k, file=name)
            next_track = counter[0]
            (out_dir / "window.json").write_text(json.dumps({
                "camera": camera, "frames": len(sources), "opened_at": None,
                "closed_at": time.time(), "distance_m": distance,
                "altitude_m": entry["altitude_m"], "excel_row": entry["row"],
                "timestamp": stamp}, indent=1), encoding="utf-8")
            x0, y0, x1, y1 = best["bbox"] if best else (None,) * 4
            summary.append(dict(
                row=entry["row"], camera=camera, distance_m=distance,
                altitude_m=entry["altitude_m"], timestamp=stamp, detections=count,
                tracks=len(tracks_in_window),
                frames_with_det=len({r["file"] for r in rows_out
                                     if r["window"] == window}),
                best_conf=round(best["conf"], 3) if best else "",
                best_bbox_w=round(x1 - x0, 1) if best else "",
                best_bbox_h=round(y1 - y0, 1) if best else "",
                best_frame=best["file"] if best else ""))
            print(f"  row {entry['row']:>2} {camera:<10} d={distance:>4}m  "
                  f"{count} box(es){'' if best else '  -- none'}", flush=True)

    with (bucket_root / "detections.jsonl").open("w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(r) + "\n" for r in rows_out)
    (bucket_root / "track_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    with (bucket_root / "drone_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)

    hit = sum(1 for s in summary if s["detections"])
    print(f"\n{len(rows_out)} boxes in {hit}/{len(summary)} sessions, "
          f"{time.time() - started:.0f}s -> {bucket_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
