"""In-memory control-loop state that doesn't belong in SQLite: the cascade's
own last commanded target and PI integral (for the beer-temp-lost hold-
last-target failsafe, and the PI controller's own accumulated error),
per-stage gravity/temp-hold gate accumulators, and the last written sample
(for the sampling policy's should_write() comparison).

Known, disclosed limitation: this state does not survive a daemon restart.
A gravity/temp_hold gate that was 20 of its required 24 stable hours into
being satisfied resets to zero on restart, a beer-temp-lost hold-target
failsafe loses its "last known good" target, and beer_error_integral loses
whatever correction it had accumulated so far. All are conservative
failure modes (a stage takes a bit longer to complete; a failsafe falls
back to recomputing from whatever reading is available; the PI controller
starts re-accumulating from zero, the same as it does at the start of any
fresh fermentation) rather than unsafe ones, and persisting this to SQLite
every tick was judged not worth the write volume for what M2 needs --
revisit if real deployments restart mid-stage often enough for it to
matter.

Also disclosed: beer_error_integral resets only with the rest of this
fermentation-scoped state (a new fermentation, or a daemon restart), NOT
on every stage transition within one fermentation -- a stage change (e.g.
Primary -> Cold crash) carries whatever correction was accumulated during
the previous stage into the next one. INTEGRAL_MAX_F_H's anti-windup
clamp already bounds how much bias that can possibly carry (see
contracts/control_constants.py), so this is accepted as a bounded,
conservative simplification rather than something worth a separate
per-stage reset mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from krauken.contracts.og_detection import OGDetector
from krauken.daemon.sampler import SampleCandidate


@dataclass
class ControlState:
    last_chamber_target_f: float | None = None
    # The PI controller's own accumulated beer-temp error (contracts/
    # cascade.py's update_beer_error_integral()/chamber_target_for()) --
    # F-hours, already anti-windup-clamped to +/-INTEGRAL_MAX_F_H by
    # update_beer_error_integral() itself.
    beer_error_integral: float = 0.0
    # The derivative term's own filtered state (contracts/cascade.py's
    # update_closing_rate_filter()/chamber_target_for()) -- same
    # fermentation-scoped reset semantics as beer_error_integral above,
    # for the same reason (a stage transition within one fermentation
    # carries whatever the controller was already doing into the next
    # stage; see this class's own module docstring). prev_beer_temp_f/
    # prev_beer_target_f are None until the first tick with a healthy
    # beer reading has run once -- update_closing_rate_filter() treats
    # that as "no rate available yet" rather than fabricating one.
    closing_rate_filtered_f_per_h: float = 0.0
    prev_beer_temp_f: float | None = None
    prev_beer_target_f: float | None = None
    # ctx.clock.monotonic() as of the last tick (ANY tick, fermentation
    # active or not -- see control_loop.py's control_tick(), which runs
    # every scheduled tick regardless), so update_beer_error_integral()
    # gets that tick's REAL elapsed dt_h rather than assuming the nominal
    # 30s interval. None means "no prior tick this process has seen yet",
    # so the very first tick contributes dt_h=0 to the integral instead of
    # some fabricated/guessed interval. Deliberately monotonic, never
    # ctx.clock.now() -- see contracts/clock.py's Clock.now() docstring on
    # why wall-clock time must never drive timer arithmetic.
    last_tick_monotonic: float | None = None
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
        self.beer_error_integral = 0.0
        self.closing_rate_filtered_f_per_h = 0.0
        self.prev_beer_temp_f = None
        self.prev_beer_target_f = None
        self.last_tick_monotonic = None
        self.gates = {}
        self.last_sample = None
        self.last_health = {}
        self.og_detector = OGDetector()
