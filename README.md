# bird-review-app

Live bird detection from IP cameras, and a web tool for reviewing what the model
found.

Two things run here:

- **Capture** (`live_capture.py`) watches one camera's stream and runs a
  finetuned YOLO model on it, but never writes a frame to disk on its own —
  saving only starts once *recording* is switched on for that camera.
- **The app** (`app.py`) is a local web page with three views, switched from the
  header: **Review** for going through captured sessions — confirming or
  rejecting what the model found, drawing in what it missed, recording optional
  species / distance / size notes; **Live** for picking cameras across every
  configured site, watching them and switching each one's recording on or off;
  and **Dataset** for auditing the training labels themselves. Point it at more
  than one `--config` and it spans every site at once — Live groups cameras by
  site, Review gets a site switcher.
- The app starts a camera's capture process itself, the moment someone picks
  it in Live — see **Live**, below — rather than requiring every camera to be
  started by hand beforehand.

---

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml && chmod 600 config.yaml
```

Edit `config.yaml`: the site name, the camera list, and the path to the model
weights. `config.yaml` holds camera passwords and is in `.gitignore` — keep it
there.

### Client / reviewer-only setup (data on the shared Tori NAS)

A reviewer who only browses already-captured data (no cameras of their own)
can run:

```bash
./install.sh
```

It asks for the CIFS credentials for the Tori share (given to you separately —
not your own login), mounts it, creates the lowercase `corbu`/`babadag` site
aliases `sitepaths.py` needs (the NAS directories are capitalised), sets up the
venv, and writes a minimal `config.yaml`. See `install.sh`'s header comment for
exactly where credentials are and are not stored — never in the script itself.

---

## Open the app

```bash
./app.py --config config.yaml --weights weights/best.pt
./app.py --config config.corbu.yaml --config config.yaml --weights weights/best.pt   # every site
```

Then open http://127.0.0.1:8766/login. On a remote machine, forward the port
(VS Code's PORTS panel, or `ssh -L 8766:127.0.0.1:8766 user@host`) — the
server binds to localhost only.

### Sign in

Every page and every API now requires a login (see [client/README.md](client/README.md)
and [auth.py](auth.py)). First time only:

```bash
cp users.example.yaml users.yaml && chmod 600 users.yaml
python auth.py --users users.yaml --add-user alice --role operator
```

Two roles:

- **operator** — everything: Review, Live with recording, Dataset audit.
- **client** — read-only: Live (no recording toggle), Sightings, Species.
  Every mutating API refuses a client-role session outright, regardless of
  what any page shows — see `auth.py`'s docstring for why that check lives in
  one place rather than per-route.

Logging in as `operator` lands on the new client-facing app's **Live** page
(`/app/live`), which also links out to **Review** and **Dataset** — the
original tools below, unchanged and still reached at `/review` and
`/dataset` once signed in.

Pass `--config` once per site. Camera names must be unique across every config
given — they are how a stream request or a recording toggle finds its way back
to the right one. Each site keeps its own data under its own
`<sites-root>/<site>/...`, exactly as if you had run the app once per site.

`--weights` is what Live starts a camera's capture process with, for any site
whose own config doesn't set `model.weights` — without one, cameras in that
site show why in place of a status instead of starting. `--gpu-fps` (default
6.5, measured on an RTX 4070 Ti at FP16) is the same capacity math
`live_capture_all.py` uses, checked again — as a warning, not a refusal — every
time picking one more camera would push the running set over budget.

Before starting any camera, the app also checks *actual free VRAM* right then
(`nvidia-smi`) and refuses — a real refusal this time, not a warning — if
there isn't enough headroom to load one more model, rather than let it start
and crash with CUDA out-of-memory a few seconds in. This is a snapshot, not a
lock: if the GPU is shared with something else whose own usage is still
growing (another training run, say), it can still lose that race in the
second or two after the check passes. On a GPU with other jobs on it, don't
be surprised if a camera that started fine five minutes ago fails to start
now — that's the other job, not this one.

### Review

**The Site box** at the top switches which configured site Review shows —
sessions, verdicts and notes are entirely separate per site, so switching wipes
whatever was open and loads the other site's own queue.

**The left column** lists capture sessions, split into *to review* and
*reviewed manually*. Click one to open it; click **done** on it when you have
been through it, which moves it to the reviewed group. A session still being
written shows **REC** and cannot be marked done yet.

**The middle** is a frame scrubber. Step through consecutive frames with the
arrow keys (hold shift to jump ten) and watch how a detection moves — that is
usually what separates a bird from a lens mark or a scrap of cloud, and it is
not something a single still can show. Boxes are drawn in place on each frame.

**next uncertain** jumps to the lowest-confidence detection you have not judged
yet, anywhere in the session. Use it to find the work; use the scrubber to do
it. Going frame by frame from the start means scrolling past the same obvious
bird thirty times before reaching the one that actually needs a decision.

**The right column** carries the legend, a magnifier, the verdict buttons, and
the optional annotation fields.

| key | |
|---|---|
| `←` `→` | step one frame (`shift` = ten) |
| `Y` / `N` / `space` | real bird / false positive / unsure |
| `U` | jump to the next uncertain detection |
| `P` | add — click two corners to draw a box the model missed |
| `M` | move — drag any box to reposition it |
| `F` | follow — carry this box onto the frames ahead |
| `X` | out of scene — stop following |
| `Del` | delete the selected box |
| `Z` | undo the last verdict |
| `+` `-` `0` | zoom in, out, fit |
| `Esc` | leave the pencil |

**Following a bird.** Click a box, press `F`, then hold `→`. On each frame the
box is placed where that bird's own recent motion says it should be, so a bird
holding a steady line often needs no correction at all; drag it when it does,
and the correction feeds the next prediction. `X` when it leaves the scene.
Every box of one followed bird shares a `track`, so it reads as one animal
rather than forty unrelated boxes.

**Moving and deleting** work on the model's boxes too, not just yours. The
model's own output is never rewritten: a moved or removed detection is recorded
in `box_edits.jsonl` beside `detections.jsonl`, which stays exactly as the
detector produced it — that file is what every later comparison of one model
against another is measured against. Deleting a detection on one frame is a
different statement from a `N` verdict: the verdict judges the whole track.

Species, distance and size are always optional; nothing requires them. They
attach to whichever detection is selected, and save as you type. With the pencil
out they attach instead to the box you are about to draw — the fields clear when
you pick the pencil up, so a new box never inherits the previous one's notes.

---

### Live

Cameras are grouped by site. **Ticking one starts its capture process** —
`app.py` spawns `live_capture.py` for that camera itself, the moment the box is
checked, rather than requiring it to already be running. The model takes a few
seconds to load, shown as **starting…**; once it does, the feed is the frame
the model just processed, with its boxes drawn on. Unticking stops pulling the
stream, and the process itself stops a short while after — a page reload or a
momentary disconnect doesn't kill it outright, so the model isn't reloaded for
nothing over a blip.

This app never opens an RTSP connection or runs the model directly — the
subprocess it starts does that. So the fleet actually spending GPU time and
tunnel bandwidth is always exactly the fleet someone is watching or recording,
never merely every camera a config happens to list — which matters most for a
site like Babadag, where running all twelve at once would ask more of the GPU
than it has (see the capacity note below).

**Record** additionally decides whether any of it reaches disk, and keeps the
process running even with the box unticked. It is **off by default** for every
camera: watching and deciding something is worth keeping are different
moments. Click **record** to turn it on — from that moment `live_capture.py`
opens windows on detection exactly as described below, and they show up in
Review. Click it again to stop; a window already open closes immediately
rather than keep growing unwanted. The toggle is a small marker file
(`live/<camera>/recording`) that the app creates or removes — the actual gate
lives in `live_capture.py`, which polls it, so recording still works even
across a restart of the app itself, as long as the capture process it already
started keeps running.

A camera whose site has no model weights configured (see **Open the app**,
`--weights`) shows the reason in place of a status — nothing starts until one
is set.

### Dataset

Start the app with `--dataset path/to/data.yaml` and the third tab audits the
training set itself — every label, **worst-looking first**:

| queue | what it holds |
|---|---|
| out of bounds | the box leaves the tile; it cannot be a correct label |
| tiny | under 4px on an edge, smaller than any bird in this data |
| huge | over 35% of the tile — usually a real bird close to the camera, a different object from the distant ones the model is for |
| ordinary | nothing obviously wrong, ranked by distance from the typical bird size |

A set of 15,000 labels cannot be read end to end, and in file order the broken
ones are invisible among thousands of correct ones. Ranked, stopping half way
still means *"I have seen everything questionable"*.

Each cell is **one box**, not one tile — a tile with three birds can be right
about two of them. Click a cell to reject that label, click again to undo,
shift-click to see the whole tile with every box on it (yellow is the one in
question, blue the others) — which is also how you spot a bird nobody labelled.

Decisions go to `label_review.json` beside the dataset and change nothing on
their own. To produce a cleaned set:

```bash
./apply_label_review.py --data path/to/data.yaml --dry-run   # report first
./apply_label_review.py --data path/to/data.yaml             # writes <dataset>-clean
```

The original is never edited — the set a model was trained on has to stay as it
was, or that run can never be reproduced. Images are hard-linked, so the copy
costs kilobytes. A tile whose last label was rejected is **kept** with an empty
label file: if its only box was a false positive, the tile is exactly the
negative that teaches the mistake away.

`dataset_report.py` (in the research tree) prints the same statistics
non-interactively, including per-class counts and provenance.

---

## Capture from the cameras

Normally you don't run this by hand: **picking a camera in Live starts it**,
and it stops again once nobody is watching or recording it (see **Live**,
above). `live_capture.py` is what the app spawns to do that, one process per
camera — this section is for running it yourself instead, without the app: a
fixed always-on fleet, a headless box, or just watching one camera's own log.

One camera:

```bash
./live_capture.py --config config.yaml --camera corbu-1
```

Every camera in the config, one process each, restarted if they die:

```bash
./live_capture_all.py --config config.yaml
./live_capture_all.py --config config.yaml --cameras mast2-a,mast2-b   # a subset
```

Per-camera logs land in `logs/`.

Recording is off until switched on — from the app's Live tab if it's running,
or by hand (`touch <sites-root>/<site>/live/<camera>/recording`) if it's not —
a fresh `live_capture.py` runs inference and updates the live preview, but
saves nothing until then. Pass `--always-record` to skip the toggle entirely
and capture on every detection, the way earlier versions of this script always
did.

### What gets saved

While nothing is happening, the model runs every `scan_fps` seconds and **nothing
is written** — an empty sky costs no disk.

The first detection opens a **window**. For as long as that window is open every
sampled frame is saved, at the denser `save_fps`, whether or not that particular
frame had a box on it. The window closes after `cooldown_seconds` of quiet.

That is deliberate. Saving only the frames that fired gives you a scatter of
disconnected stills — enough to see that something was detected, not enough to
tell what it was, and impossible to play back. Keeping the frames on either side
is what makes a window reviewable and reconstructable as a clip.

### What it costs, and the ceiling you cannot argue with

Tiled 4K inference measures **~154 ms per frame at FP16** on an RTX 4070 Ti —
about **6.5 inferences per second for the whole GPU**, shared by every camera.
That is why the model does not run on every saved frame: it runs every
`infer_every_n` frames and the boxes are carried across the ones in between
(written as `"carried": true` and drawn dimmer, so nothing claims the model saw
a frame it never ran on).

Each camera asks the GPU for `scan_fps` while idle, and
`save_fps / infer_every_n` while in a window. `live_capture_all.py` adds this up
before starting anything and **refuses to start a fleet that does not fit**,
because going over does not crash — the processes just quietly fall behind and
write thinner windows than configured, which nobody notices until the playback
looks wrong months later.

Two things it does *not* solve:

- **Bandwidth.** Each process pulls a ~6 Mbit/s 4K stream continuously. Twelve of
  those over one VPN link is more than the link carries, whatever the GPU is
  doing. Run a subset.
- **Disk.** Measured on a 3840x2160 camera: a saved frame is **1.7 MB**, so at
  the default 4 fps a window costs about **400 MB for every minute it stays
  open**. A scene that keeps triggering therefore fills a disk quickly — watch
  what is actually opening the windows before leaving this running unattended
  (see below), and offload finished days.

The substream is not a way out of either: a bird is 10–45 px in the 4K frame,
which the substream shrinks to 2–7 px — below anything the model has been
trained on. It would simply stop finding them.

### Recording, and what actually decides to save a frame

A **window** opens the moment the model first fires while recording is on for
that camera, and every sampled frame is saved — see below — until
`cooldown_seconds` of quiet, or `max_window_seconds` total, closes it. Turning
recording off closes an open window immediately rather than letting it keep
growing after nobody asked for it.

### Check what is opening the windows

A single static false positive — a roof corner, an aerial, a lens mark — will
re-trigger every few seconds and hold a window open indefinitely, which is the
fastest way to fill a disk with pictures of nothing. Measured on Corbu on
2026-09-11: one building corner accounted for **71% of 1153 detections** and
kept the recorder running continuously.

Group a run's detections by position to spot it — a bird wanders, a false
positive does not:

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

The fix is to teach the model, not to filter around it: keep a handful of those
frames as hard negatives and finetune.

---

## Where things end up

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

`--sites-root` points all of this somewhere else — a NAS mount, or an existing
tree of captures. By default it sits next to the code.

Two notes on the formats, because they matter if anything downstream reads them:

- `verdicts.json` values are plain strings. Training-export tooling matches them
  with `== "keep"`, so they must stay that way; the species/distance/size notes
  live in `track_meta.json` for exactly that reason.
- Marking a session reviewed only writes a flag in `folder_status.json`. Nothing
  is ever moved on disk, so every path already recorded keeps resolving.

The app assumes one reviewer at a time — saves are last-writer-wins.

---

## Relationship to the research pipeline

Three files are trimmed copies of the larger pipeline this was built from, kept
here so the app stands alone:

`camera_config.py` · `model_infer.py` · `sitepaths.py`

They will drift from the originals as camera quirks get fixed on one side and
not the other. When something odd turns up with a camera, that is the list to
check first.
