import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import lifecycle
from layout import Retention, SiteLayout, gone_windows, read_events

DAY = "2026-10-01"
RET = Retention(inbox_days=3, trash_days=7)
NOW = time.time()


def make(tmp_path):
    lay = SiteLayout(tmp_path / "s")
    lay.ensure()
    (lay.records / "live").mkdir(exist_ok=True)
    return lay, lay.records / "live"


def window(lay, root, name, closed_days_ago=0.0, closed=True, extra=None, day=DAY):
    d = root / day / name
    d.mkdir(parents=True)
    (d / "frame_0.jpg").write_bytes(b"x")
    if closed:
        meta = {"camera": "c", "frames": 1, "closed_at": NOW - closed_days_ago * 86400, **(extra or {})}
        (d / "window.json").write_text(json.dumps(meta))
    return d


def detections(bucket, name, tracks=(1,), day=DAY):
    with (bucket / "detections.jsonl").open("a") as f:
        for t in tracks:
            f.write(json.dumps({"day": day, "window": name, "file": "frame_0.jpg", "bbox": [0, 0, 1, 1],
                                "conf": .9, "track": t}) + "\n")


def verdicts(bucket, **v):
    p = bucket / "verdicts.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(v)
    p.write_text(json.dumps(cur))


def run(lay, bucket):
    return lifecycle.reconcile(lay, RET, bucket, now=NOW)


def test_keep_confirms_and_moves_to_frames(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "c-100000")
    detections(b, "c-100000")
    verdicts(b, **{f"{DAY}/c-100000/t0001": "keep"})
    acts = run(lay, b)
    assert [a["to"] for a in acts] == ["confirmed"]
    assert lay.state(DAY, "c-100000") == "confirmed"
    assert (lay.frames / DAY / "c-100000" / "frame_0.jpg").exists()
    assert read_events(b)[-1]["to"] == "confirmed"


def test_all_tracks_dropped_goes_to_trash_not_deleted(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "c-100000")
    detections(b, "c-100000", tracks=(1, 2))
    verdicts(b, **{f"{DAY}/c-100000/t0001": "drop"})
    assert run(lay, b) == []                                    # one track still undecided
    verdicts(b, **{f"{DAY}/c-100000/t0002": "drop"})
    assert run(lay, b)[0]["to"] == "trash"
    assert lay.state(DAY, "c-100000") == "trashed"
    assert f"{DAY}/c-100000" in gone_windows(lay)


def test_unsure_and_hand_work_protect(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "u-1", closed_days_ago=10)
    detections(b, "u-1")
    verdicts(b, **{f"{DAY}/u-1/t0001": "unsure"})
    window(lay, lay.inbox, "m-1", closed_days_ago=10)
    detections(b, "m-1")
    (b / "manual_boxes.jsonl").write_text(json.dumps({"day": DAY, "window": "m-1", "file": "frame_0.jpg"}) + "\n")
    assert run(lay, b) == []
    assert lay.state(DAY, "u-1") == lay.state(DAY, "m-1") == "pending"


def test_expiry_only_after_inbox_days_and_only_when_closed(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "fresh", closed_days_ago=1)
    window(lay, lay.inbox, "old", closed_days_ago=4)
    window(lay, lay.inbox, "open", closed=False)
    for n in ("fresh", "old", "open"):
        detections(b, n)
    acts = run(lay, b)
    assert [(a["window"], a["reason"]) for a in acts] == [("old", "expired")]
    assert lay.state(DAY, "old") == "trashed" and lay.state(DAY, "fresh") == "pending"
    assert lay.state(DAY, "open") == "pending"


def test_confirmed_and_imported_windows_are_never_touched(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.frames, "old-1", closed_days_ago=100)
    window(lay, lay.inbox, "imp", closed_days_ago=100, extra={"excel_row": 3})
    detections(b, "old-1")
    assert run(lay, b) == []
    assert lay.state(DAY, "old-1") == "confirmed"


def test_two_roots_is_refused_not_resolved(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "dup", closed_days_ago=10)
    window(lay, lay.frames, "dup")
    acts = run(lay, b)
    assert "two roots" in acts[0]["error"]
    assert (lay.inbox / DAY / "dup").exists() and (lay.frames / DAY / "dup").exists()


def test_purge_after_retention_removes_window_crops_and_proxies(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "gone", closed_days_ago=4)
    detections(b, "gone")
    (b / "crops" / DAY / "gone").mkdir(parents=True)
    (b / "crops" / DAY / "gone" / "c.jpg").write_bytes(b"x")
    run(lay, b)
    assert lifecycle.purge(lay, b, today=date.today()) == []                 # not due yet
    later = date.today() + timedelta(days=8)
    assert lifecycle.purge(lay, b, today=later)[0]["window"] == "gone"
    assert not (b / "crops" / DAY / "gone").exists()
    assert lay.state(DAY, "gone") is None
    assert read_events(b)[-1]["to"] == "purged"


def test_restore_resets_the_clock(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "r", closed_days_ago=4)
    detections(b, "r")
    run(lay, b)
    lifecycle.restore(lay, DAY, "r", b)
    assert lay.state(DAY, "r") == "pending"
    assert run(lay, b) == []                                                 # fresh clock


