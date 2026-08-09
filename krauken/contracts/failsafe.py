"""Health/staleness detection for the three sensor readings the control
loop depends on, and what "lost" means for each:

- Beer temp lost: the daemon has no runner-up source to fail over to (the
  role-mapping spec is explicit about this -- one platform per role, no
  priority chain). The control loop's response is to hold the last
  commanded chamber target rather than recompute a new one from a stale
  reading, and mark the sample's target_source as "failsafe" -- see
  daemon/control_loop.py, which is where that hold actually happens (this
  module only detects the condition, it doesn't act on it).
- Gravity lost: per the role-mapping spec, an unmapped or failing gravity
  source means gravity-gated stages fall back to their max_hours cap. No
  special-cased fallback code is needed for that beyond what
  contracts.stages already does -- the control loop just must NOT feed a
  stale gravity reading into the stage's GravityGate as if it were a fresh
  in-band value (see reset_stale_gate below), so a dead sensor can't
  spuriously "hold stable" a gate via elapsed time alone.
- Chamber temp lost: real compressor-protection consequence (de-energize)
  belongs to the Hardware Supervisor tier, not here -- this module only
  reports the staleness so the daemon can surface it and choose not to
  trust the reading for its own cascade math.
"""
from __future__ import annotations

from dataclasses import dataclass

from krauken.contracts.control_constants import BEER_TEMP_LOST_THRESHOLD_S, CHAMBER_TEMP_LOSS_S

# No separate gravity-staleness constant exists in control_constants.py --
# gravity is a beer-side reading like beer temp, so it reuses the same
# threshold rather than inventing an arbitrary second number.
GRAVITY_LOST_THRESHOLD_S = BEER_TEMP_LOST_THRESHOLD_S


def is_stale(last_good_ts: float | None, now: float, threshold_s: float) -> bool:
    if last_good_ts is None:
        return True
    return (now - last_good_ts) > threshold_s


@dataclass(frozen=True, slots=True)
class HealthAssessment:
    beer_temp_ok: bool
    chamber_temp_ok: bool
    gravity_ok: bool | None  # None = no gravity source mapped at all -- distinct from "mapped but stale"


def assess_health(
    *,
    now: float,
    beer_last_good_ts: float | None,
    chamber_last_good_ts: float | None,
    gravity_last_good_ts: float | None,
    gravity_mapped: bool,
) -> HealthAssessment:
    return HealthAssessment(
        beer_temp_ok=not is_stale(beer_last_good_ts, now, BEER_TEMP_LOST_THRESHOLD_S),
        chamber_temp_ok=not is_stale(chamber_last_good_ts, now, CHAMBER_TEMP_LOSS_S),
        gravity_ok=(not is_stale(gravity_last_good_ts, now, GRAVITY_LOST_THRESHOLD_S)) if gravity_mapped else None,
    )
