#!/usr/bin/env python3
"""Tiled YOLO inference over full native frames, plus the image-writing helpers
every other script in this app shares.

WHY TILED, NOT A STRAIGHT model.predict(frame):
    The model is trained on native-resolution 640px tiles because a bird here is
    10-45px in a 2160x3840 frame -- resizing the whole frame down to imgsz=640
    shrinks it below anything the model has ever seen and it finds nothing,
    silently. So the frame is tiled on an overlapping grid and every detection is
    mapped back to full-frame coordinates.

WHY THE GRID OVERLAPS, AND WHY THERE IS AN NMS PASS AFTER:
    A non-overlapping grid splits a bird sitting on a tile seam in two: each
    tile sees a fragment, which is either too small to detect (a miss) or
    produces two partial boxes for one bird (a duplicate). 128px of overlap --
    comfortably above every bird size measured on this footage -- guarantees a
    bird near a seam sits fully inside at least one tile; nms() then collapses
    the duplicate that overlap itself creates.

WHY EVERY IMAGE WRITE IS ATOMIC:
    cv2.imwrite writes in place, so a reader that opens the file mid-write gets
    a truncated image. That is invisible when frames are only read long after
    capture, but this app's review scrubber and live viewer both read files
    while a capture process is still writing them -- so writes go to a temp file
    in the same directory and are renamed into place, which is atomic on POSIX.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np

# Zoom geometry for a review crop: a 10-45px bird needs this magnification to
# be judgeable at all.
ZOOM_NATIVE = 224
ZOOM_OUT = 260

BOX_COLOR = (0, 230, 255)      # measured detection
CARRIED_COLOR = (0, 160, 200)  # box carried from an earlier inferred frame


def atomic_imwrite(path: Path, image: np.ndarray, params: list[int] | None = None) -> bool:
    """Encode, write to a temp file in the same directory, then rename into place.

    The encode is done with cv2.imencode rather than cv2.imwrite so the format
    comes from the destination's suffix explicitly -- a temp file named for
    atomicity would otherwise leave OpenCV guessing the format from a `.tmp`
    extension, which it cannot do.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(path.suffix or ".jpg", image, params or [])
    if not ok:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(buffer.tobytes())
    os.replace(tmp, path)
    return True


def write_jpeg(path: Path, image: np.ndarray, quality: int = 95) -> bool:
    return atomic_imwrite(path, image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])


def fit_width(image: np.ndarray, width: int) -> np.ndarray:
    """Downscale so the longest edge is `width`, preserving aspect. Never upscales."""
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= width:
        return image
    k = width / longest
    return cv2.resize(image, (max(1, int(w * k)), max(1, int(h * k))),
                      interpolation=cv2.INTER_AREA)


def grid_origins(width: int, height: int, tile: int, overlap: int = 0) -> list[tuple[int, int]]:
    step = max(1, tile - overlap)
    xs = list(range(0, max(1, width - tile + 1), step))
    if xs[-1] != width - tile:
        xs.append(max(0, width - tile))
    ys = list(range(0, max(1, height - tile + 1), step))
    if ys[-1] != height - tile:
        ys.append(max(0, height - tile))
    return [(x, y) for y in ys for x in xs]


def iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def _area(box: list[float]) -> float:
    x0, y0, x1, y1 = box
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _intersection(a: list[float], b: list[float]) -> float:
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return iw * ih


def nms(boxes: list[dict], iou_thresh: float = 0.5, contain_thresh: float = 0.8) -> list[dict]:
    """Greedy NMS across all tiles' detections, already in full-frame coordinates.

    Plain IoU misses one real duplicate shape: a small box sitting almost
    entirely inside a much bigger one. Union is then dominated by the big box's
    area, so IoU stays low no matter how complete the containment -- seen in
    practice as two boxes on one bird, one full-body and one just its tail, both
    surviving a 0.5 threshold. So containment is suppressed as well.
    """
    kept: list[dict] = []
    for b in sorted(boxes, key=lambda b: -b["conf"]):
        b_area = _area(b["bbox"])
        duplicate = False
        for k in kept:
            if iou(b["bbox"], k["bbox"]) >= iou_thresh:
                duplicate = True
                break
            smaller = min(b_area, _area(k["bbox"]))
            if smaller > 0 and _intersection(b["bbox"], k["bbox"]) / smaller >= contain_thresh:
                duplicate = True
                break
        if not duplicate:
            kept.append(b)
    return kept


