#!/usr/bin/env python3
"""Browser run of the review flow against a seeded sandbox (tests/perf/sandbox_server.py --seed-review).

    python tests/e2e/review_flow.py --port 8792 --root /tmp/x
Asserts what the user sees AND where the files ended up.
"""
import argparse, json
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8792)
ap.add_argument("--root", type=Path, required=True)
ap.add_argument("--shots", type=Path, default=Path("/tmp"))
a = ap.parse_args()
base = f"http://127.0.0.1:{a.port}"
site = a.root / "sites/sandbox"
import time
day = time.strftime("%Y-%m-%d")
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{base}/login")
    pg.request.post(f"{base}/api/login", data=json.dumps({"username": "t", "password": "test"}),
                    headers={"Content-Type": "application/json"})

    pg.goto(f"{base}/app/inbox"); pg.wait_for_selector(".inbox-item")
    check(pg.locator(".inbox-item").count() == 3, "inbox lists the 3 pending windows (the expired one is gone)")
    check(pg.locator(".shell-badge").inner_text() == "3", "nav badge shows 3")
    pg.screenshot(path=str(a.shots / "inbox.png"))

    # confirm track 1 of window 100000 (it also has an undecided track 2)
    pg.goto(f"{base}/app/live/sandbox/track/{day}/cam00-100000/t0001".replace("/t0001", "%2Ft0001").replace(f"{day}/cam00", f"{day}%2Fcam00"))
    pg.wait_for_selector(".review-panel")
    check("pending review" in pg.locator(".review-panel-head").inner_text(), "panel says pending review")
    pg.screenshot(path=str(a.shots / "review-pending.png"))
    pg.get_by_role("button", name="Confirm bird").click()
    pg.wait_for_selector(".review-outcome")
    check("kept permanently" in pg.locator(".review-outcome").inner_text(), "outcome says kept permanently")
    check((site / "frames" / day / "cam00-100000").is_dir() and not (site / "inbox" / day / "cam00-100000").exists(),
          "files: window moved inbox -> frames")
    check((site / "frames" / day / "cam00-100000" / "frame_0.jpg").exists(), "frames still there after the move")
    pg.screenshot(path=str(a.shots / "review-confirmed.png"))

    # reject window 100100 with a reason
    pg.goto(f"{base}/app/live/sandbox/track/{day}%2Fcam00-100100%2Ft0001")
    pg.wait_for_selector(".review-panel")
    pg.get_by_role("button", name="Something is wrong…").click()
    pg.select_option(".review-reject select", "not_a_bird")
    pg.fill(".review-reject input", "it is a bush")
    pg.get_by_role("button", name="Reject detection").click()
    pg.wait_for_selector(".review-outcome")
    check("trash" in pg.locator(".review-outcome").inner_text(), "outcome says moved to trash")
    check(not (site / "inbox" / day / "cam00-100100").exists() and any((site / "trash").glob(f"*/{day}/cam00-100100")),
          "files: window moved inbox -> trash/<purge-date>, not deleted")
    events = [json.loads(l) for l in (site / "dataset/detections/live/lifecycle.jsonl").read_text().splitlines()]
    check(any(e.get("event") == "review" and e.get("reason") == "not_a_bird" and e.get("note") == "it is a bush" for e in events),
          "lifecycle.jsonl records the reason and note")
    # a rejected detection is no longer served
    r = pg.request.get(f"{base}/api/track?site=sandbox&id={day}%2Fcam00-100100%2Ft0001")
    check(r.status == 404, "rejected detection's records are hidden (404)")
    pg.goto(f"{base}/app/live/sandbox/track/{day}%2Fcam00-100100%2Ft0001"); pg.wait_for_selector(".state-page")
    check("rejected or has expired" in pg.inner_text("body"), "page explains why it is gone")

    # undo: the rejection of 100300 is taken back, then rejected again and finally confirmed from a stale page
    pg.goto(f"{base}/app/live/sandbox/track/{day}%2Fcam00-100300%2Ft0001"); pg.wait_for_selector(".review-panel")
    pg.get_by_role("button", name="Something is wrong…").click()
    pg.get_by_role("button", name="Reject detection").click(); pg.wait_for_selector(".rv-undo")
    check(any((site / "trash").glob(f"*/{day}/cam00-100300")), "100300 rejected -> trash")
    pg.locator(".rv-undo").click(); pg.wait_for_selector("text=Rejection undone")
    check((site / "inbox" / day / "cam00-100300").is_dir(), "undo: window back in inbox")
    check(json.loads((site / "dataset/detections/live/verdicts.json").read_text()).get(f"{day}/cam00-100300/t0001") is None,
          "undo: the drop verdict is cleared")
    pg.get_by_role("button", name="Something is wrong…").click()
    pg.get_by_role("button", name="Reject detection").click(); pg.wait_for_selector(".rv-undo")
    # the page is stale (window is in trash); clicking Confirm must restore it AND keep it
    pg.get_by_role("button", name="Confirm bird").click(); pg.wait_for_selector("text=kept permanently")
    check((site / "frames" / day / "cam00-100300").is_dir() and not any((site / "trash").glob(f"*/{day}/cam00-100300")),
          "confirm from a stale page: restored and moved to frames, not purged later")

    # the bell / inbox now only has the untouched one + the undecided sibling
    pg.goto(f"{base}/app/inbox"); pg.wait_for_selector(".inbox-empty")
    check("Nothing waiting" in pg.inner_text("body"), "inbox is empty: every window was decided")
    check(not errors, f"no page errors {errors}")
    b.close()
raise SystemExit(1 if fails else 0)
