#!/usr/bin/env python3
"""Impact risk from distance, and the decision each level triggers.

THE ZONES ARE NOT AGREED YET:
    Nobody has said where Low ends and High begins. The thresholds therefore come
    from the config's `risk:` block, and when that block is missing the built-in
    values below are used and `policy.configured` is False -- the UI shows that
    flag next to every risk it computes, so a placeholder can never be mistaken
    for a site's real safety zones.

        risk:
          turbine: "Turbine 1"       # name used in alert text
          high_below_m: 100          # closer than this        -> High
          medium_below_m: 250        # closer than this, else  -> Medium; further -> Low

DECISIONS ARE RECORDED, NOT ACTED ON:
    Low    -> no action
    Medium -> deterrent activated
    High   -> turbine stopped
    This module (and the alert log built on it) writes the decision down as metadata.
    Nothing here is connected to a deterrent or to a turbine controller, and every
    decision carries `actuated: false` to say so. Wiring a real actuator belongs in
    one place -- whatever consumes alerts.json -- and must not be inferred from the
    word "activated".
"""
from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("low", "medium", "high")
LABELS = {"low": "Low", "medium": "Medium", "high": "High"}

# level -> (machine name, human text)
DECISIONS = {
    "low": ("no_action", "No action"),
    "medium": ("deterrent_activated", "Deterrent activated"),
    "high": ("turbine_stopped", "Turbine stopped"),
}

DEFAULT_HIGH_BELOW_M = 100.0
DEFAULT_MEDIUM_BELOW_M = 250.0


@dataclass(frozen=True)
class RiskPolicy:
    high_below_m: float = DEFAULT_HIGH_BELOW_M
    medium_below_m: float = DEFAULT_MEDIUM_BELOW_M
    turbine: str = "Turbine"
    configured: bool = False   # False = the placeholder zones above are in use

    @classmethod
    def from_cfg(cls, cfg: dict) -> "RiskPolicy":
        block = (cfg or {}).get("risk")
        if not isinstance(block, dict):
            return cls()
        try:
            high = float(block.get("high_below_m", DEFAULT_HIGH_BELOW_M))
            medium = float(block.get("medium_below_m", DEFAULT_MEDIUM_BELOW_M))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"risk: thresholds must be numbers ({exc})") from exc
        if not 0 < high <= medium:
            raise ValueError(f"risk: need 0 < high_below_m ({high}) <= medium_below_m ({medium})")
        both = "high_below_m" in block and "medium_below_m" in block
        return cls(high, medium, str(block.get("turbine") or "Turbine"), configured=both)

    def classify(self, distance_m, override: str | None = None) -> str | None:
        """'low' | 'medium' | 'high', or None when there is nothing to judge from.

        A reviewer's override wins over the distance: they may know something the
        number does not (a distance measured to the wrong bird).
        """
        if override in LEVELS:
            return override
        try:
            d = float(distance_m)
        except (TypeError, ValueError):
            return None
        if d < 0 or d != d:
            return None
        if d < self.high_below_m:
            return "high"
        if d < self.medium_below_m:
            return "medium"
        return "low"

    def describe(self) -> dict:
        return {"turbine": self.turbine, "high_below_m": self.high_below_m,
                "medium_below_m": self.medium_below_m, "configured": self.configured}


def decision_for(level: str) -> dict:
    name, text = DECISIONS[level]
    return {"decision": name, "decision_label": text, "actuated": False}


def alert_message(site: str, camera: str, species: str | None, distance_m,
                  turbine: str, level: str) -> str:
    """'<site>, camera <camera> detected a <species> at <d> m from <turbine> (<Level> risk)'."""
    what = f"a {species}" if species else "a bird"
    d = f"{float(distance_m):g} m"
    return f"{site}, camera {camera} detected {what} at {d} from {turbine} ({LABELS[level]} risk)"
