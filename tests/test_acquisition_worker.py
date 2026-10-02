"""The per-camera state machine of acquisition_service, fed synthetic frames: no camera, no GPU.

    python tests/test_acquisition_worker.py
"""
import json
import sys
import tempfile
import threading
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np

import acquisition_service as svc
from camera_config import build_cameras


def make(tmp: Path, always_record=False):
    cfg = {"site": "t", "cameras": [{"name": "cam-a", "host": "10.0.0.1"}],
           "defaults": {"username": "u", "password": "p",
                        "record_url": "rtsp://{host}/x"}}
    camera = build_cameras(cfg)[0]
    site = svc.SiteCtx("t", cfg, tmp / "frames", tmp / "det", tmp / "live")
    site.detections_root.mkdir(parents=True)
    worker = svc.CameraWorker(camera, site, Namespace(always_record=always_record))
    worker.notify = True
    return worker, site


def snap(w=640, h=360):
    return svc.Snapshot(np.zeros((h, w, 3), np.uint8), 1000.0, 1.0)


INFO = {"age_ms": 5.0, "skew_ms": 1.0, "frames": 1, "round_ms": 50.0}
BOX = lambda x: [{"bbox": [x, 100.0, x + 40, 140.0], "conf": 0.9, "cls": "bird"}]


def rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def test_not_recording_notifies_but_saves_nothing():
    """Watching a stream (no Acquisition, no Record flag) detects and notifies; nothing reaches the disk."""
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.process(snap(), BOX(100), {}, INFO)
        assert w.window is None and not list((Path(t) / "frames").rglob("*.jpg"))
        assert rows(site.detections_root / "detections.jsonl") == []
        ev = rows(site.events_path)
        assert len(ev) == 1 and ev[0]["recording"] is False and "file" not in ev[0]


def test_acquisition_on_records_without_the_per_camera_flag():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.acq_enabled = True                                         # the header switch
        w.process(snap(), BOX(100), {}, INFO)
        assert w.window is not None and w.window.dir.parent.parent == Path(t) / "frames"
        ev = rows(site.events_path)
        assert ev[0]["recording"] is True and ev[0]["window"] == w.window.name and ev[0]["file"].endswith(".jpg")


def test_window_starts_before_the_detection_and_runs_on_after_it():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.acq_enabled = True
        for _ in range(5):                                           # nothing in view: only the ring fills
            w.process(snap(), [], {}, INFO)
        assert w.window is None and len(w.preroll) == w.preroll_frames
        w.process(snap(), BOX(100), {}, INFO)                        # the bird is detected
        assert w.window is not None
        files = sorted(p.name for p in w.window.dir.glob("frame_*.jpg"))
        assert files == ["frame_0.jpg", "frame_1.jpg", "frame_2.jpg", "frame_3.jpg"]   # 3 before + the detected one
        det = rows(site.detections_root / "detections.jsonl")
        assert [d["file"] for d in det] == ["frame_3.jpg"]           # boxes only where the model saw the bird
        w.process(snap(), None, {}, INFO)                            # after it: frames keep being saved
        assert (w.window.dir / "frame_4.jpg").exists()
        win = w.window
        w.close_window()
        assert w.window is None and (win.dir / "window.json").exists()   # closed: reviewable


def test_preroll_is_written_into_window_json_and_ring_is_not_replayed():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.acq_enabled = True
        for _ in range(3):
            w.process(snap(), [], {}, INFO)
        w.process(snap(), BOX(100), {}, INFO)
        win = w.window
        w.close_window()
        assert json.loads((win.dir / "window.json").read_text())["preroll_frames"] == 3
        assert len(w.preroll) == 0                                   # a second window must not re-save them


def test_no_preroll_when_not_recording():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        for _ in range(4):
            w.process(snap(), [], {}, INFO)
        assert len(w.preroll) == 0                                   # nothing is held in memory for nothing


def test_recording_opens_window_saves_and_event_links_to_it():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.recording_flag.touch()
        w.process(snap(), BOX(100), {}, INFO)
        assert w.window is not None
        det = rows(site.detections_root / "detections.jsonl")
        assert len(det) == 1 and det[0]["window"] == w.window.name and not det[0]["carried"]
        ev = rows(site.events_path)
        assert ev[0]["recording"] is True and ev[0]["window"] == w.window.name and ev[0]["file"] == "frame_0.jpg"
        # a following frame with no model run is carried and still saved into the open window
        w.process(snap(), None, {}, INFO)
        det = rows(site.detections_root / "detections.jsonl")
        assert [d["carried"] for d in det] == [False, True]
        assert (w.window.dir / "frame_1.jpg").exists()


def test_same_bird_keeps_its_track_and_notifies_once():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        for x in (100, 130, 160, 190):
            w.process(snap(), BOX(x), {}, INFO)
            w.last_event_mono = 0.0                      # remove the cooldown: only the track decides
        assert len(rows(site.events_path)) == 1


def test_recording_switched_off_closes_the_window():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.recording_flag.touch()
        w.process(snap(), BOX(100), {}, INFO)
        win = w.window
        w.recording_flag.unlink()
        w.process(snap(), None, {}, INFO)
        assert w.window is None and (win.dir / "window.json").exists()


def test_notify_off_means_no_event_even_with_detections():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.notify = False
        w.process(snap(), BOX(100), {}, INFO)
        assert rows(site.events_path) == []


def test_busy_flag_is_cleared_even_if_processing_fails():
    with tempfile.TemporaryDirectory() as t:
        w, site = make(Path(t))
        w.busy.set()
        w.recording_flag.touch()                                    # so the frame must be encoded
        w.process(svc.Snapshot(None, 0.0, 0.0), BOX(1), {}, INFO)   # a broken frame: encode raises
        assert not w.busy.is_set() and w.last_error


class _Cap:
    def __init__(self, failure):
        self.failure = failure


def test_unreachable_camera_never_moves_it_to_the_cpu_decoder():
    with tempfile.TemporaryDirectory() as t:
        w, _ = make(Path(t))
        w.decoder, w.decoder_pref = "nvdec", "auto"
        for _ in range(10):
            w._note_open_failure(_Cap("stream"))        # the camera is down, NVDEC is fine
        assert w.decoder == "nvdec" and w.nvdec_failures == 0


def test_stream_nvdec_cannot_decode_falls_back_after_three_tries():
    with tempfile.TemporaryDirectory() as t:
        w, _ = make(Path(t))
        w.decoder, w.decoder_pref = "nvdec", "auto"
        for _ in range(2):
            w._note_open_failure(_Cap("nvdec"))
        assert w.decoder == "nvdec"
        w._note_open_failure(_Cap("nvdec"))
        assert w.decoder == "cpu"


def test_forced_nvdec_never_falls_back():
    with tempfile.TemporaryDirectory() as t:
        w, _ = make(Path(t))
        w.decoder, w.decoder_pref = "nvdec", "nvdec"
        for _ in range(10):
            w._note_open_failure(_Cap("nvdec"))
        assert w.decoder == "nvdec"


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("ok  ", name)
            except Exception:
                failed += 1; print("FAIL", name); traceback.print_exc()
    sys.exit(1 if failed else 0)
