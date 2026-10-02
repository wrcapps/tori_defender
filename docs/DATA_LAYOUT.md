# Data layout

Every file this app writes or reads lives in exactly one place, decided in
[`backend/layout.py`](../backend/layout.py). No other module builds those paths by hand —
`tests/test_layout.py` fails if one does, and fails if this document stops naming something the
code knows. The rules for moving and deleting are in [`backend/lifecycle.py`](../backend/lifecycle.py).

```
python backend/lifecycle.py check   --sites-root sites --site <site>    # read-only; exit 1 on an error
python backend/lifecycle.py migrate --sites-root sites --site <site>    # dry run; --apply to write
python backend/lifecycle.py sweep   --sites-root sites --site <site>    # dry run; --apply to act
python backend/lifecycle.py restore --sites-root sites --site <site> DAY WINDOW
```

## 0. Hot and archive

`sites/<site>/` next to the app is the **local, hot** tree: capture writes there and Review reads there, so
neither waits for the NAS (measured on this setup: 75 ms to read a 1.8 MB frame, 91 ms to write one, ~22 MB/s;
twelve cameras need about four times that). The data folder chosen in Settings (`data_dir`, or `--archive-root`) is
the **archive** (the NAS), laid out the same way: `<archive>/<site>/frames/...`. `backend/archive.py`, a
separate process the app starts, moves finished data there whenever the NAS answers:

| what | when | how |
|---|---|---|
| `frames/<day>/<window>` (confirmed) | as soon as it is confirmed | copy to a temporary name, rename on the NAS, compare file names and sizes, then remove the local copy |
| `negatives/<day>/<file>` | older than 24 h | the same, per file |
| records (`*.json`, `*.jsonl`, `*.csv` of each bucket) | when changed | **backup** copy; the records stay local |

A window is found wherever it is (`SiteLayout.window_dir`: inbox, local frames, archive), so nothing else needs to
know. Rejecting an archived window deletes it on the NAS, in place. If the NAS is down, nothing stops: data waits
locally and the header shows `NAS down` with how much is waiting. Crops and proxies stay local.

## 1. The tree

```
<home>/                          next to the exe, or the repo root
├── config.yaml                  site, cameras (with passwords), model, retention        chmod 600
├── users.yaml                   logins (password hashes)                                chmod 600
├── logs/                        process logs; logs/acquisition/{control,status}.json
└── sites/<site>/
    ├── live/<camera>/           EPHEMERAL previews, rewritten in place: latest.jpg, status.json,
    │                            frame_ts.json, metrics.jsonl, recording (a marker file: "save what you see")
    ├── inbox/<day>/<window>/    PENDING — detections waiting for review. Capture writes here, and only here.
    │                            frame_N.jpg + window.json. Waits for review; expires only if retention.inbox_days > 0 (default 0 = never).
    │                            A window starts a few frames BEFORE the detection (window.json: preroll_frames) and
    │                            ends a few seconds after it.
    ├── frames/<day>/<window>/   CONFIRMED — kept permanently. Same shape as the inbox. Also holds
    │                            imported sessions (Excel import) and all footage from before this layout.
    ├── trash/<purge-date>/<day>/<window>/
    │                            REJECTED or EXPIRED. Recoverable until <purge-date>
    │                            (retention.trash_days, default 0 = deleted immediately on rejection), then deleted.
    ├── negatives/<day>/<camera>-<HHMMSS>.jpg
    │                            hard negatives: single frames the detector found NOTHING in, a few per camera per
    │                            period (live: negatives_per_period / negatives_period_s, default 5 per 2 h). Reviewed and
    │                            deleted in the Dataset page; "send to review" turns one into an inbox window.
    ├── backups/<timestamp>/     copies of the records, written by `lifecycle.py migrate --apply`
    └── dataset/detections/<bucket>/     RECORDS of one model bucket (live, drone, …)
        ├── detections.jsonl     what the detector produced; append-only (only the Excel importer rewrites its own bucket)
        ├── live_events.jsonl    bell notifications; append-only
        ├── lifecycle.jsonl      audit trail: every window move and every review decision (see §3)
        ├── verdicts.json        {track_id: "keep"|"drop"|"unsure"}  — flat strings, the training export matches them
        ├── track_meta.json      {track_id: {species, distance, size}}
        ├── manual_boxes.jsonl   boxes a person drew
        ├── box_edits.jsonl      corrections to model boxes
        ├── folder_status.json   {day/window: {reviewed: true}}   legacy tag
        ├── alerts.json          decision taken per track
        ├── drone_summary.csv    written by the Excel importer
        ├── crops/<day>/<window>/    per-detection crops   (removed with the window at purge)
        └── proxies/<day>/<window>/  downscaled frames for the scrubber (removed with the window at purge)
```

