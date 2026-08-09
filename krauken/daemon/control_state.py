"""In-memory control-loop state that doesn't belong in SQLite: the cascade's
own last commanded target/mode (for the beer-temp-lost hold-last-target
failsafe), per-stage gravity/temp-hold gate accumulators, and the last
written sample (for the sampling policy's should_write() comparison).

Known, disclosed limitation: this state does not survive a daemon restart.
A gravity/temp_hold gate that was 20 of its required 24 stable hours into
being satisfied resets to zero on restart, and a beer-temp-lost hold-target
failsafe loses its "last known good" target. Both are conservative failure
modes (a stage takes a bit longer to complete; a failsafe falls back to
recomputing from whatever reading is available) rather than unsafe ones,
and persisting this to SQLite every tick was judged not worth the write
volume for what M2 needs -- revisit if real deployments restart mid-stage
often enough for it to matter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from krauken.contracts.og_detection import OGDetector
from krauken.daemon.sampler import SampleCandidate


@dataclass
class ControlState:
    last_chamber_target_f: float | None = None
    last_relay_mode: str = "idle"
    gates: dict[int, Any] = field(default_factory=dict)  # stage_id -> GravityGate | TempHoldGate
    last_sample: SampleCandidate | None = None
    # health field name -> its ok/not-ok value as of the last tick, so the
    # control loop can log a *_lost/*_recovered event only on the edge,
    # not every tick it stays unhealthy.
    last_health: dict[str, bool | None] = field(default_factory=dict)
    # One per fermentation, not per stage -- OG detection runs from
    # fermentation start regardless of which stage happens to be current.
    # See contracts/og_detection.py.
    og_detector: OGDetector = field(default_factory=OGDetector)

    def reset(self) -> None:
        self.last_chamber_target_f = None
        self.last_relay_mode = "idle"
        self.gates = {}
        self.last_sample = None
        self.last_health = {}
        self.og_detector = OGDetector()
