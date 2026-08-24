"""Control-loop constants -- single source of truth, imported by both the
daemon (beer-side enforcement) and the Hardware Supervisor (compressor-
protection enforcement). See fermentation_controller_master_spec's sibling
software plan and the resolved-decisions plan for why enforcement is split
by tier while the values live in one place:

Daemon enforces the beer-side numbers below (needs beer temp, which the
Supervisor must never know about, so it keeps protecting the compressor
even if the daemon/cascade is completely broken). Supervisor enforces the
compressor-protection numbers (must survive daemon crashes).

Source: the UI prototype's simulator (designs/Krauken Fermentation Chart.dc.html)
and the handoff README's "Control model" section, confirmed as the real
intended values -- not placeholders -- by the project owner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# --- beer-side (enforced in the control daemon's cascade) ---
# "Close enough to count as at target" -- no longer read by the cascade
# itself (a continuous PI controller has no discrete trigger/release
# point to size), but kept here since contracts/stages.py's TempHoldGate
# reuses it as its own "at target" tolerance for a temp_hold stage's
# completion gate, deliberately the same tolerance the cascade used to
# use, not a coincidence -- see that class's own docstring.
BEER_TRIGGER_BAND_F: Final[float] = 0.5
# PI gains for chamber_target_for() (contracts/cascade.py) -- replaced a
# fixed-clamp bang-bang cascade (fire a hard offset once |beer - target|
# crosses BEER_TRIGGER_BAND_F, release it entirely back to zero once beer
# crosses back through target) that could correct a one-time deviation
# but had no way to counter a SUSTAINED one-directional disturbance (an
# actively fermenting beer's own exothermic heat) except by re-triggering
# over and over, each time releasing all the way back to no offset in
# between -- exactly the "long idle then a spike" oscillation a real run
# showed. A PI controller holds a smooth, continuously-graded offset
# instead: BEER_KP_F_PER_F reacts to the instantaneous error, and
# BEER_KI_F_PER_F_H accumulates it over time so a disturbance that never
# lets the error clear on its own still gets fully countered eventually,
# without ever needing to "release" anything.
#
# BEER_KP_F_PER_F: chamber-target offset (F) per F of instantaneous beer-
# temp error. Deliberately modest -- a transient 1F deviation produces a
# 2F chamber offset immediately, well under full compensation of a
# sustained disturbance (see BEER_KI_F_PER_F_H below) -- so the integral
# term, not an aggressive proportional gain, is what carries a SUSTAINED
# correction; this alone should never be enough to overshoot on its own.
BEER_KP_F_PER_F: Final[float] = 2.0
# BEER_KI_F_PER_F_H: chamber-target offset (F) per F-hour of accumulated
# beer-temp error. Grounded against the simulator's own documented values,
# not guessed: platforms/simulator/plant.py's ExothermParams.peak_f_per_h
# (0.22 F/h, the simulator's peak fermentation heat rate) and
# PlantParams.beer_chamber_coupling (0.05/h, beer<->chamber thermal
# coupling) together mean fully cancelling that peak rate at steady state
# needs a chamber offset of 0.22 / 0.05 = 4.4F below the beer target. At
# this gain, a sustained ~1F residual error accumulates that full 4.4F of
# correction in about 4.4 / 2.0 = 2.2 hours -- roughly the same ballpark
# the OLD fixed CHAMBER_COOL_CLAMP_F=5.0 clamp applied instantly (and on a
# hard bang-bang trigger) instead of ramping in smoothly as a real
# disturbance persists. A starting point, not a rigorously tuned value --
# real fermentation data may move this once this ships.
BEER_KI_F_PER_F_H: Final[float] = 2.0
# INTEGRAL_MAX_F_H: anti-windup ceiling on the raw accumulated integral
# (F-hours) itself, NOT on its output contribution -- clamping
# beer_error_integral to +/-3.0 caps its output contribution
# (BEER_KI_F_PER_F_H * integral) at +/-6.0F: comfortably above the 4.4F
# needed to fully cancel the documented peak exotherm (headroom for a real
# disturbance running hotter than the simulator's own documented peak),
# but bounded so a prolonged real deviation (a stuck sensor, an
# authored stage that's simply wrong) can't let the integral accumulate
# without limit and cause a dangerous overshoot once whatever caused it
# finally clears.
INTEGRAL_MAX_F_H: Final[float] = 3.0
# Assumed beer<->chamber thermal responsiveness, used ONLY to size how much
# extra offset the cascade needs to add while the beer's OWN target is
# actively ramping (see cascade.chamber_target_for's docstring) -- PI
# alone corrects a deviation from a HELD setpoint just fine, but a target
# that's ramping down (e.g. a cold crash) keeps moving out from under a
# chamber that's only reacting to present error, so the beer settles into
# a permanent lag behind it instead of ever closing the gap. Expressed the
# same way plant.py's own beer_chamber_coupling is (the fraction of the
# beer/chamber gap the beer closes per hour) so the parallel reads
# directly, but this is a control-side ASSUMPTION about a typical
# fermenter's thermal mass, not a physics measurement pulled from the
# simulator -- real hardware has no such constant to read, so the cascade
# has to assume one, exactly the way the PI gains above already are
# assumed/tuned rather than derived.
RAMP_FEEDFORWARD_COUPLING_PER_H: Final[float] = 0.05

# --- chamber/actuator side (enforced in the Hardware Supervisor) ---
CHAMBER_DEADBAND_F: Final[float] = 0.5  # NOT from the mock -- see module docstring; the
# mock's simulated fridge asymptotically approaches the clamp and never arrives, so it
# never needed a chamber-side thermostat. A real fridge reaches it and must stop.
MIN_ON_S: Final[float] = 5 * 60
MIN_OFF_S: Final[float] = 5 * 60
OPPOSITE_LOCKOUT_S: Final[float] = 30 * 60

# --- absolute safety envelope ---
CHAMBER_TARGET_MIN_F: Final[float] = 28.0
CHAMBER_TARGET_MAX_F: Final[float] = 90.0

# --- degradation thresholds ---
CHAMBER_TEMP_LOSS_S: Final[float] = 5 * 60  # supervisor de-energizes if it goes blind
DAEMON_SILENCE_ALERT_S: Final[float] = 5 * 60  # alertable; does not change actuation
BEER_TEMP_LOST_THRESHOLD_S: Final[float] = 15 * 60  # default, user-configurable


@dataclass(frozen=True, slots=True)
class ControlTuning:
    """Override point for settings + scenario time-compression. scaled()
    lets a compressed-time test scenario assert against the SCALED value of
    a timer rather than the literal 45/90 minutes."""

    beer_trigger_band_f: float = BEER_TRIGGER_BAND_F
    chamber_deadband_f: float = CHAMBER_DEADBAND_F
    min_on_s: float = MIN_ON_S
    min_off_s: float = MIN_OFF_S
    opposite_lockout_s: float = OPPOSITE_LOCKOUT_S
    chamber_target_min_f: float = CHAMBER_TARGET_MIN_F
    chamber_target_max_f: float = CHAMBER_TARGET_MAX_F

    def scaled(self, factor: float) -> "ControlTuning":
        return ControlTuning(
            beer_trigger_band_f=self.beer_trigger_band_f,
            chamber_deadband_f=self.chamber_deadband_f,
            min_on_s=self.min_on_s * factor,
            min_off_s=self.min_off_s * factor,
            opposite_lockout_s=self.opposite_lockout_s * factor,
            chamber_target_min_f=self.chamber_target_min_f,
            chamber_target_max_f=self.chamber_target_max_f,
        )
