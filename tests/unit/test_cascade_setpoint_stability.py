"""Behavioral/scenario tests for the PI cascade's setpoint STABILITY --
distinct from test_cascade.py's pure per-term unit tests (P, I, anti-
windup, envelope clamp in isolation). This file formalizes and proves one
specific, explicit requirement: dramatic setpoint movement is fine and
expected when the beer genuinely needs it, but the cascade must never
rapidly reverse between a cooling demand and a heating demand the way the
OLD bang-bang cascade did.

Confirmed real-world motivation, not hypothetical: on 2026-08-24, the
currently-deployed OLD cascade (fixed-clamp bang-bang, since replaced --
see contracts/cascade.py's module docstring) was observed live on fermentation
2's Free Rise stage (a 66->70F ramp over 6h) swinging chamber_target_f
between ~53F ("cool" mode, the ramp's abs(rate)-derived push applied in
the WRONG direction) and ~80F ("heat" mode, same push now right-signed)
on almost every tick, as the beer probe read 66/67 right at the ramp's
current value. Test A below replays that exact recorded sequence through
the new PI functions.
"""
from __future__ import annotations

import pytest

from krauken.contracts.cascade import chamber_target_for, update_beer_error_integral
from krauken.contracts.control_constants import CHAMBER_TARGET_MAX_F, CHAMBER_TARGET_MIN_F


def _direction(chamber_target_f: float, beer_target_f: float, threshold_f: float = 1.0) -> int:
    """-1 (cooling demand), +1 (heating demand), 0 (holding) -- compares
    the cascade's own output against the beer target, its natural zero-
    reference (error=0, integral=0, ramp=0 gives chamber_target_f ==
    beer_target_f exactly). threshold_f excludes trivial near-zero
    crossings from counting as a real demand in either direction."""
    diff = chamber_target_f - beer_target_f
    if diff <= -threshold_f:
        return -1
    if diff >= threshold_f:
        return 1
    return 0


def _count_reversals(directions: list[int]) -> int:
    """Walks a sequence of directions, ignoring holds (0), and counts
    every adjacent nonzero-to-opposite-nonzero transition. THIS is the
    concrete metric for "jumping between cooling and heating repeatedly"
    -- since the only two nonzero values are -1/+1, any adjacent pair of
    differing nonzero directions is by definition a real reversal."""
    nonzero = [d for d in directions if d != 0]
    return sum(1 for a, b in zip(nonzero, nonzero[1:]) if a != b)


def _replay(ticks: list[tuple[float, float, float, float]]) -> list[float]:
    """Threads update_beer_error_integral() -> chamber_target_for() across
    a sequence of (beer_temp_f, beer_target_f, ramp_rate_f_per_h, dt_h)
    ticks, integral starting at 0.0 (matching a fresh ControlState) --
    the shared engine every scenario test in this file drives."""
    integral = 0.0
    outputs = []
    for beer_temp_f, beer_target_f, ramp_rate_f_per_h, dt_h in ticks:
        integral = update_beer_error_integral(beer_temp_f, beer_target_f, integral, dt_h)
        outputs.append(chamber_target_for(beer_temp_f, beer_target_f, integral, ramp_rate_f_per_h))
    return outputs


# --- A. Real-incident regression replay ---------------------------------

# Exact (beer_temp_f, effective_target_f, dt_h) pulled directly from the
# real fermentation's `samples` table, 2026-08-24T08:28:17Z through
# 09:16:18Z -- the precise sequence that made the OLD cascade flip
# cooling<->heating repeatedly. ramp_rate is Free Rise's own authored rate,
# (70.0 - 66.0) / 6.0 -- constant for a "stepped" stage's whole duration.
FREE_RISE_RATE_F_PER_H = (70.0 - 66.0) / 6.0

