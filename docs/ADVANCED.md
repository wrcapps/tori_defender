# Advanced notes

Background and details that aren't needed to install or use the app day to
day. See the top-level [README.md](../README.md) for that.

---

## Running capture by hand

Normally you don't run this yourself: **picking a camera in Live starts it**,
and it stops again once nobody is watching or recording it. `live_capture.py`
is what the app spawns to do that, one process per camera — this section is
for running it yourself instead: a fixed always-on fleet, a headless box, or
just watching one camera's own log.

One camera:

```bash
./backend/live_capture.py --config config.yaml --camera corbu-1
```

Every camera in the config, one process each, restarted if they die:

```bash
./backend/live_capture_all.py --config config.yaml
./backend/live_capture_all.py --config config.yaml --cameras mast2-a,mast2-b   # a subset
```

Per-camera logs land in `logs/`.

Recording is off until switched on — from the app's Live tab if it's running,
or by hand (`touch <sites-root>/<site>/live/<camera>/recording`) if it's not.
Pass `--always-record` to skip the toggle entirely and capture on every
detection.

### What gets saved

While nothing is happening, the model runs every `scan_fps` seconds and
**nothing is written** — an empty sky costs no disk.

The first detection opens a **window**. For as long as that window is open
every sampled frame is saved, at the denser `save_fps`, whether or not that
particular frame had a box on it. The window closes after `cooldown_seconds`
of quiet, or after `max_window_seconds` total.

Saving only the frames that fired a detection would give a scatter of
disconnected stills — enough to see something was detected, not enough to
tell what it was, and impossible to play back. Keeping the frames on either
side is what makes a window reviewable and reconstructable as a clip.

## GPU, bandwidth and disk budget

Tiled 4K inference measures **~154 ms per frame at FP16** on an RTX 4070 Ti —
about **6.5 inferences per second for the whole GPU**, shared by every
camera. That's why the model doesn't run on every saved frame: it runs every
`infer_every_n` frames and carries the last boxes across the ones in between
(written as `"carried": true` and drawn dimmer in Review).

Each camera asks the GPU for `scan_fps` while idle, and
`save_fps / infer_every_n` while in a window. `live_capture_all.py` adds this
up before starting anything and **refuses to start a fleet that doesn't
fit** — going over budget doesn't crash, the processes just quietly fall
behind and write thinner windows than configured, which nobody notices until
the playback looks wrong months later.

Two things this budget check does *not* cover:

- **Bandwidth.** Each process pulls a ~6 Mbit/s 4K stream continuously.
  Running many cameras over one shared link (e.g. a VPN) can exceed what the
  link carries even when the GPU has headroom. Run a subset if so.
- **Disk.** A saved 4K frame is about **1.7 MB**, so at the default 4 fps a
  recording window costs roughly **400 MB per minute it stays open**. A scene
  that keeps triggering fills a disk quickly — see below, and offload
  finished days.

The substream isn't a way around either limit: a bird is 10–45 px in the 4K
frame, which the substream shrinks to 2–7 px — below anything the model was
trained on. It would simply stop finding them.

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

## Relationship to the research pipeline

Three files are trimmed copies of the larger pipeline this was built from,
kept here so the app stands alone:

`backend/camera_config.py` · `backend/model_infer.py` · `backend/sitepaths.py`

They will drift from the originals as camera quirks get fixed on one side
and not the other. When something odd turns up with a camera, that is the
list to check first.
