#!/usr/bin/env python3
"""Watch one camera's stream, and keep the frames that have birds in them.

RECORDING IS OFF BY DEFAULT:
    The model runs and the live preview updates regardless, but nothing reaches
    disk until recording is switched on for this camera -- from the app's Live
    tab, or by hand (`touch <sites-root>/<site>/live/<camera>/recording`).
    Watching a feed and deciding it is worth capturing are different moments;
    saving by default would mean every idle hour of an unattended camera
    quietly becomes disk and detections nobody asked for. Pass --always-record
    to skip the toggle and capture on every detection, as this script used to.

WHAT IT ACTUALLY SAVES, AND WHY IT IS NOT JUST THE FRAMES THAT FIRED:
    While nothing is happening the model runs every `scan_fps` seconds and
    nothing is written at all -- an empty sky costs no disk. The first detection
    opens a WINDOW, and for as long as that window is open every sampled frame is
    saved, at the denser `save_fps`, whether or not that particular frame had a
    box on it.

    Saving only the frames that fired would produce a scatter of disconnected
    stills: enough to see that something was detected, not enough to tell a bird
    from a lens artefact, and impossible to play back. A bird is identified by
    how it moves, so the window keeps the frames on either side of the ones that
    fired. The window closes after `cooldown_seconds` of quiet, and saving stops.

WHY THE MODEL DOES NOT RUN ON EVERY SAVED FRAME:
    Tiled 4K inference measures ~154ms/frame at FP16 -- roughly 6.5 frames per
    second for the entire GPU, shared by every camera. Running it on all 8
    frames per second this saves is not physically possible, so it runs every
    `infer_every_n` saved frames and the last measured boxes are carried across
    the frames in between. Carried boxes are written with "carried": true and
    drawn dimmer, so nothing ever claims the model saw a frame it never ran on.

WHY FULL FRAMES AND NOT A VIDEO FILE:
    The review tool needs to show a box in its place on the whole frame, and the
    training pipeline needs native-resolution crops. Both would mean decoding a
    clip back out again. JPEG q95 keeps a frame visually lossless at about a
    fifteenth of the equivalent PNG.

    ./live_capture.py --config config.yaml --camera corbu-1
    ./live_capture.py --config config.yaml --camera corbu-1 --weights weights/best.pt
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import yaml

from camera_config import ROTATE_CODES, build_cameras, redact, select_cameras
from metrics_log import log_metric
from model_infer import (build_infer, build_infer_rfdetr, detect_frame, draw_boxes,
                         fit_width, save_crop, write_jpeg)
from layout import SiteLayout
from sitepaths import add_sites_root_argument, site_dir, site_root
from tracker import Linker

# Read by OpenCV when it builds each FFmpeg context. UDP RTSP drops packets on
# any real link and the decoder turns those into smeared, half-updated frames,
# which reads as lag rather than as loss; nobuffer/low_delay stop it from
# holding frames back to smooth playback, which is the opposite of what a
# detector wants.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                      "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")

# How long grab() may keep succeeding while retrieve() never once decodes a
# usable frame before this gives up on the connection and reconnects.
NO_DECODABLE_FRAME_TIMEOUT = 15.0


class Window:
    """One detection burst on disk: <frames-root>/<day>/<camera>-<HHMMSS>/.

    The camera name is part of the folder, not just the config: several cameras
    run as separate processes against one site, and two of them triggering in the
    same second would otherwise land in the same folder, overwrite each other's
    frame_0 and -- because each process numbers its own tracks from zero -- make
    two different birds share a verdict key.
    """

    def __init__(self, root: Path, camera: str, now: datetime, layout: SiteLayout | None = None):
        self.camera = camera
        self.day = now.strftime("%Y-%m-%d")
        base = f"{camera}-{now.strftime('%H%M%S')}"
        self.name = layout.unique_window(self.day, base) if layout else base   # unique in inbox, frames, trash
        self.dir = root / self.day / self.name
        suffix = 1
        # A restart within the same second (the supervisor relaunching a crashed
        # child, say) must not reopen a folder that already holds frames.
        while self.dir.exists():
            suffix += 1
            self.name = f"{base}-{suffix}"
            self.dir = root / self.day / self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.opened_at = time.time()
        self.opened_monotonic = time.monotonic()
        # monotonic, because the cooldown is a duration and wall-clock time can
        # step sideways under NTP -- which this site has already seen once.
        self.last_detection = time.monotonic()

    def next_path(self) -> Path:
        path = self.dir / f"frame_{self.frames}.jpg"
        self.frames += 1
        return path

    def close(self) -> None:
        """The marker that says this window is finished.

        Without it the review tool cannot tell a session that is done from one
        still being written, and would let a reviewer mark a half-captured
        window as fully reviewed.
        """
        (self.dir / "window.json").write_text(json.dumps({
            "camera": self.camera, "frames": self.frames,
            "opened_at": self.opened_at, "closed_at": time.time(),
        }, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--camera", default=None,
                    help="camera name or host (default: the config's only camera; "
                         "required if it has more than one)")
    ap.add_argument("--weights", default=None,
                    help="finetuned model weights (default: the config's model.weights)")
    ap.add_argument("--model-type", default=None, choices=["yolo", "rfdetr"],
                    help="default: the config's model.type, or yolo if unset")
    ap.add_argument("--rfdetr-variant", default=None,
                    choices=["nano", "small", "medium", "base", "large"],
                    help="only for --model-type rfdetr; default: the config's "
                         "model.variant, or nano if unset")
    ap.add_argument("--fp32", action="store_true",
                    help="disable FP16 inference (slower, ~2x the VRAM, no measured "
                         "accuracy gain -- for a GPU without FP16 support)")
    ap.add_argument("--no-preview", action="store_true",
                    help="do not write the live preview JPEG that live_view.py streams")
    ap.add_argument("--always-record", action="store_true",
                    help="save windows to disk on every detection, ignoring the app's "
                         "record toggle (the old, unconditional behaviour -- for running "
                         "this script on its own, without app.py)")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: <sites-root>/<site>/frames")
    ap.add_argument("--bucket", default="live",
                    help="detections land in dataset/detections/<bucket>/ (default: live)")
    add_sites_root_argument(ap)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cameras = select_cameras(build_cameras(cfg), args.camera)
    if len(cameras) > 1:
        print(f"{len(cameras)} cameras in this config; pass --camera to pick one "
              f"({', '.join(c.name for c in cameras)}), or use live_capture_all.py "
              f"to run all of them", file=sys.stderr)
        return 2
    camera = cameras[0]
    live = camera.live

    # WHY VIDEO IS ALWAYS AVAILABLE, EVEN WITH NO MODEL:
    #   Decoding a stream and writing the live preview needs a CPU/GPU decode
    #   path, never a loaded model or the VRAM one takes -- only detect_frame()
    #   below does. So a missing/unreadable weights file, or a GPU with no
    #   room left for one more model right now, degrades this process to
    #   video-only rather than refusing to run at all: run_model is forced
    #   False for the whole session (see the main loop), which means no boxes
    #   are ever produced, no window ever opens, and nothing is saved -- but
    #   the camera stays watchable, which is the whole point of a live view
    #   at a scale where not every camera can have a model loaded at once.
    model_cfg = cfg.get("model") or {}
    weights = args.weights or model_cfg.get("weights")
    model_type = args.model_type or model_cfg.get("type") or "yolo"
    rfdetr_variant = args.rfdetr_variant or model_cfg.get("variant") or "nano"
    infer_fn = None
    if not weights:
        print("no weights configured -- running video-only, no detection", flush=True)
    elif not Path(weights).exists():
        print(f"weights not found: {weights} -- running video-only, no detection",
              file=sys.stderr, flush=True)
        weights = None

    layout = SiteLayout(site_root(cfg, args.sites_root, args.config))
    layout.ensure()
    frames_root = args.out or layout.inbox       # new windows wait in the inbox until reviewed
    detections_root = site_dir(cfg, "dataset", "detections", base=args.sites_root,
                               source=args.config) / args.bucket
    crops_root = detections_root / "crops"
    detections_root.mkdir(parents=True, exist_ok=True)
    preview_path = (site_dir(cfg, "live", camera.name, base=args.sites_root,
                             source=args.config) / "latest.jpg")
    # The app's Live tab flips this file to say "save what you see"; its absence
    # means "preview only". Checked once per sampled frame (a cheap stat), not
    # once per preview tick, so toggling it takes effect within one sample
    # interval rather than instantly -- fine, since a person just clicked a
    # button and isn't timing it to the millisecond.
    recording_flag = preview_path.parent / "recording"
    # What the Live tab calls "live" needs more than one fresh frame: a link
    # that keeps dropping and reconnecting can still produce an occasional
    # frame in between drops, which would read as "live" on a connection that
    # is anything but. So the app is told not just THAT a frame arrived but
    # WHEN the current connection was established -- it only calls a camera
    # live once that has held for a few seconds without a reconnect.
    status_path = preview_path.parent / "status.json"
    # Phase 0 instrumentation (PERFORMANCE.md): the true capture-to-render
    # clock, distinct from latest.jpg's mtime -- the mtime already has
    # decode+tile+inference+encode baked into it, so it can't answer "how
    # stale is what's on screen" on its own. Written on every decoded frame,
    # not gated on a window, so idle-scan age is covered too (most of a
    # camera's runtime is idle).
    frame_ts_path = preview_path.parent / "frame_ts.json"
    metrics_path = preview_path.parent / "metrics.jsonl"

    scan_interval = 1.0 / float(live["scan_fps"])
    save_interval = 1.0 / float(live["save_fps"])
    preview_enabled = not args.no_preview
    preview_interval = 1.0 / float(live["preview_fps"])
    infer_every_n = int(live["infer_every_n"])
    cooldown = float(live["cooldown_seconds"])
    max_window = float(live["max_window_seconds"])
    quality = int(live["jpeg_quality"])
    rotation = ROTATE_CODES.get(camera.rotate)

    if weights:
        print(f"loading {weights} ({model_type})", flush=True)
        try:
            if model_type == "rfdetr":
                infer_fn = build_infer_rfdetr(weights, variant=rfdetr_variant,
                                              tile=int(live["tile"]), conf=float(live["conf"]))
            else:
                infer_fn = build_infer(weights, tile=int(live["tile"]), conf=float(live["conf"]),
                                       half=not args.fp32)
        except Exception as exc:
            # A model load can fail for reasons only discoverable by trying
            # (a corrupt checkpoint, a CUDA error, an actual out-of-memory the
            # caller's own headroom check didn't catch in time) -- none of
            # that is a reason to refuse the video this process was also
            # asked for. Degrade the same way a known-in-advance refusal does.
            print(f"could not load {weights}: {exc} -- running video-only, "
                  f"no detection", file=sys.stderr, flush=True)
            infer_fn = None

    print(f"[{camera.name}] {redact(camera.record_url)}", flush=True)
    print(f"  idle: {live['scan_fps']:g} fps scanned, nothing saved", flush=True)
    if preview_enabled:
        print(f"  live view refreshed at {live['preview_fps']:g} fps "
              f"(independent of how often the model runs)", flush=True)
    if infer_fn is not None:
        print(f"  in a window: {live['save_fps']:g} fps saved, model every "
              f"{infer_every_n} frame(s), closes after {cooldown:g}s quiet "
              f"or {max_window:g}s total", flush=True)
    else:
        print(f"  detection: OFF -- video only, nothing will be saved or recorded",
              flush=True)
    print(f"  frames     -> {frames_root}", flush=True)
    print(f"  detections -> {detections_root / 'detections.jsonl'}", flush=True)
    if not args.no_preview:
        print(f"  live view  -> {preview_path}", flush=True)
    if args.always_record:
        print("  recording  -> always on (--always-record)", flush=True)
    else:
        print(f"  recording  -> OFF until toggled on from the app's Live tab "
              f"(or: touch {recording_flag})", flush=True)
    print(f"  review + live view: ./app.py --config {args.config} --model {args.bucket}",
          flush=True)

    # The supervisor stops children with SIGTERM, which by default kills the
    # process outright -- leaving the open window with no window.json, so the
    # review tool can never tell it is finished. Turning it into the same
    # exception Ctrl-C raises means both paths run the same shutdown.
    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    manifest = (detections_root / "detections.jsonl").open("a", encoding="utf-8")
    window: Window | None = None
    track_ids = iter(range(10 ** 9))

    def fresh_track() -> int:
        return next(track_ids)

    link_px = float(live.get("link_px", 0) or 0)
    linker = Linker(fresh_track, min_gate_px=link_px) if link_px > 0 else None
    saved_frames = 0
    since_infer = 0
    carried: list[dict] = []
    backoff = 1.0

    def close_window(w: Window | None) -> None:
        if w is None:
            return
        w.close()
        print(f"  window {w.name} closed after {w.frames} frame(s)", flush=True)

    def write_status(connected_at: float, reconnects: int) -> None:
        tmp = status_path.with_name(f".{status_path.name}.tmp")
        tmp.write_text(json.dumps({"connected_at": connected_at, "reconnects": reconnects}),
                       encoding="utf-8")
        tmp.replace(status_path)

    def write_frame_ts(capture_ts: float) -> None:
        tmp = frame_ts_path.with_name(f".{frame_ts_path.name}.tmp")
        tmp.write_text(json.dumps({"capture_ts": capture_ts}), encoding="utf-8")
        tmp.replace(frame_ts_path)

    reconnects = -1   # the first successful connection is not a "re"-connect
    try:
        while True:
            capture = cv2.VideoCapture(camera.record_url, cv2.CAP_FFMPEG)
            if not capture.isOpened():
                print(f"cannot open {redact(camera.record_url)}; retrying in {backoff:.0f}s",
                      file=sys.stderr, flush=True)
                capture.release()
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            reconnects += 1
            write_status(time.time(), max(0, reconnects))
            print(f"connected to {redact(camera.record_url)}"
                  + (f" (reconnect #{reconnects})" if reconnects else ""), flush=True)
            backoff = 1.0
            last_sample = 0.0
            # grab() succeeding only means a packet arrived -- a degraded link
            # (measured: constant "First slice in a frame missing" HEVC errors
            # on one Babadag mast) can keep doing that forever while retrieve()
            # never once decodes a usable frame. Without a watchdog that looks
            # like a live process that is silently, permanently stuck: it never
            # errors, never reconnects, and never shows anything but
            # "starting...". Measured against wall-clock time actually spent
            # trying, not a frame count, since how often retrieve() is even
            # attempted depends on save/preview cadence.
            last_good_frame = time.monotonic()

            try:
                last_preview = 0.0
                while True:
                    # grab() takes the next frame off the socket WITHOUT decoding
                    # it. Decoding every frame of a 25 fps 4K stream in order to
                    # use one of them per second costs more CPU than the model
                    # does, and once decoding cannot keep up the loop falls
                    # steadily further behind the camera -- which is exactly what
                    # a laggy live view is. Only the frames actually wanted are
                    # decoded, by retrieve() below.
                    if not capture.grab():
                        print("stream ended; reconnecting in 2s", file=sys.stderr, flush=True)
                        break

                    now = time.monotonic()
                    cadence = save_interval if window else scan_interval
                    want_sample = now - last_sample >= cadence
                    want_preview = preview_enabled and now - last_preview >= preview_interval
                    if not (want_sample or want_preview):
                        continue

                    t_decode0 = time.monotonic()
                    ok, frame = capture.retrieve()
                    decode_ms = (time.monotonic() - t_decode0) * 1000
                    if not ok or frame is None:
                        if now - last_good_frame > NO_DECODABLE_FRAME_TIMEOUT:
                            print(f"no decodable frame in {NO_DECODABLE_FRAME_TIMEOUT:g}s "
                                  f"(the link is up but every frame is failing to decode); "
                                  f"reconnecting", file=sys.stderr, flush=True)
                            break
                        continue
                    last_good_frame = now
                    if rotation is not None:
                        frame = cv2.rotate(frame, rotation)

                    # Every decoded frame gets a capture timestamp, not just
                    # saved ones -- this is what lets a viewer's displayed
                    # "age" stay honest between saves, which matters most on
                    # Babadag's degraded link (PERFORMANCE.md §2.b).
                    capture_ts = time.time()
                    t_fts0 = time.monotonic()
                    write_frame_ts(capture_ts)
                    frame_ts_write_ms = (time.monotonic() - t_fts0) * 1000

                    # A preview-only frame shows the boxes from the last frame the
                    # model did run on, so the view keeps moving between
                    # inferences instead of freezing on one still per second.
                    run_model = False
                    boxes = carried

                    if not want_sample:
                        last_preview = now
                        write_jpeg(preview_path,
                                   draw_boxes(fit_width(frame, int(live["preview_width"])),
                                              rescale(boxes, frame, int(live["preview_width"])),
                                              carried=True), 80)
                        continue

                    last_sample = now
                    # Idle frames are always inferred -- that is the only thing
                    # that can open a window. Inside a window the model runs on
                    # every infer_every_n-th frame and the rest carry its boxes.
                    # With no model loaded (video-only mode) this is always
                    # False: boxes stays permanently empty, so no window ever
                    # opens and nothing is ever saved -- the video keeps
                    # playing regardless, which is the point of this mode.
                    run_model = infer_fn is not None and (
                        window is None or since_infer % infer_every_n == 0)
                    infer_timing: dict = {}
                    if run_model:
                        boxes = detect_frame(frame, int(live["tile"]), infer_fn,
                                             int(live["overlap"]), float(live["nms_iou"]),
                                             timing=infer_timing)
                        # A track id is given when the model measures a box, and the
                        # frames that carry it reuse it. With link_px > 0 a box that
                        # lands where an open track of the SAME window predicts it
                        # keeps that track's id, so one bird is one track across the
                        # whole window rather than one per inference. An idle scan
                        # (no window open) starts from a clean linker.
                        if linker is not None:
                            if window is None:
                                linker = Linker(fresh_track, min_gate_px=link_px)
                            linker.update(boxes)
                        else:
                            for b in boxes:
                                b["track"] = fresh_track()
                        carried = boxes
                    else:
                        boxes = carried
                    since_infer += 1

                    recording = args.always_record or recording_flag.exists()
                    if window is not None and not recording:
                        # The button was switched off mid-window: stop right
                        # here rather than let an unwanted window keep growing
                        # until the next natural cooldown.
                        print(f"  window {window.name} stopped early: recording "
                              f"turned off", flush=True)
                        close_window(window)
                        window = None
                        carried = []
                        since_infer = 0

                    stamp = datetime.now()
                    if boxes and run_model and recording:
                        if window is None:
                            window = Window(frames_root, camera.name, stamp, layout)
                            # 1, not 0: the model has just run on this very
                            # frame, so the next one carries its boxes.
                            since_infer = 1
                            print(f"[{stamp:%H:%M:%S}] window {window.name} opened "
                                  f"({len(boxes)} detection(s), best "
                                  f"{max(b['conf'] for b in boxes):.2f})", flush=True)
                        window.last_detection = time.monotonic()

                    encode_ms = None
                    if window is not None:
                        path = window.next_path()
                        t_enc0 = time.monotonic()
                        write_jpeg(path, frame, quality)
                        encode_ms = (time.monotonic() - t_enc0) * 1000
                        saved_frames += 1
                        for b in boxes:
                            crop_rel = None
                            # Only a measured box gets a crop rendered: a carried
                            # one would be a near-duplicate image of the same
                            # bird, for a box the model never actually ran on.
                            if run_model:
                                crop_path = (crops_root / window.day / window.name /
                                             f"{path.stem}-t{b['track']:04d}.jpg")
                                if save_crop(frame, b["bbox"], b["conf"], crop_path):
                                    crop_rel = str(crop_path.relative_to(crops_root.parent))
                            manifest.write(json.dumps({
                                "day": window.day, "window": window.name, "file": path.name,
                                "bbox": b["bbox"], "conf": b["conf"], "track": b["track"],
                                "camera": camera.name, "carried": not run_model,
                                "vx": 0.0, "vy": 0.0, "saved": True, "crop": crop_rel,
                                "capture_ts": capture_ts,
                            }) + "\n")
                        manifest.flush()

                        if time.monotonic() - window.last_detection > cooldown:
                            close_window(window)
                            window = None
                            carried = []
                            since_infer = 0
                        elif time.monotonic() - window.opened_monotonic > max_window:
                            # A busy scene can trigger something every few
                            # seconds for hours, and a window that never closes
                            # is both un-reviewable (it is never "done") and
                            # unbounded on disk -- measured at ~190 MB/minute on
                            # a 4K camera. Split it instead: the next detection
                            # opens a fresh window, so nothing is lost.
                            print(f"  window {window.name} hit {max_window:g}s; "
                                  f"splitting after {window.frames} frame(s)", flush=True)
                            close_window(window)
                            window = None
                            since_infer = 0

                    log_metric(
                        metrics_path, "stage_latency", print_line=False,
                        camera=camera.name,
                        decode_ms=round(decode_ms, 2),
                        frame_ts_write_ms=round(frame_ts_write_ms, 2),
                        tile_ms=round(infer_timing.get("tile_ms", 0.0), 2),
                        infer_ms=round(infer_timing.get("infer_ms", 0.0), 2),
                        nms_ms=round(infer_timing.get("nms_ms", 0.0), 2),
                        encode_ms=None if encode_ms is None else round(encode_ms, 2),
                        run_model=run_model,
                    )

                    if preview_enabled:
                        last_preview = time.monotonic()
                        write_jpeg(preview_path,
                                   draw_boxes(fit_width(frame, int(live["preview_width"])),
                                              rescale(boxes, frame, int(live["preview_width"])),
                                              carried=not run_model), 80)
            finally:
                capture.release()
            time.sleep(2.0)
    except KeyboardInterrupt:
        print(f"\nstopped -- {saved_frames} frame(s) saved under {frames_root}")
    finally:
        # Also covers an unexpected failure mid-window (a full disk, say): a
        # window with no marker would otherwise stay un-reviewable.
        close_window(window)
        manifest.close()
    return 0


def rescale(boxes: list[dict], frame, width: int) -> list[dict]:
    """Boxes in the preview's coordinates. The preview is downscaled for
    bandwidth, so native-coordinate boxes would be drawn off the edge of it."""
    longest = max(frame.shape[:2])
    if longest <= width:
        return boxes
    k = width / longest
    return [{**b, "bbox": [v * k for v in b["bbox"]]} for b in boxes]


if __name__ == "__main__":
    raise SystemExit(main())
