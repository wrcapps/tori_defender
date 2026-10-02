# Desktop app

The same app in its own window (pywebview) instead of a browser tab.

    python desktop/launcher.py              # run from source
    python desktop/launcher.py --browser    # no pywebview: print/open the URL instead
    python desktop/build.py [--light]       # package (run ON the target OS; PyInstaller can't cross-compile)

Windows: `pip install -r requirements.txt -r desktop/requirements.txt`, then `python desktop\build.py`
-> `dist\BirdReview\BirdReview.exe`. `--light` leaves out torch/ultralytics/rfdetr (Review + Live UI only,
no capture). Put `config.yaml` (and the model weights it points to) next to the exe; a first run copies
`config.example.yaml` there.

## Decisions

- **One connection for all live tiles.** Browsers allow ~6 connections per host and an MJPEG `<img>`
  never ends, so the 12-camera wall starved every `/api` call and the UI froze. Tiles now draw from one
  multiplexed `GET /stream/multi?cams=a,b,c` (`app.py _stream_multi`, `client/src/lib/liveWall.js`).
  Single-camera `/stream/<cam>.mjpg` still exists (CameraDetail uses it: one connection).
  Measured with `tests/perf/` (12 cams, headless Chromium): before, 6/12 tiles got a frame and every
  `/api/whoami` probe timed out (5 s); after, 12/12 tiles, p50 2.3 ms / p95 ~10-15 ms, ~59 frames/s total, no
  long tasks, server ~3% CPU (~9% at 30 fps sources).
- **Auto sign-in for the local window** via a per-run token (`/desktop-login?t=`), no password prompt.
- **Acquisition service as a child of the same exe** (`BirdReview.exe --service ...`); it is stopped by
  closing its stdin (`--stop-on-stdin-eof`), which also ends it if the app dies. Works on Windows, where
  SIGTERM would be a hard kill.
- Data (config, sites/, logs/) lives next to the exe, not inside the bundle. So do `models/` (drop model files
  there) and `settings.yaml` (written by the first-run setup / Settings page: model, data folder).

## Not verified yet
Settings/first-run setup in the frozen build (the device probe imports torch in-process there instead of a child process).
Native window (no display on the dev box), anything on Windows, and the full (torch) build.
