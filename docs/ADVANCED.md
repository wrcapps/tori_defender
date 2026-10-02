# Advanced notes

Background and details that aren't needed to install or use the app day to
day. See the top-level [README.md](../README.md) for that.

---

## The acquisition service (one process, all cameras)

Everything that watches cameras is **one process**, `backend/acquisition_service.py`. The app
starts and stops it for you; you can also run it by hand, headless:

```bash
./backend/acquisition_service.py --config config.yaml --enable-all
```

It loads the model once and works in **synchronised rounds**: when the first camera is due,
every camera due within `--sync-ms` (150) joins the round, each reader decodes its *next*
frame at that instant (so a round's frames were captured within about one frame interval of
each other -- reported as *capture sync*), and the frames are detected together: 12 cameras =
12 frames = 384 tiles. Results are then saved off the round thread, so writing a 4K JPEG never
delays the next round. Cameras stay on a shared time grid, so a fleet that starts together
stays together.

### What a detection saves

While **Acquisition is on, every camera records**: when the model finds a bird, the camera opens a window in
`inbox/` and saves the frames from `live.preroll_frames` idle frames *before* the detection (default 3, scan_fps
apart), through the detection, to `live.cooldown_seconds` after the last one (default 8; 3 in this install). The
window then waits in the Inbox for review (see DATA_LAYOUT.md). Pre-roll frames are held undecoded in memory (about
25 MB per frame per camera at 4K) and have no boxes; `window.json` records how many (`preroll_frames`).
Merely watching a stream, with Acquisition off and no Record flag, detects and notifies but saves nothing.

### The header button, and what it does not touch

| thing | what it controls |
|---|---|
| **Acquisition** (header, operator) | processes **every** camera: detection, notifications, sparse live previews, and **saves each detection's window** (frames before, during and after) to the inbox |
| **Record** (per camera) | the same saving for one camera while Acquisition is off |
| an open **stream** in Live | processes that camera so the view has boxes |

Any one is enough to keep the service running; with none of them it exits and returns its
VRAM. Enabling acquisition never sets or clears a recording flag, and recording works with
acquisition off (that camera alone is processed). `enabled` is remembered across an app
restart (`logs/acquisition/control.json`).

Notifications go out for cameras that are enabled or recording, one per *new* track and at
most one per camera every 5 s. They land in `dataset/detections/<bucket>/live_events.jsonl`
and in the bell as *Live detection*, marked **recorded** (with *Open interval*) or **not
recorded (recording is off)**. Nothing is saved without Record.

### The live view is sparse on purpose

The live view is a recent still, not video: `live.preview_fps` (default **2**) for a camera
someone is watching, `live.idle_preview_fps` (default 0.2) for the rest. Measured: a 15 s
stream gave 30 frames = 2.0 fps from a 25 fps camera.

### Why one process, and why chunks of 32 tiles  (RTX 4070 Ti, FP16, 4K frame = 32 tiles)

| 12-frame round, tiles per forward | time | torch peak | nvidia-smi total |
|---|---|---|---|
| 32 (default) | **470 ms** | 1014 MiB | **1741 MiB** |
| 96 | 517 ms | 2391 MiB | 3803 MiB |
| 384 (the whole round at once) | 544 ms | 8498 MiB | 10419 MiB |

Per frame, YOLO11s: 65.1 ms through the old `model.predict(tiles)` path, **37.8 ms** with tiling
and normalisation on the GPU (`batch_detector.GpuTiledYolo`; 253/253 boxes identical on all
630 drone frames). RF-DETR nano went 113 -> **31 ms/frame** the same way (GPU-resident tiling,
one batched forward in FP16, `batch_detector.GpuTiledRfdetr`; identical boxes on 60 real frames),
and live video is decoded on NVDEC instead of the CPU when an NVIDIA GPU is present. **Batching more frames into one forward does
not raise throughput** (YOLO 37.8 / 40.8 / 41.8 / 42.7 ms per frame at 1 / 2 / 3 / 4 frames):
one frame's 32 tiles already saturate the GPU. The round is synchronous, the network runs in
chunks (`--max-tiles`). The old design -- a model and CUDA context per camera, ~1.4 GB each --
needed ~17 GB for twelve cameras and never fit.

On an out-of-memory error the service halves the chunk and retries instead of losing the round.

### When live cannot be held: the NVR's recording, pulled in the background

A camera follows `live.source` (default `auto`):

1. **live** -- the RTSP stream, as above;
2. if it cannot be held (no connection for `live_giveup_s`, or `degrade_misses` rounds in a row
   without a frame) the camera drops to **segments**: short pieces of the NVR's recording are
   downloaded in the background (`segment_seconds`, at most `max_downloads` at once across all
   cameras), each decoded once start-to-end, and one frame per scan interval kept. Rounds take
   those frames in order, so detection runs on whatever has arrived. The RTSP connection is
   released so its bandwidth goes to the downloads, and live is retried every `live_retry_s`;
3. with **no data** (NVR unreachable, camera not on the NVR, nothing for `stale_s`) the camera
   is a plain **black** frame -- never a stale picture shown as current.

Needs a top-level block (credentials are `defaults`):

```yaml
nvr:
  host: 192.168.88.253
  scheme: https
```