REAL_INCIDENT_TICKS = [
    (67.0, 66.005577, 0.0),
    (67.0, 66.01403, 0.012678),
    (67.0, 66.019673, 0.008465),
    (67.0, 66.025314, 0.008462),
    (67.0, 66.03096, 0.008468),
    (67.0, 66.042249, 0.016935),
    (67.0, 66.053533, 0.016925),
    (67.0, 66.064823, 0.016936),
    (67.0, 66.070463, 0.00846),
    (67.0, 66.076113, 0.008475),
    (67.0, 66.081747, 0.008451),
    (67.0, 66.093036, 0.016933),
    (67.0, 66.098689, 0.00848),
    (67.0, 66.109981, 0.016938),
    (67.0, 66.115616, 0.008452),
    (67.0, 66.132547, 0.025396),
    (67.0, 66.138186, 0.008459),
    (67.0, 66.143843, 0.008486),
    (67.0, 66.149483, 0.008459),
    (67.0, 66.15514, 0.008486),
    (67.0, 66.16643, 0.016935),
    (67.0, 66.177741, 0.016967),
    (67.0, 66.189038, 0.016945),
    (67.0, 66.200329, 0.016937),
    (67.0, 66.211614, 0.016928),
    (67.0, 66.217266, 0.008477),
    (67.0, 66.228567, 0.016952),
    (67.0, 66.234211, 0.008466),
    (67.0, 66.239851, 0.00846),
    (67.0, 66.256789, 0.025407),
    (67.0, 66.262426, 0.008455),
    (67.0, 66.285004, 0.033867),
    (67.0, 66.29065, 0.00847),
    (67.0, 66.296299, 0.008473),
    (67.0, 66.307581, 0.016923),
    (67.0, 66.318873, 0.016938),
    (67.0, 66.330168, 0.016943),
    (67.0, 66.34711, 0.025413),
    (67.0, 66.352753, 0.008465),
    (67.0, 66.358402, 0.008473),
    (67.0, 66.375349, 0.025421),
    (67.0, 66.380993, 0.008465),
    (67.0, 66.386633, 0.00846),
    (67.0, 66.392281, 0.008473),
    (67.0, 66.403568, 0.01693),
    (67.0, 66.426149, 0.033872),
    (66.0, 66.443089, 0.02541),
    (66.0, 66.44873, 0.008461),
    (67.0, 66.454389, 0.008489),
    (67.0, 66.460036, 0.00847),
    (66.0, 66.465673, 0.008456),
    (66.0, 66.471319, 0.008469),
    (66.0, 66.47696, 0.008461),
    (66.0, 66.482603, 0.008465),
    (66.0, 66.488252, 0.008474),
    (66.0, 66.493903, 0.008477),
    (66.0, 66.505198, 0.016942),
    (66.0, 66.510835, 0.008456),
    (67.0, 66.52213, 0.016942),
    (66.0, 66.527778, 0.008473),
    (66.0, 66.53907, 0.016937),
    (66.0, 66.544707, 0.008456),
]


def _replay_real_incident() -> tuple[list[float], list[float]]:
    ticks = [(beer, target, FREE_RISE_RATE_F_PER_H, dt_h) for beer, target, dt_h in REAL_INCIDENT_TICKS]
    outputs = _replay(ticks)
    targets = [target for _, target, _ in REAL_INCIDENT_TICKS]
    return outputs, targets


def test_real_incident_replay_produces_no_direction_reversals():
    # The exact sequence that made the OLD cascade flip cooling->heating
    # repeatedly (see this file's module docstring) must not do so here.
    outputs, targets = _replay_real_incident()
    directions = [_direction(o, t) for o, t in zip(outputs, targets)]
    assert _count_reversals(directions) == 0


def test_real_incident_replay_has_no_wild_tick_to_tick_jumps():
    # The old cascade jumped ~13-27F between adjacent ticks on a mode
    # flip. The beer probe itself only ever moves 1F between these real
    # ticks -- the new cascade's response to that must stay bounded.
    outputs, _ = _replay_real_incident()
    jumps = [abs(b - a) for a, b in zip(outputs, outputs[1:])]
    assert max(jumps) < 10.0, f"largest tick-to-tick jump was {max(jumps):.2f}F"


def test_real_incident_replay_stays_within_the_safety_envelope():
    outputs, _ = _replay_real_incident()
    assert all(CHAMBER_TARGET_MIN_F <= o <= CHAMBER_TARGET_MAX_F for o in outputs)


# --- B. Synthetic sensor-noise chatter (generalizes A) -------------------

def _warm_up_then_alternate(warm_up_tick: tuple[float, float, float, float], noise_pair: tuple[float, float], beer_target: float, rate: float, n_noise_ticks: int = 20) -> list[int]:
    warm_up = [warm_up_tick] * 6  # ~a few hours of sustained real disturbance -> a genuine, nonzero integral bias
    noise = [(noise_pair[i % 2], beer_target, rate, 0.0083) for i in range(n_noise_ticks)]
    outputs = _replay(warm_up + noise)
    targets = [beer_target] * len(warm_up) + [beer_target] * len(noise)
    return [_direction(o, t) for o, t in zip(outputs, targets)]


def test_noise_alone_does_not_flip_direction_under_a_held_target_with_an_existing_bias():
    # Beer ran warm (69F vs a 66F target) for a while first -- a real,
    # nonzero cooling bias in the integral -- then the probe starts
    # alternating 66/67F (quantization noise) around the still-held
    # target. Noise alone must not flip the demand direction.
    directions = _warm_up_then_alternate(
        warm_up_tick=(69.0, 66.0, 0.0, 0.5), noise_pair=(66.0, 67.0), beer_target=66.0, rate=0.0,
    )
    assert _count_reversals(directions) == 0


def test_noise_alone_does_not_flip_direction_under_a_rising_ramp():
    # Beer lagging behind a rising ramp for a while (needs heat), then
    # probe noise on top -- same target throughout warm-up and noise (see
    # the falling-ramp test's own comment on why that isolation matters).
    directions = _warm_up_then_alternate(
        warm_up_tick=(63.0, 66.0, FREE_RISE_RATE_F_PER_H, 0.5),
        noise_pair=(65.0, 66.0), beer_target=66.0, rate=FREE_RISE_RATE_F_PER_H,
    )
    assert _count_reversals(directions) == 0