def detect_frame(image: np.ndarray, tile: int, infer_fn, overlap: int = 128,
                 nms_iou: float = 0.5, timing: dict | None = None) -> list[dict]:
    """Every box in the frame, in full-frame coordinates.

    `timing`, if given, is filled in place with wall-clock milliseconds for
    each internal stage (tile_ms, infer_ms, nms_ms) -- Phase 0 instrumentation
    (PERFORMANCE.md §1/§4). Optional and additive: every other caller (this
    repo has exactly one, an unrelated script in a different tree) passes
    None and pays zero timing overhead.
    """
    height, width = image.shape[:2]
    t0 = time.monotonic()
    origins = grid_origins(width, height, tile, overlap)
    tiles = [image[oy:oy + tile, ox:ox + tile] for ox, oy in origins]
    if timing is not None:
        timing["tile_ms"] = (time.monotonic() - t0) * 1000
    t1 = time.monotonic()
    boxes = []
    for (ox, oy), dets in zip(origins, infer_fn(tiles)):
        for d in dets:
            x0, y0, x1, y1 = d["bbox"]
            boxes.append({"bbox": [ox + x0, oy + y0, ox + x1, oy + y1],
                          "conf": d["conf"], "cls": d["cls"]})
    if timing is not None:
        timing["infer_ms"] = (time.monotonic() - t1) * 1000
    t2 = time.monotonic()
    kept = nms(boxes, nms_iou)
    if timing is not None:
        timing["nms_ms"] = (time.monotonic() - t2) * 1000
    return kept


def build_infer(weights: str, tile: int = 640, conf: float = 0.25, half: bool = True):
    """A tiles -> per-tile detections function for a finetuned YOLO model.

    half=True by default: FP16 was measured to cost no accuracy on this model
    while cutting inference time ~1.5x and VRAM ~46%, which is what makes more
    than a couple of camera processes fit on one GPU at all. Ultralytics falls
    back to FP32 by itself on a device without FP16 support.
    """
    import logging

    from ultralytics import YOLO  # imported lazily: --help shouldn't pay torch's import

    # Ultralytics warns about `half=` on every single predict call. At several
    # inferences a second that is thousands of identical lines a day in a log
    # that is meant to be read when a camera misbehaves. Drop just that message,
    # so everything else it has to say still gets through.
    class _DropHalfDeprecation(logging.Filter):
        def filter(self, record):
            return "'half' is deprecated" not in record.getMessage()

    logging.getLogger("ultralytics").addFilter(_DropHalfDeprecation())

    model = YOLO(weights)

    def infer(tiles):
        results = model.predict(tiles, imgsz=tile, conf=conf, half=half, verbose=False)
        return [[{"bbox": box.xyxy[0].tolist(), "conf": float(box.conf[0]),
                  "cls": r.names[int(box.cls)]} for box in r.boxes] for r in results]

    return infer


def draw_boxes(image: np.ndarray, boxes: list[dict], carried: bool = False) -> np.ndarray:
    """Detection boxes burned into a copy of the frame, for the live preview.

    `carried` boxes (propagated from the last inferred frame rather than
    measured on this one) are drawn in a dimmer colour and marked, so a viewer
    is never told the model saw something on a frame it never ran on.
    """
    out = image.copy()
    color = CARRIED_COLOR if carried else BOX_COLOR
    thickness = max(1, round(min(out.shape[:2]) / 500))
    for b in boxes:
        x0, y0, x1, y1 = (int(v) for v in b["bbox"])
        cv2.rectangle(out, (x0, y0), (x1, y1), color, thickness)
        label = f'{b["conf"]:.2f}' + ("~" if carried else "")
        cv2.putText(out, label, (x0, max(12, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * thickness, color, thickness)
    return out


def save_crop(image: np.ndarray, bbox: list[float], conf: float | None,
              out_path: Path) -> bool:
    """A zoomed, boxed crop around one detection, for the review strip."""
    x0, y0, x1, y1 = bbox
    cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
    h, w = image.shape[:2]
    half = ZOOM_NATIVE // 2
    cx0 = int(np.clip(cx - half, 0, max(0, w - ZOOM_NATIVE)))
    cy0 = int(np.clip(cy - half, 0, max(0, h - ZOOM_NATIVE)))
    patch = image[cy0:cy0 + ZOOM_NATIVE, cx0:cx0 + ZOOM_NATIVE]
    if patch.size == 0:
        return False
    if patch.shape[0] != ZOOM_NATIVE or patch.shape[1] != ZOOM_NATIVE:
        patch = cv2.copyMakeBorder(patch, 0, ZOOM_NATIVE - patch.shape[0],
                                   0, ZOOM_NATIVE - patch.shape[1], cv2.BORDER_CONSTANT)
    patch = cv2.resize(patch, (ZOOM_OUT, ZOOM_OUT), interpolation=cv2.INTER_NEAREST)
    k = ZOOM_OUT / ZOOM_NATIVE
    cv2.rectangle(patch, (int((x0 - cx0) * k) - 2, int((y0 - cy0) * k) - 2),
                  (int((x1 - cx0) * k) + 2, int((y1 - cy0) * k) + 2), BOX_COLOR, 1)
    if conf is not None:
        cv2.putText(patch, f"{conf:.2f}", (5, ZOOM_OUT - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, BOX_COLOR, 1)
    return write_jpeg(out_path, patch, 95)
