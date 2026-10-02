"""SegmentFeed's consumer side and the sequential extraction, with no NVR: a synthetic clip and a stub hub.

    python tests/test_segment_feed.py
"""
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import cv2
import numpy as np

from segment_feed import SegmentFeed

HUB = SimpleNamespace(nvr=SimpleNamespace(clock_offset=0.0), gate=None)


def make_clip(path: Path, seconds=6, fps=25):
    """A clip whose Nth second has brightness N*40, so extraction order is checkable."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 180))
    for i in range(seconds * fps):
        writer.write(np.full((180, 320, 3), min(255, (i // fps) * 40 + 10), np.uint8))
    writer.release()


def feed(tmp, **kw):
    return SegmentFeed("cam", "10.0.0.1", HUB, Path(tmp) / "seg", sample_interval=1.0, **kw)


def test_extract_keeps_one_frame_per_interval_in_time_order():
    with tempfile.TemporaryDirectory() as t:
        f, clip = feed(t), Path(t) / "c.mp4"
        make_clip(clip)
        start = time.time() - 20
        f._extract(clip, start)
        out = []
        while True:
            kind, image, wall = f.next_frame()
            if kind != "frame":
                break
            out.append((round(wall - start), int(image.mean()) // 40))
        assert [o[0] for o in out] == [0, 1, 2, 3, 4, 5]           # one per second, oldest first
        assert [o[1] for o in out] == [0, 1, 2, 3, 4, 5]           # and they are the right frames


def test_a_span_pulled_twice_does_not_queue_duplicates():
    with tempfile.TemporaryDirectory() as t:
        f, clip = feed(t), Path(t) / "c.mp4"
        make_clip(clip)
        start = time.time() - 20
        f._extract(clip, start)
        f._extract(clip, start)                                     # a retried span
        n = 0
        while f.next_frame()[0] == "frame":
            n += 1
        assert n == 6


def test_frames_older_than_the_backlog_are_dropped_not_queued():
    with tempfile.TemporaryDirectory() as t:
        f, clip = feed(t, max_backlog_s=10.0), Path(t) / "c.mp4"
        make_clip(clip)
        f._extract(clip, time.time() - 120)                         # all of it is 2 minutes old
        assert f.next_frame()[0] in ("caught_up", "none")
        assert f.dropped_stale == 6


def test_caught_up_is_not_no_data_until_stale():
    with tempfile.TemporaryDirectory() as t:
        f, clip = feed(t, stale_s=0.3), Path(t) / "c.mp4"
        make_clip(clip, seconds=2)
        f._extract(clip, time.time() - 5)
        while f.next_frame()[0] == "frame":
            pass
        assert f.next_frame()[0] == "caught_up"                     # nothing new yet, but recent data
        time.sleep(0.4)
        assert f.next_frame()[0] == "none"                          # nothing for stale_s: black
        assert not f.has_data()


def test_never_pulled_is_no_data():
    with tempfile.TemporaryDirectory() as t:
        assert feed(t).next_frame()[0] == "none"


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
