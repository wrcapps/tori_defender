import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import captured
from layout import SiteLayout

DAY = "2026-10-02"


def make(tmp_path):
    lay = SiteLayout(tmp_path / "s")
    lay.ensure()
    (lay.records / "live").mkdir(exist_ok=True)
    return lay, lay.records / "live"


def window(root, name, closed=True, extra=None):
    d = root / DAY / name
    d.mkdir(parents=True)
    (d / "frame_0.jpg").write_bytes(b"x")
    if closed:
        (d / "window.json").write_text(json.dumps({"camera": "c", "frames": 1, "closed_at": time.time(), **(extra or {})}))
    return d


def detect(bucket, name):
    with (bucket / "detections.jsonl").open("a") as f:
        f.write(json.dumps({"day": DAY, "window": name, "file": "frame_0.jpg", "bbox": [0, 0, 1, 1], "conf": .9, "track": 1}) + "\n")


def negative(lay, camera="c1", hms="101500"):
    p = lay.negatives / DAY / f"{camera}-{hms}.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_groups(tmp_path):
    lay, b = make(tmp_path)
    window(lay.inbox, "new-1"), detect(b, "new-1")                     # nobody judged it
    window(lay.frames, "kept-1"), detect(b, "kept-1")
    (b / "verdicts.json").write_text(json.dumps({f"{DAY}/kept-1/t0001": "keep"}))
    window(lay.inbox, "open-1", closed=False)                           # capture still writing: not offered
    window(lay.frames, "imp-1", extra={"excel_row": 2})                 # imports are not offered
    negative(lay)
    assert captured.summary(lay)["groups"] == {"unreviewed": 1, "with_detections": 1, "no_detections": 1}
    assert [i["id"] for i in captured.items(lay, "unreviewed")] == [f"{DAY}/new-1"]


def test_delete_is_direct_and_refuses_open_windows(tmp_path):
    lay, b = make(tmp_path)
    d = window(lay.inbox, "new-1")
    crops = b / "crops" / DAY / "new-1"
    crops.mkdir(parents=True)
    (crops / "c.jpg").write_bytes(b"x")
    open_ = window(lay.inbox, "open-1", closed=False)
    res = captured.delete_windows(lay, [f"{DAY}/new-1", f"{DAY}/open-1", "../etc/passwd"], b, "t")
    assert res["deleted"] == [f"{DAY}/new-1"] and len(res["skipped"]) == 2
    assert not d.exists() and not crops.exists() and open_.exists()
    neg = negative(lay)
    assert captured.delete_negatives(lay, [f"{DAY}/{neg.name}", "x/../y.jpg"])["deleted"] == [f"{DAY}/{neg.name}"]
    assert not neg.exists()


def test_negative_sent_to_review_becomes_a_protected_inbox_window(tmp_path):
    import lifecycle
    from layout import Retention
    lay, b = make(tmp_path)
    neg = negative(lay)
    wid = captured.negative_to_review(lay, f"{DAY}/{neg.name}", b, "t")
    day, name = wid.split("/")
    assert lay.state(day, name) == "pending" and not neg.exists()
    assert (lay.inbox / day / name / "frame_0.jpg").is_file()
    # even with expiry switched on, it waits for the person who sent it
    lifecycle.reconcile(lay, Retention(inbox_days=1, trash_days=0), b, now=time.time() + 30 * 86400)
    assert lay.state(day, name) == "pending"


def test_negative_path_is_unique(tmp_path):
    lay, _ = make(tmp_path)
    now = datetime(2026, 10, 2, 10, 15, 0)
    a = captured.negative_path(lay, "c1", now)
    a.parent.mkdir(parents=True)
    a.write_bytes(b"x")
    assert captured.negative_path(lay, "c1", now) != a