def test_noise_alone_does_not_flip_direction_under_a_falling_ramp():
    # Beer running above a falling ramp's current value for a while
    # (needs cooling), then probe noise on top -- same target throughout,
    # unlike the rising-ramp test above (which legitimately advances the
    # target between warm-up and noise since it's modeling "beer still
    # catching up to a moving target" rather than "noise around a fixed
    # point" -- here we hold the target fixed to isolate noise alone).
    cold_crash_rate = (38.0 - 68.0) / 96.0
    directions = _warm_up_then_alternate(
        warm_up_tick=(63.0, 60.0, cold_crash_rate, 0.5),
        noise_pair=(59.0, 60.0), beer_target=60.0, rate=cold_crash_rate,
    )
    assert _count_reversals(directions) == 0


# --- C. Dramatic movement is fine -- smooth, not capped ------------------

def test_larger_deviations_produce_proportionally_larger_corrections_not_capped():
    # "It's not a problem for setpoints to move dramatically" -- explicit
    # requirement. Increasingly large, genuine deviations must produce
    # increasingly large (not artificially flattened/capped) corrections.
    beer_target = 66.0
    offsets = []
    for beer_temp in (66.0, 68.0, 71.0, 76.0):  # 0, 2, 5, 10 degrees off
        target = chamber_target_for(beer_temp, beer_target, 0.0, 0.0)
        offsets.append(beer_target - target)  # how hard we're pushing to cool
    assert offsets == sorted(offsets), "correction should grow monotonically with the deviation"
    assert offsets[-1] > offsets[0] + 5, "a 10F deviation must produce a meaningfully bigger correction than a 0F one"


def test_a_large_sustained_disturbance_is_not_artificially_damped_by_chatter_prevention():
    # Nothing in this file's chatter-prevention properties should come at
    # the cost of timidity -- a real, large, sustained error should still
    # drive the chamber meaningfully far from the beer target.
    outputs = _replay([(72.0, 66.0, 0.0, 0.5)] * 10)  # 5h sustained 6F error
    assert outputs[-1] < 66.0 - 8.0, "a sustained 6F error for 5h should produce well over 8F of correction"


# --- D. Ramp direction consistency (the actual root-cause fix) -----------

@pytest.mark.parametrize("rate", [FREE_RISE_RATE_F_PER_H, (38.0 - 68.0) / 96.0])
def test_ramp_feedforward_sign_matches_ramp_direction_regardless_of_instantaneous_error_sign(rate: float):
    # The OLD bug: ramp_push_f = abs(rate)/COUPLING, applied to whichever
    # discrete mode the instantaneous error happened to select -- so a
    # rising ramp's push could get applied in the COOLING direction if
    # beer momentarily read above target. The new additive term must be
    # identical regardless of the instantaneous error's own sign.
    beer_target = 66.0
    ramp_component_beer_above = (
        chamber_target_for(67.0, beer_target, 0.0, rate) - chamber_target_for(67.0, beer_target, 0.0, 0.0)
    )
    ramp_component_beer_below = (
        chamber_target_for(65.0, beer_target, 0.0, rate) - chamber_target_for(65.0, beer_target, 0.0, 0.0)
    )
    assert ramp_component_beer_above == pytest.approx(ramp_component_beer_below)
    expected_positive = rate > 0
    assert (ramp_component_beer_above > 0) == expected_positive


# --- E. Parameterized full-ramp scenario ---------------------------------

@pytest.mark.parametrize(
    "rate_f_per_h,lag_f",
    [
        (FREE_RISE_RATE_F_PER_H, 1.0),  # Free Rise's own real rate
        ((38.0 - 68.0) / 96.0, -1.0),  # Cold Crash's own real rate
        (1.0, 1.0),  # steeper than either real stage, still plausible
    ],
)
def test_a_beer_lagging_a_moving_target_never_chatters_across_the_whole_ramp(rate_f_per_h: float, lag_f: float):
    # Beer held at a constant, realistic lag behind a continuously moving
    # ramp target (never fully catching up, the common real case for a
    # fast ramp) -- simulated over the full stage, checked for chatter,
    # not exact physics (that's plant.py/projection.py's job).
    beer_target_start = 66.0
    dt_h = 0.1
    directions = []
    integral = 0.0
    for i in range(60):  # 6 simulated hours
        t_h = i * dt_h
        beer_target = beer_target_start + rate_f_per_h * t_h
        beer_temp = beer_target - lag_f  # lag_f>0: beer trailing a rising ramp; <0: trailing a falling one
        integral = update_beer_error_integral(beer_temp, beer_target, integral, dt_h)
        target = chamber_target_for(beer_temp, beer_target, integral, rate_f_per_h)
        directions.append(_direction(target, beer_target))
    assert _count_reversals(directions) == 0
