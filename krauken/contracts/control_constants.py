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
# (F-hours) itself, NOT on its output contribution. Now the ONLY thing
# governing both exotherm cancellation AND ramp-tracking (an earlier,
# separate ramp-feedforward term was removed -- see cascade.py's own
# module docstring for why it was both redundant with a real PI's
# integral and confirmed live to be actively harmful, saturating the
# entire chamber safety envelope for hours on a real fermentation's
# Free Rise stage). Standard control theory: a PI's integral settles on
# its own, with no separate term needed, at whatever value sustains zero
# steady-state error against a constant-rate ramp --
# I_ss = rate / (coupling * BEER_KI_F_PER_F_H), using the same assumed
# beer<->chamber coupling (0.05/h) platforms/simulator/plant.py's
# PlantParams.beer_chamber_coupling documents.
#
# Sized with comfortable margin over the two REAL, already-authored
# disturbances confirmed to matter -- not the largest ramp rate anyone
# could ever author, which would require growing this ceiling large
# enough to reproduce the same saturation risk the feedforward term had,
# just relocated:
#   - the simulator's documented peak exotherm (0.22F/h) needs
#     0.22 / (0.05 * 2.0) = 2.2 F-h.
#   - Cold Crash's own authored ramp (-0.3125F/h) needs
#     0.3125 / (0.05 * 2.0) = 3.125 F-h.
# 4.0 F-h covers both with margin (output contribution capped at
# BEER_KI_F_PER_F_H * 4.0 = 8.0F). A stage ramping meaningfully faster
# than that (Free Rise's own 1.5F/h would need 15 F-h) will legitimately,
# safely lag the ramp instead -- a bounded outcome, surfaced honestly to
# the user (the dashboard's "won't reach target" state) rather than
# silently blowing through the envelope for hours. That's a property of
# the authored stage, not something worth widening this ceiling to paper
# over.
INTEGRAL_MAX_F_H: Final[float] = 4.0

# BEER_KD_F_PER_F_PER_H / CLOSING_RATE_FILTER_TAU_H /
# MAX_PLAUSIBLE_BEER_RATE_F_PER_H: the derivative term (contracts/
# cascade.py's chamber_target_for()/update_closing_rate_filter()), added
# after real live data (fermentation 3's Free Rise stage) showed P+I
# alone leaves real headroom unused once INTEGRAL_MAX_F_H saturates on a
# fast-moving ramp target -- see cascade.py's own module docstring for the
# two alternatives (back-calculation anti-windup; dropping the internal
# ceiling for the real envelope) that were tried and rejected first, each
# by direct simulation, not assumption.
#
# BEER_KD_F_PER_F_PER_H: chamber-target offset (F) per (F/h) of filtered
# closing rate. Chosen by sweeping Kd against CLOSING_RATE_FILTER_TAU_H
# together (a bigger Kd needs a longer filter to stay noise-safe) across
# four scenarios simulated together, not tuned against any one alone:
#   - the real observed transient (a ~4F startup gap): overshoot improves
#     from today's P+I-only +0.91F to +0.51F.
#   - Free Rise-style ramp tracking (1.5F/h, matching the live batch):
#     mid-ramp lag improves from +3.32F to +2.48F (~25%).
#   - sustained disturbance steady state (documented exotherm 0.22F/h,
#     Cold Crash's own -0.3125F/h): steady-state error stays ~0
#     (-0.0009F / -0.0013F), matching P+I alone -- the derivative doesn't
#     compete with the integral's job.
#   - realistic sensor noise (sigma=0.03F/tick): zero heat/cool demand
#     reversals over a simulated 24h, across two random seeds.
# 24.0 was the smallest value in that sweep clearing all four cleanly;
# higher values keep improving overshoot/lag further with diminishing
# returns (both are bounded by CHAMBER_TARGET_MAX_F itself, not by Kd) at
# the cost of needing an even longer filter to stay noise-clean.
BEER_KD_F_PER_F_PER_H: Final[float] = 24.0
# CLOSING_RATE_FILTER_TAU_H: low-pass time constant (hours) on the
# closing-rate signal Kd multiplies. Long by ordinary PID standards
# (minutes, not hours) because BEER_KD_F_PER_F_PER_H is large enough that
# even ordinary per-tick sensor noise would otherwise get amplified into
# real relay chatter -- verified directly: at this tau, realistic noise
# (sigma=0.03F/tick) produces zero demand reversals over 24h; shorter
# taus (0.25h) produced hundreds at the same Kd.
CLOSING_RATE_FILTER_TAU_H: Final[float] = 4.0
# MAX_PLAUSIBLE_BEER_RATE_F_PER_H: hard clamp on the RAW (pre-filter)
# closing-rate signal -- no real beer thermal mass can plausibly change
# faster than this, so anything beyond it is a sensor artifact, not
# physics. Necessary ALONGSIDE the long filter above, not redundant with
# it: a single large glitch has no filter history to be averaged against
# yet, so even CLOSING_RATE_FILTER_TAU_H's 4 hours barely dents it in the
# first few ticks. Confirmed against this exact fermentation's own
# recorded probe-settling artifact (a real 5.75F swing in under 2
# minutes, implying a momentary rate of -159F/h): without this clamp, that
# glitch alone slammed the commanded chamber target straight to
# CHAMBER_TARGET_MAX_F on tick one, even at a 4-hour filter. 5.0F/h is
# comfortably above the fastest genuine rate this session has ever
# observed (Free Rise's own ~1.2-1.3F/h during real tracking) while still
# well below the artifact's implied rate.
MAX_PLAUSIBLE_BEER_RATE_F_PER_H: Final[float] = 5.0

