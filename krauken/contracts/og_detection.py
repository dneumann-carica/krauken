"""Auto-detects a fermentation's OG (original gravity) from the gravity
sensor at fermentation start, instead of requiring the user to type it in
-- but tolerant of the unstable readings typical of the first few minutes
after racking beer into the fermenter (hydrometer/Tilt still bobbing, beer
still sloshing), so it waits for readings to genuinely settle before
locking in a value rather than trusting the very first one.

Built on the same self-relative-flatness primitive
(contracts/gravity_stability.py's GravityStabilityWindow) that
contracts/stages.py's GravityGate uses for "has fermentation actually
finished" -- same underlying question ("has this sensor's recent history
stopped moving"), asked here with a much shorter window and no threshold-
safety-net equivalent (there's no "stall" concept when locking in a
starting value).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from krauken.contracts.gravity_stability import GravityStabilityWindow

OG_STABLE_WINDOW_H = 0.5  # readings must hold tight for this long before locking
OG_STABILITY_TOLERANCE_SG = 0.002  # max-min spread allowed within that window
OG_DETECTION_MAX_H = 6.0  # give up waiting for stability and force-lock with best-available data


@dataclass
class OGDetector:
    """Locks in `locked_og` once gravity readings have genuinely settled.
    A no-op once locked -- OG is set exactly once per fermentation."""

    window: GravityStabilityWindow = field(default_factory=GravityStabilityWindow)
    locked_og: float | None = None

    def update(self, t_h: float, gravity_sg: float) -> None:
        if self.locked_og is not None:
            return
        self.window.update(t_h, gravity_sg, OG_STABLE_WINDOW_H)
        if self.window.is_flat(t_h, OG_STABLE_WINDOW_H, OG_STABILITY_TOLERANCE_SG):
            self.locked_og = self.window.mean()

    def reset(self) -> None:
        """Call this instead of update() when the reading is stale/lost --
        a dropout must not let readings from before and after the gap be
        stitched together as if continuous. Never clears an already-locked
        value -- once OG is locked, it's locked for good."""
        if self.locked_og is None:
            self.window.reset()

    def force_lock(self) -> None:
        """Called once OG_DETECTION_MAX_H has elapsed without the reading
        ever settling -- locks to whatever's been collected rather than
        leaving OG null forever. A safe no-op (retries next tick) if the
        window is currently empty, e.g. a stale reading emptied it right
        at the deadline."""
        if self.locked_og is not None or not self.window.readings:
            return
        self.locked_og = self.window.mean()