def test_identity_is_unique_across_inbox_frames_and_trash(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "c-100000")
    window(lay, lay.frames, "c-100000-2")
    assert lay.unique_window(DAY, "c-100000") == "c-100000-3"
    assert lay.unique_window(DAY, "other") == "other"


def test_move_never_copies_across_filesystems(tmp_path, monkeypatch):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "x")

    def boom(*a):
        raise OSError(18, "Invalid cross-device link")
    monkeypatch.setattr(os, "rename", boom)
    detections(b, "x")
    verdicts(b, **{f"{DAY}/x/t0001": "keep"})
    acts = run(lay, b)
    assert "cross-device" in acts[0]["error"]
    assert (lay.inbox / DAY / "x" / "frame_0.jpg").exists()


def test_migrate_grandfathers_without_moving_and_is_idempotent(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.frames, "legacy")
    detections(b, "legacy")
    plan = lifecycle.migrate(lay, b, apply=True)
    assert plan["grandfather"] == [f"{DAY}/legacy"] and plan["backup"]
    assert (lay.frames / DAY / "legacy" / "frame_0.jpg").exists()
    assert lifecycle.migrate(lay, b, apply=True)["grandfather"] == []
    assert not any(s == "error" for s, _ in lifecycle.check(lay))


def test_check_flags_hand_work_for_a_missing_window(tmp_path):
    lay, b = make(tmp_path)
    (b / "manual_boxes.jsonl").write_text(json.dumps({"day": DAY, "window": "lost", "file": "f.jpg"}) + "\n")
    assert any(s == "error" and "lost" in m for s, m in lifecycle.check(lay))


def test_restore_clears_the_drop_verdicts_so_it_is_not_rejected_again(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "r2", closed_days_ago=0)
    detections(b, "r2")
    verdicts(b, **{f"{DAY}/r2/t0001": "drop"})
    run(lay, b)
    assert lay.state(DAY, "r2") == "trashed"
    lifecycle.restore(lay, DAY, "r2", b)
    assert json.loads((b / "verdicts.json").read_text()) == {}
    assert run(lay, b) == [] and lay.state(DAY, "r2") == "pending"


def test_a_verdict_saved_after_the_pass_started_cancels_the_move(tmp_path, monkeypatch):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "late", closed_days_ago=4)          # due to expire...
    detections(b, "late")
    real = lifecycle.load_facts
    calls = {"n": 0}

    def load(layout):
        facts = real(layout)
        calls["n"] += 1
        if calls["n"] == 1:                                    # ...then the operator clicks Keep mid-pass
            verdicts(b, **{f"{DAY}/late/t0001": "keep"})
        return facts
    monkeypatch.setattr(lifecycle, "load_facts", load)
    acts = run(lay, b)
    assert [a["to"] for a in acts] == ["confirmed"] and lay.state(DAY, "late") == "confirmed"


def test_purge_refuses_a_window_that_also_exists_elsewhere(tmp_path):
    lay, b = make(tmp_path)
    window(lay, lay.inbox, "twin", closed_days_ago=4)
    run(lay, b)
    window(lay, lay.frames, "twin")                             # the same identity reappears
    out = lifecycle.purge(lay, b, today=date.today() + timedelta(days=9))
    assert "also present" in out[0]["error"]
    assert (lay.frames / DAY / "twin").exists() and lay.trashed_dir(DAY, "twin")


def test_failures_do_not_use_up_the_move_cap(tmp_path):
    lay, b = make(tmp_path)
    for i in range(3):                                          # three stuck windows (identity in two roots)
        window(lay, lay.inbox, f"stuck{i}", closed_days_ago=9)
        window(lay, lay.frames, f"stuck{i}")
    window(lay, lay.inbox, "ok", closed_days_ago=9)
    detections(b, "ok")
    acts = lifecycle.reconcile(lay, RET, b, now=NOW, max_moves=1)
    assert any(a.get("window") == "ok" and a.get("to") == "trash" for a in acts)


def test_defaults_never_expire_and_delete_a_rejection_at_once(tmp_path):
    lay, b = make(tmp_path)
    ret = Retention()                                  # the shipped defaults
    old = window(lay, lay.inbox, "old", closed_days_ago=400)
    window(lay, lay.inbox, "bad", closed_days_ago=0)
    detections(b, "old"), detections(b, "bad")
    verdicts(b, **{f"{DAY}/bad/t0001": "drop"})
    lifecycle.reconcile(lay, ret, b, now=NOW)
    lifecycle.purge(lay, b)
    assert old.exists()                                # nobody reviewed it: it waits, however old
    assert lay.state(DAY, "bad") is None and not list(lay.trash.rglob("frame_0.jpg"))   # gone, not in trash


def test_a_confirmed_window_is_deleted_only_when_rejected_afterwards(tmp_path):
    lay, b = make(tmp_path)
    ret = Retention()
    rej = window(lay, lay.frames, "rej", closed_days_ago=50)
    kept = window(lay, lay.frames, "kept", closed_days_ago=50)
    plain = window(lay, lay.frames, "plain", closed_days_ago=50)
    for n in ("rej", "kept", "plain"):
        detections(b, n)
    verdicts(b, **{f"{DAY}/rej/t0001": "drop", f"{DAY}/kept/t0001": "keep"})
    lifecycle.reconcile(lay, ret, b, now=NOW)
    lifecycle.purge(lay, b)
    assert not rej.exists() and kept.exists() and plain.exists()