Anything else under a site or a bucket is reported by `lifecycle.py check`.

## 2. Names

| thing | form | example |
|---|---|---|
| `<day>` | `YYYY-MM-DD`, imports may add a tag | `2026-09-30`, `2026-09-30-drona` |
| `<window>` | `<camera>-<HHMMSS>[-<n>]`; imports `<camera>-d<dist>m-r<row>-<HHMMSS>` | `c2-48-143201` |
| window identity | `<day>/<window>` — **never changes**, wherever the window is | `2026-09-30/c2-48-143201` |
| model track id | `<day>/<window>/t%04d` | `2026-09-30/c2-48-143201/t0003` |
| manual track id | `<day>/<window>/m…` | `…/mt0001`, `…/m000007` |
| box edit key | `<day>/<window>/<file>#<track>` | `…/frame_0.jpg#72` |

`<window>` is unique across inbox, frames **and** trash (capture asks `SiteLayout.unique_window`), so a repeated
`HHMMSS` — a restart in the same second, the autumn clock change — can never reuse a window's identity.

## 3. Window lifecycle

```
 capture ─► inbox ─ a track is confirmed ───────────────────────► frames           (permanent)
              │  ─ every track rejected, no hand work ─────────► trash/<date> ─► deleted after trash_days (default: at once)
              │  ─ nobody looked at it for inbox_days (off by default) ► trash/<date> ─► deleted after trash_days
              └─ unsure / only some tracks decided / hand work: stays, and does not expire
```

* **Location is state.** `lifecycle.jsonl` is the audit trail, not a second truth. A move is one `os.rename`
  inside the site (never a copy across filesystems); the log line follows. Because the identity does not change, no
  record that mentions a window is ever rewritten when it moves.
* **Decided only when closed.** Capture writes `window.json` when, and only when, it closes a window. A window
  without it is still being written (or its capture died) and is never moved or expired; `check` reports it.
* **Protected from expiry:** any verdict (including `unsure`), species/distance/size typed in, manual boxes, box
  edits. **Protected from rejection:** manual boxes and box edits. Imported windows (`excel_row` in `window.json`)
  and everything in `frames/` are never touched by the sweep.
* **Records of a trashed or purged window are hidden** (sightings, species, the bell, `/api/track`). Purge deletes the
  window, its crops and proxies in every bucket. `detections.jsonl` is not rewritten; readers filter.
* **Deleting is two steps** (trash, then purge) and a run moves at most 50 windows.

`lifecycle.jsonl` lines — window moves: `{ts, day, window, from, to, reason, by}` with `to` one of
`confirmed|pending|trash|purged` and `reason` one of `keep|rejected|expired|retention|restored|grandfathered`;
review decisions: `{ts, event:"review", day, window, track, decision, reason, note, by}`.

## 4. Rules for code

1. Capture writes only to `inbox/`, `live/` and the records. Nothing but `lifecycle.py` moves or deletes a window.
2. Resolve a window with `SiteLayout.window_dir(day, window)` / `Site.window_root(day, window)`; never
   `frames / day / window`.
3. Records that rewrite themselves (verdicts, track_meta, folder_status, alerts) write atomically (tmp + rename).
4. A tool that deletes frames in bulk (`prune_empty_frames.py` in the research tree) must never be pointed at `inbox/`.
5. New file or directory? Add it to `layout.py`, to this document, and make `tests/test_layout.py` pass.

## 5. Migrating an existing site

`lifecycle.py migrate` moves **no footage**. Everything already in `frames/` is confirmed by definition and is only
*grandfathered* (one log line each): putting old footage in `inbox/` would let it expire. `--apply` first copies every
record file to `backups/<timestamp>/` with a sha256 manifest. It is idempotent.
