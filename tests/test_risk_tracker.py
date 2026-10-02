import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import itertools
import pytest

from risk import RiskPolicy, alert_message, decision_for
from tracker import Linker


def test_policy_zones_and_boundaries():
    p = RiskPolicy.from_cfg({"risk": {"high_below_m": 100, "medium_below_m": 250, "turbine": "T7"}})
    assert p.configured and p.turbine == "T7"
    assert p.classify(0) == "high"
    assert p.classify(99.9) == "high"
    assert p.classify(100) == "medium"      # boundary belongs to the safer-but-nearer zone below it
    assert p.classify(249) == "medium"
    assert p.classify(250) == "low"
    assert p.classify(5000) == "low"


def test_missing_or_bad_distance_is_unjudged_not_low():
    p = RiskPolicy()
    for bad in (None, "", "abc", -3, float("nan")):
        assert p.classify(bad) is None


def test_override_wins():
    assert RiskPolicy().classify(1000, override="high") == "high"
    assert RiskPolicy().classify(None, override="medium") == "medium"
    assert RiskPolicy().classify(5, override="nonsense") == "high"


def test_defaults_are_flagged_as_placeholder():
    assert RiskPolicy.from_cfg({}).configured is False
    assert RiskPolicy.from_cfg({"risk": {"high_below_m": 50}}).configured is False


def test_bad_config_is_rejected():
    with pytest.raises(ValueError):
        RiskPolicy.from_cfg({"risk": {"high_below_m": 300, "medium_below_m": 100}})
    with pytest.raises(ValueError):
        RiskPolicy.from_cfg({"risk": {"high_below_m": "x"}})


def test_decisions():
    assert decision_for("low")["decision"] == "no_action"
    assert decision_for("medium")["decision"] == "deterrent_activated"
    assert decision_for("high")["decision"] == "turbine_stopped"
    assert all(decision_for(l)["actuated"] is False for l in ("low", "medium", "high"))


def test_message():
    m = alert_message("babadag", "modul2-47", "drone", 6, "Turbine 1", "high")
    assert m == "babadag, camera modul2-47 detected a drone at 6 m from Turbine 1 (High risk)"
    assert "a bird" in alert_message("s", "c", None, 120.0, "T", "medium")


def box(cx, cy, w=40, h=40):
    return {"bbox": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]}


def test_linker_follows_one_bird_with_constant_velocity():
    ids = itertools.count(1)
    lk = Linker(lambda: next(ids))
    seen = []
    for k in range(6):
        (b,) = lk.update([box(100 + 90 * k, 500)])
        seen.append(b["track"])
    assert len(set(seen)) == 1


def test_linker_keeps_two_birds_apart_and_opens_new_ones():
    ids = itertools.count(1)
    lk = Linker(lambda: next(ids))
    a1, b1 = lk.update([box(100, 100), box(2000, 900)])
    a2, b2 = lk.update([box(130, 105), box(1960, 880)])
    assert a1["track"] == a2["track"] and b1["track"] == b2["track"] and a1["track"] != b1["track"]
    (c,) = lk.update([box(3500, 2000)])           # far from both
    assert c["track"] not in (a1["track"], b1["track"])


def test_linker_closes_stale_tracks_and_survives_a_gap():
    ids = itertools.count(1)
    lk = Linker(lambda: next(ids), max_missed=2)
    (a,) = lk.update([box(100, 100)])
    lk.update([]); (back,) = lk.update([box(110, 100)])   # one-frame gap: same bird
    assert back["track"] == a["track"]
    for _ in range(4):
        lk.update([])
    (late,) = lk.update([box(110, 100)])                  # long gone: a new track
    assert late["track"] != a["track"]