Measured against the real NVR: a 10 s 4K clip is ~2.3 MB and downloads in 0.6 s; decoding it
sequentially costs 3.7 ms/frame (seeking instead gave 137-454 ms and broken frames, so it is not
used). In a test, a camera with a dead stream fell back after ~23 s, pulled 7 segments (42 MiB),
and was detected on at a 26 s lag, in the same rounds as live cameras.

Limits: segment frames are behind real time (segment length + download, typically 20-40 s), are
time-stamped from the segment's requested start (a clip begins on a keyframe, so up to ~1 s off),
and exist only at scan rate -- a recording window in segment mode is as sparse as scanning.
Notifications carry `lag_s` and `source`.

### Not covered

**Bandwidth.** Each camera pulls its 4K main stream (~6 Mbit/s); twelve over one VPN tunnel can
exceed the link whatever the GPU does. The button's panel shows the estimate. **Decode CPU:**
every frame of every active stream is decoded; this was not measured at twelve cameras.

## Running capture by hand (single camera, old script)

`live_capture.py` still works for one camera on its own, with its own model and the old
per-process behaviour; `live_capture_all.py` (a process per camera) is superseded by the
service above.

## Finding a stuck false positive

A single static false positive — a roof corner, an aerial, a lens mark — can
re-trigger every few seconds and hold a recording window open indefinitely,
which is the fastest way to fill a disk with pictures of nothing. Measured on
one site: a single building corner accounted for 71% of all detections in a
day and kept the recorder running continuously.

Group a run's detections by position to spot it — a bird wanders, a false
positive doesn't:

```python
import json, collections
rows = [json.loads(l) for l in open("sites/<site>/dataset/detections/live/detections.jsonl")
        if l.strip()]
cells = collections.Counter(
    (int((r["bbox"][0] + r["bbox"][2]) / 2 // 200) * 200,
     int((r["bbox"][1] + r["bbox"][3]) / 2 // 200) * 200)
    for r in rows if not r["carried"])
print(cells.most_common(5))     # one cell dominating = a static false positive
```

The fix is to teach the model, not to filter around it: keep a handful of
those frames as hard negatives and finetune.

## Where things end up, in full

```
sites/<site>/
  frames/<day>/<camera>-<HHMMSS>/     one capture window
      frame_0.jpg ...                 the saved frames, native resolution
      window.json                     written when the window closes
  live/<camera>/latest.jpg            what the Live view streams
  live/<camera>/recording             present = capture saves; absent = preview only
  dataset/detections/<bucket>/
      detections.jsonl                every box the model produced: frame, bbox,
                                       confidence, track -- never rewritten
      crops/                          a zoomed crop per detection
      verdicts.json                   {track: keep|drop|unsure}
      track_meta.json                 {track: {species, distance, size}}
      manual_boxes.jsonl              boxes added or followed by hand
      box_edits.jsonl                 corrections to the model's own boxes
      folder_status.json              which sessions are marked reviewed
      proxies/                        downscaled frames for the scrubber
```

`--sites-root` points all of this somewhere else — a NAS mount, or an
existing tree of captures. By default it sits next to the code.

Two notes on the formats, because they matter if anything downstream reads
them:

- `verdicts.json` values are plain strings. Training-export tooling matches
  them with `== "keep"`, so they must stay that way; species/distance/size
  notes live in `track_meta.json` for exactly that reason.
- Marking a session reviewed only writes a flag in `folder_status.json`.
  Nothing is ever moved on disk.

The app assumes one reviewer at a time — saves are last-writer-wins.

## Impact risk, alerts and what they decide

A track that has a **distance** gets an **impact risk** from it, and a decision is
written down for that risk:

| risk | default zone | decision |
|---|---|---|
| High   | closer than `high_below_m`   | turbine stopped |
| Medium | closer than `medium_below_m` | deterrent activated |
| Low    | further                      | no action |

```yaml
risk:
  turbine: "Turbine 1"     # name used in the alert sentence
  high_below_m: 100        # NOT AGREED YET -- these two are placeholders
  medium_below_m: 250
```

Until both thresholds are set, the app uses 100 m / 250 m and marks every risk it shows
as *placeholder zones*. A reviewer can override a track's risk in Review (impact risk
→ Low / Medium / High); the override beats the distance.

**Decisions are recorded, not carried out.** They go to `dataset/detections/<bucket>/alerts.json`
(`{track: {message, risk, decision, actuated: false, first_file, last_file, history, ...}}`)
and show in the bell as *"site, camera X detected a <species> at <d> m from <turbine>
(<Level> risk)"* with the decision. Nothing is connected to a deterrent or a turbine
controller; whatever should act on a decision reads `alerts.json`.

The bell's **Open interval** (operators) opens Review on that session at the first frame
of the track, with the track selected.

## Tracking

With `live.link_px > 0` (default 300) a box that lands where an open track of the same
capture window predicts it keeps that track's id, so one bird is one track across the
window instead of one per inference (`backend/tracker.py`). Review draws the selected
bird's trail: solid up to the current frame, dashed ahead, a bigger dot for now. Set
`link_px: 0` for the old one-id-per-box behaviour.

## Relationship to the research pipeline

Three files are trimmed copies of the larger pipeline this was built from,
kept here so the app stands alone:

`backend/camera_config.py` · `backend/model_infer.py` · `backend/sitepaths.py`

They will drift from the originals as camera quirks get fixed on one side
and not the other. When something odd turns up with a camera, that is the
list to check first.
