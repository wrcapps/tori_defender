#!/usr/bin/env python3
"""Turn the Dataset view's decisions into a cleaned copy of the training set.

WHY A COPY, NOT AN EDIT IN PLACE:
    The set a model was trained on has to stay exactly as it was, or the run
    that produced today's weights can never be reproduced or compared against.
    The clean set is a new directory; images are hard-linked, so it costs
    kilobytes rather than another copy of every tile.

WHY A TILE WHOSE LAST LABEL WAS REJECTED IS KEPT:
    If the only box on a tile was a false positive, the tile itself is a
    perfectly good NEGATIVE -- a picture of whatever the model wrongly fired on,
    now labelled "nothing here". Deleting it would throw away the one example
    that teaches the mistake away. It is kept, with an empty label file, and
    counted separately below.

    ./apply_label_review.py --data sites/combined/dataset/coco-cvat/data.yaml
    ./apply_label_review.py --data .../data.yaml --out /tmp/clean --dry-run
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, help="the dataset's data.yaml")
    ap.add_argument("--decisions", type=Path, default=None,
                    help="default: label_review.json beside the dataset")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: <dataset>-clean beside the original")
    ap.add_argument("--copy", action="store_true",
                    help="copy the images instead of hard-linking them (use when the "
                         "destination is on another filesystem)")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.data.read_text(encoding="utf-8")) or {}
    root = Path(cfg.get("path") or args.data.parent)
    splits = {k: cfg[k] for k in ("train", "val", "test") if cfg.get(k)}
    decisions_path = args.decisions or (root / "label_review.json")
    out = args.out or root.with_name(root.name + "-clean")

    if not decisions_path.exists():
        print(f"no decisions at {decisions_path} -- nothing to apply")
        return 1
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    rejected = {k for k, v in decisions.items() if v.get("bad")}
    if not rejected:
        print(f"{len(decisions)} decisions, none of them a rejection -- nothing to do")
        return 1

    print(f"source     {root}")
    print(f"decisions  {decisions_path}  ({len(rejected)} rejected of {len(decisions)} judged)")
    print(f"clean copy {out}{'  (dry run)' if args.dry_run else ''}\n")

    stats = collections.Counter()
    per_split: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for split, rel in splits.items():
        labels_dir = root / rel.replace("images", "labels")
        images_dir = root / rel
        if not labels_dir.is_dir():
            continue
        out_labels = out / rel.replace("images", "labels")
        out_images = out / rel
        if not args.dry_run:
            out_labels.mkdir(parents=True, exist_ok=True)
            out_images.mkdir(parents=True, exist_ok=True)

        for lp in sorted(labels_dir.glob("*.txt")):
            rows = [l for l in lp.read_text(encoding="utf-8").splitlines() if l.strip()]
            kept = [r for i, r in enumerate(rows) if f"{split}/{lp.stem}#{i}" not in rejected]
            dropped = len(rows) - len(kept)

            per_split[split]["tiles"] += 1
            per_split[split]["labels_in"] += len(rows)
            per_split[split]["labels_out"] += len(kept)
            if dropped:
                per_split[split]["tiles_changed"] += 1
                per_split[split]["labels_dropped"] += dropped
                if rows and not kept:
                    # became a negative: the thing it pointed at was not a bird
                    per_split[split]["became_negative"] += 1

            if args.dry_run:
                continue
            (out_labels / lp.name).write_text(
                "".join(r + "\n" for r in kept), encoding="utf-8")
            src_img = images_dir / f"{lp.stem}.jpg"
            dst_img = out_images / src_img.name
            if src_img.exists() and not dst_img.exists():
                if args.copy:
                    shutil.copy2(src_img, dst_img)
                else:
                    try:
                        os.link(src_img, dst_img)
                    except OSError:
                        shutil.copy2(src_img, dst_img)

    for split, c in per_split.items():
        print(f"{split}:")
        print(f"  tiles                {c['tiles']:>7}")
        print(f"  labels               {c['labels_in']:>7} -> {c['labels_out']}"
              f"   ({c['labels_dropped']} removed)")
        print(f"  tiles changed        {c['tiles_changed']:>7}")
        print(f"  tiles now negative   {c['became_negative']:>7}"
              f"   (kept, with an empty label file)")
        stats.update(c)

    if not args.dry_run:
        clean_yaml = dict(cfg)
        clean_yaml["path"] = str(out)
        (out / "data.yaml").write_text(yaml.safe_dump(clean_yaml, sort_keys=False),
                                       encoding="utf-8")
        shutil.copy2(decisions_path, out / "label_review.json")
        print(f"\nwrote {out / 'data.yaml'}")
        print(f"train with:  yolo detect train data={out / 'data.yaml'} model=yolo11s.pt imgsz=640")
    else:
        print("\n(dry run -- nothing written)")

    print(f"\ntotal: {stats['labels_dropped']} label(s) removed from "
          f"{stats['tiles_changed']} tile(s); {stats['became_negative']} tile(s) "
          f"became negatives")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
