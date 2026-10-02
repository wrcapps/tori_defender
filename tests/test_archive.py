import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import archive
import lifecycle
from layout import Retention, SiteLayout

DAY = "2026-10-02"


def make(tmp_path):
    lay = SiteLayout(tmp_path / "local" / "s", archive=tmp_path / "nas" / "s")
    lay.ensure()
    (lay.records / "live").mkdir(exist_ok=True)
    lay.archive.mkdir(parents=True)
    return lay, lay.records / "live"


def window(root, name, frames=3):
    d = root / DAY / name
    d.mkdir(parents=True)
    for i in range(frames):
        (d / f"frame_{i}.jpg").write_bytes(b"x" * (10 + i))
    (d / "window.json").write_text(json.dumps({"camera": "c", "frames": frames, "closed_at": time.time()}))
    return d


def old_negative(lay, name="c1-101500.jpg", age_s=2 * 86400):
    p = lay.negatives / DAY / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"neg")
    os.utime(p, (time.time() - age_s,) * 2)
    return p


def test_confirmed_window_moves_and_is_still_found(tmp_path):
    lay, b = make(tmp_path)
    src = window(lay.frames, "w1")
    stats = archive.run_once(lay, log=lambda *_: None)
    assert stats["windows"] == 1 and not src.exists()
    assert (lay.archive_frames / DAY / "w1" / "frame_2.jpg").is_file()
    assert lay.window_dir(DAY, "w1").parent.parent == lay.archive_frames and lay.state(DAY, "w1") == "confirmed"
    assert lay.window_dir(DAY, "w1", archive=False) is None


def test_inbox_windows_never_move(tmp_path):
    lay, b = make(tmp_path)
    src = window(lay.inbox, "pending-1")
    archive.run_once(lay, log=lambda *_: None)
    assert src.exists() and not (lay.archive_frames / DAY).exists()


def test_a_mismatched_copy_keeps_the_local_window(tmp_path):
    lay, b = make(tmp_path)
    src = window(lay.frames, "w1")
    (lay.archive_frames / DAY / "w1").mkdir(parents=True)                       # a broken earlier copy
    (lay.archive_frames / DAY / "w1" / "frame_0.jpg").write_bytes(b"short")
    stats = archive.run_once(lay, log=lambda *_: None)
    assert stats["windows"] == 0 and len(stats["errors"]) == 1 and src.exists()


def test_rerun_after_a_crash_between_copy_and_delete_finishes_the_job(tmp_path):
    lay, b = make(tmp_path)
    src = window(lay.frames, "w1")
    import shutil
    shutil.copytree(src, lay.archive_frames / DAY / "w1")                       # copied, then "crashed"
    assert archive.run_once(lay, log=lambda *_: None)["windows"] == 1 and not src.exists()


def test_negatives_move_after_24h_only(tmp_path):
    lay, b = make(tmp_path)
    old, fresh = old_negative(lay), old_negative(lay, "c1-111500.jpg", age_s=3600)
    archive.run_once(lay, log=lambda *_: None)
    assert not old.exists() and fresh.exists()
    assert (lay.archive_negatives / DAY / "c1-101500.jpg").is_file()
    assert lay.negative_file(DAY, "c1-101500.jpg") is not None


def test_records_are_backed_up_not_moved(tmp_path):
    lay, b = make(tmp_path)
    (b / "verdicts.json").write_text("{}")
    (b / "detections.jsonl").write_text("a\n")
    assert archive.backup_records(lay) == 2 and (b / "verdicts.json").exists()
    assert archive.backup_records(lay) == 0                                      # unchanged: nothing copied
    (b / "detections.jsonl").write_text("a\nb\n")
    assert archive.backup_records(lay) == 1


def test_rejecting_an_archived_window_deletes_it_where_it_lies(tmp_path):
    lay, b = make(tmp_path)
    window(lay.frames, "w1")
    archive.run_once(lay, log=lambda *_: None)
    with (b / "detections.jsonl").open("w") as f:
        f.write(json.dumps({"day": DAY, "window": "w1", "file": "frame_0.jpg", "bbox": [0, 0, 1, 1], "conf": .9, "track": 1}) + "\n")
    (b / "verdicts.json").write_text(json.dumps({f"{DAY}/w1/t0001": "drop"}))
    acts = lifecycle.reconcile(lay, Retention(), b, only=f"{DAY}/w1")
    assert [(a["to"], a["from"]) for a in acts] == [("purged", "confirmed")]
    assert lay.window_dir(DAY, "w1") is None


def test_mover_cycle_reports_a_down_archive_and_what_waits(tmp_path):
    lay, b = make(tmp_path)
    window(lay.frames, "w1")
    status = tmp_path / "status.json"
    mover = archive.Mover([lay], tmp_path / "nowhere", status)                   # archive root missing = NAS down
    st = mover.cycle()
    assert st["state"] == "down" and st["pending_windows"] == 1
    assert archive.read_status(status)["state"] == "down"
    assert lay.window_dir(DAY, "w1") is not None                                 # nothing lost, still local