# BEER_D_TAPER_FULL_F / BEER_D_TAPER_OFF_F: the derivative term's own
# proximity taper -- chamber_target_for() scales the D term's contribution
# by 1.0 at/inside BEER_D_TAPER_FULL_F, linearly down to 0.0 at/beyond
# BEER_D_TAPER_OFF_F. Added after a real incident (fermentation 4,
# 2026-08-28, Primary fermentation): beer started at 44.9F against a 66F
# constant target (a 21F gap), and by the time the gap had narrowed to a
# still-substantial 5.65F, the beer's OWN recovery-driven closing rate
# (~0.9F/h, built up from the chamber's earlier legitimate full-heat
# response to that same 21F gap) had grown large enough that D alone
# (-24.0 * 0.9 = -21.5F) cancelled almost the entire P+I push (+19.4F
# combined), commanding a chamber target barely above the beer's own
# current temp -- 63.83F, with 5.65F of real error still unaddressed.
# D's job is to brake in anticipation of OVERSHOOT; there was no overshoot
# to anticipate 5.65F short of target, only a genuine, still-substantial
# gap that P+I were both correctly asking to close aggressively.
#
# The taper's band was grounded against this fermentation's own two real,
# clearly-separated error clusters -- not guessed: points where D braking
# was later confirmed (by closed-loop resimulation against the Simulator's
# own plant physics, platforms/simulator/plant.py) to be genuinely
# necessary sat at |error| <= 1.4F (e.g. the Free Rise -> Diacetyl rest
# handoff, beer at 70.02F against a 70.0F target but still carrying a real
# +1.42F/h closing rate from the ramp -- weakening Kd there measurably
# increased peak overshoot, from +0.02F at Kd=24 up to +0.80-1.32F for
# every weaker Kd tried); every confirmed-inappropriate over-braking point
# sat at |error| >= 5.65F. 2.0F/4.0F sits with margin inside that gap on
# both sides.
#
# A LINEAR TAPER, not a hard on/off cutoff, and that choice is load-
# bearing, not stylistic: a bare `abs(error) <= 3.0` gate, tested against
# this project's own standard noise level (sigma=0.03F/tick, the same
# level CLOSING_RATE_FILTER_TAU_H was validated against), produced ~1400
# D on/off toggles over a simulated 24h with beer parked at the boundary --
# exactly the "long idle then a spike" chatter class of failure the
# original bang-bang cascade was replaced for (see cascade.py's own module
# docstring). The linear taper produced zero direction reversals under the
# same test, because there is no discrete threshold left to cross.
BEER_D_TAPER_FULL_F: Final[float] = 2.0
BEER_D_TAPER_OFF_F: Final[float] = 4.0

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
