"""Behavioral/scenario tests for the PI cascade's setpoint STABILITY --
distinct from test_cascade.py's pure per-term unit tests (P, I, anti-
windup, envelope clamp in isolation). This file formalizes and proves one
specific, explicit requirement: dramatic setpoint movement is fine and
expected when the beer genuinely needs it, but the cascade must never
rapidly reverse between a cooling demand and a heating demand the way the
OLD bang-bang cascade did.

Confirmed real-world motivation, not hypothetical: on 2026-08-24, the
currently-deployed OLD cascade (fixed-clamp bang-bang, since replaced --
see contracts/cascade.py's module docstring) was observed live on
fermentation 2's Free Rise stage (a 66->70F ramp over 6h) swinging
chamber_target_f between ~53F ("cool" mode, the ramp's abs(rate)-derived
push applied in the WRONG direction) and ~80F ("heat" mode, same push now
right-signed) on almost every tick, as the beer probe read 66/67 right at
the ramp's current value. Test A below replays that exact recorded
sequence through the new PI functions.

chamber_target_for() takes no ramp-rate input at all (a later revision of
this same PI rewrite removed a separate ramp-feedforward term entirely --
see cascade.py's own module docstring for why: redundant with, and later
confirmed live to be actively harmful alongside, the integral term, which
already supplies whatever sustained offset a ramping target needs on its
own). A ramping scenario here is just beer_target_f moving tick to tick,
exactly like control_loop.py feeds it -- no separate mechanism to test.

Section E adds the derivative term (cascade.py's update_closing_rate_filter(),
added after this same real batch's Free Rise stage showed P+I alone
leaving real ramp-tracking headroom unused once INTEGRAL_MAX_F_H
saturates -- see cascade.py's own module docstring and
control_constants.py's BEER_KD_F_PER_F_PER_H comment for the full
reasoning and the simulation work that grounded the tuned values). Every
scenario test above this section already exercises the derivative term
too, via _replay()'s own threading -- Section E specifically validates
the NEW behaviors it was added for (real ramp-lag reduction, noise
robustness, sensor-artifact robustness) against the real, live production
constants, not hand-picked values.
"""
from __future__ import annotations

from krauken.contracts.cascade import chamber_target_for, update_beer_error_integral, update_closing_rate_filter
from krauken.contracts.control_constants import (
    CHAMBER_TARGET_MAX_F,
    CHAMBER_TARGET_MIN_F,
    MAX_PLAUSIBLE_BEER_RATE_F_PER_H,
)


def _direction(chamber_target_f: float, beer_target_f: float, threshold_f: float = 1.0) -> int:
    """-1 (cooling demand), +1 (heating demand), 0 (holding) -- compares
    the cascade's own output against the beer target, its natural zero-
    reference (error=0, integral=0 gives chamber_target_f ==
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


def _replay(ticks: list[tuple[float, float, float]]) -> list[float]:
    """Threads update_beer_error_integral()/update_closing_rate_filter() ->
    chamber_target_for() across a sequence of (beer_temp_f, beer_target_f,
    dt_h) ticks, integral and closing-rate filter both starting at their
    fresh-ControlState defaults (0.0/None) -- the shared engine every
    scenario test in this file drives, exercising the REAL production
    control_loop.py tick order (integral, then closing-rate filter, then
    chamber_target_for(), then remember this tick's own readings for
    next tick's rate) rather than a simplified P+I-only replay."""
    integral = 0.0
    closing_rate_filtered = 0.0
    prev_beer_temp_f: float | None = None
    prev_beer_target_f: float | None = None
    outputs = []
    for beer_temp_f, beer_target_f, dt_h in ticks:
        integral = update_beer_error_integral(beer_temp_f, beer_target_f, integral, dt_h)
        closing_rate_filtered = update_closing_rate_filter(
            beer_temp_f, beer_target_f, prev_beer_temp_f, prev_beer_target_f, closing_rate_filtered, dt_h,
        )
        outputs.append(chamber_target_for(beer_temp_f, beer_target_f, integral, closing_rate_filtered))
        prev_beer_temp_f, prev_beer_target_f = beer_temp_f, beer_target_f
    return outputs


# --- A. Real-incident regression replay ---------------------------------

# Exact (beer_temp_f, effective_target_f, dt_h) pulled directly from the
# real fermentation's `samples` table, 2026-08-24T08:28:17Z through
# 09:16:18Z -- the precise sequence that made the OLD cascade flip
# cooling<->heating repeatedly.
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
    outputs = _replay(REAL_INCIDENT_TICKS)
    targets = [target for _, target, _ in REAL_INCIDENT_TICKS]
    return outputs, targets


def test_real_incident_replay_produces_at_most_one_direction_reversal():
    # The exact sequence that made the OLD cascade flip cooling->heating
    # repeatedly (see this file's module docstring) must not do so here.
    #
    # With the derivative term (Section E) this now allows for exactly ONE
    # transition, not zero: beer sits flat at 67.0F for the first ~45
    # ticks while the target keeps slowly rising underneath it (a real
    # ~0.5F gap opens up), and the derivative term correctly recognizes
    # "falling behind a moving target" and escalates from a mild cooling
    # push to a real heating push -- exactly the behavior it was added
    # for, not a regression of this file's own no-chatter requirement.
    # What actually matters -- verified below, not just asserted -- is
    # that it's ONE clean, permanent transition (separated by ~23 neutral
    # ticks from the last cooling one), never flip-flopping back, which is
    # qualitatively nothing like the old bug's every-tick oscillation.
    outputs, targets = _replay_real_incident()
    directions = [_direction(o, t) for o, t in zip(outputs, targets)]
    assert _count_reversals(directions) <= 1
    first_heat_i = next(i for i, d in enumerate(directions) if d == 1)
    assert all(d != -1 for d in directions[first_heat_i:]), (
        "once the cascade escalates to a heating demand here, it must never flip back to cooling -- "
        "that WOULD be the old bug's chatter, a single one-way transition is not"
    )


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

def _warm_up_then_alternate(
    warm_up_tick: tuple[float, float, float], noise_pair: tuple[float, float], beer_target: float,
    n_noise_ticks: int = 20,
) -> list[int]:
    warm_up = [warm_up_tick] * 6  # ~a few hours of sustained real disturbance -> a genuine, nonzero integral bias
    noise = [(noise_pair[i % 2], beer_target, 0.0083) for i in range(n_noise_ticks)]
    outputs = _replay(warm_up + noise)
    targets = [beer_target] * (len(warm_up) + len(noise))
    return [_direction(o, t) for o, t in zip(outputs, targets)]


def test_noise_alone_does_not_flip_direction_under_a_held_target_with_an_existing_bias():
    # Beer ran warm (69F vs a 66F target) for a while first -- a real,
    # nonzero cooling bias in the integral -- then the probe starts
    # alternating 66/67F (quantization noise) around the still-held
    # target. Noise alone must not flip the demand direction.
    directions = _warm_up_then_alternate(
        warm_up_tick=(69.0, 66.0, 0.5), noise_pair=(66.0, 67.0), beer_target=66.0,
    )
    assert _count_reversals(directions) == 0


def test_noise_alone_does_not_flip_direction_while_beer_lags_a_moving_target():
    # Beer lagging behind a continuously-rising target for a while (needs
    # heat), then probe noise on top -- the ramp itself is just
    # beer_target_f moving tick to tick, same as control_loop.py feeds it.
    rate = (70.0 - 66.0) / 6.0  # Free Rise's own real rate
    ticks = []
    beer_target = 63.0
    # Warm-up: beer trailing a rising target for a few hours -- a real,
    # nonzero heating bias in the integral.
    for _ in range(6):
        beer_target += rate * 0.5
        ticks.append((beer_target - 3.0, beer_target, 0.5))
    # Noise: probe alternating +/-0.5F around the still-moving target.
    for i in range(20):
        beer_target += rate * 0.0083
        ticks.append((beer_target - 3.0 + (0.5 if i % 2 == 0 else -0.5), beer_target, 0.0083))
    outputs = _replay(ticks)
    targets = [t for _, t, _ in ticks]
    directions = [_direction(o, t) for o, t in zip(outputs, targets)]
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
    outputs = _replay([(72.0, 66.0, 0.5)] * 10)  # 5h sustained 6F error
    assert outputs[-1] < 66.0 - 8.0, "a sustained 6F error for 5h should produce well over 8F of correction"


# --- D. Full-ramp scenario, beer perpetually lagging ---------------------

def test_a_beer_lagging_a_gentle_moving_target_never_chatters_across_the_whole_ramp():
    # Cold Crash's own real rate -- INTEGRAL_MAX_F_H is sized to let the
    # integral fully track this one (see that constant's own comment) --
    # beer should smoothly close in on a small, stable lag, no chatter.
    rate_f_per_h = (38.0 - 68.0) / 96.0
    beer_target = 68.0
    dt_h = 0.1
    ticks = []
    for _ in range(60):  # 6 simulated hours
        beer_target += rate_f_per_h * dt_h
        ticks.append((beer_target - (-1.0), beer_target, dt_h))  # trailing a falling ramp: beer stays slightly above it
    outputs = _replay(ticks)
    targets = [t for _, t, _ in ticks]
    directions = [_direction(o, t) for o, t in zip(outputs, targets)]
    assert _count_reversals(directions) == 0


def test_a_beer_lagging_a_steep_moving_target_saturates_smoothly_not_via_chatter():
    # Free Rise's own real rate (1.5F/h as configured live) is steeper
    # than INTEGRAL_MAX_F_H can fully track (see that constant's own
    # comment) -- beer legitimately, permanently lags this ramp. The
    # requirement this test protects isn't "catches up" (it structurally
    # can't, and shouldn't be forced to), it's "still never chatters" even
    # while saturated at the envelope.
    rate_f_per_h = (75.0 - 66.0) / 6.0
    beer_target = 66.0
    dt_h = 0.1
    ticks = []
    for _ in range(60):  # 6 simulated hours
        beer_target += rate_f_per_h * dt_h
        ticks.append((beer_target - 1.0, beer_target, dt_h))  # a real, sustained lag behind the ramp
    outputs = _replay(ticks)
    targets = [t for _, t, _ in ticks]
    directions = [_direction(o, t) for o, t in zip(outputs, targets)]
    assert _count_reversals(directions) == 0


# --- E. Derivative term: the specific behaviors it was added for ---------
#
# Closed-loop (not single-formula-call) validations -- beer_temp actually
# evolves in response to the commanded chamber_target_f each tick, via the
# same simple first-order coupling plant.py's own PlantParams.
# beer_chamber_coupling documents (0.05/h) and BEER_KI_F_PER_F_H's own
# derivation already cites -- these tests aren't asking "does the formula
# produce X", they're asking "does the closed loop actually behave
# better", the same question the simulation work behind
# BEER_KD_F_PER_F_PER_H/CLOSING_RATE_FILTER_TAU_H/
# MAX_PLAUSIBLE_BEER_RATE_F_PER_H answered before those were chosen.

_BEER_CHAMBER_COUPLING_PER_H = 0.05  # plant.py's PlantParams.beer_chamber_coupling


def _simulate_closed_loop(beer_temp_f: float, target_fn, hours: float, dt_h: float = 1 / 60) -> list[tuple[float, float, float]]:
    """Returns [(t_h, beer_temp_f, beer_target_f), ...], stepping beer_temp_f
    toward whatever chamber_target_for() commands each tick via the same
    coupling constant plant.py uses. No exotherm/ambient forcing -- this
    isolates the derivative term's own effect from the simulator's other
    physics, the same isolation Section A/B/C/D already apply to P/I."""
    integral = 0.0
    closing_rate_filtered = 0.0
    prev_beer_temp_f: float | None = None
    prev_beer_target_f: float | None = None
    t_h = 0.0
    trace = []
    n = int(hours / dt_h)
    for _ in range(n):
        target = target_fn(t_h)
        integral = update_beer_error_integral(beer_temp_f, target, integral, dt_h)
        closing_rate_filtered = update_closing_rate_filter(
            beer_temp_f, target, prev_beer_temp_f, prev_beer_target_f, closing_rate_filtered, dt_h,
        )
        ctarget = chamber_target_for(beer_temp_f, target, integral, closing_rate_filtered)
        prev_beer_temp_f, prev_beer_target_f = beer_temp_f, target
        beer_temp_f += _BEER_CHAMBER_COUPLING_PER_H * (ctarget - beer_temp_f) * dt_h
        t_h += dt_h
        trace.append((t_h, beer_temp_f, target))
    return trace


def _free_rise_target(t_h: float, from_f: float = 66.0, to_f: float = 75.0, ramp_h: float = 6.0) -> float:
    if t_h >= ramp_h:
        return to_f
    return from_f + (to_f - from_f) * t_h / ramp_h


def test_derivative_term_meaningfully_reduces_free_rise_ramp_lag_vs_pi_alone():
    # The real regression this term was added for: Free Rise's authored
    # rate (1.5F/h) saturates INTEGRAL_MAX_F_H almost immediately (see that
    # constant's own comment), and P+I alone left real ramp-tracking
    # headroom unused -- confirmed live, fermentation 3's Free Rise stage
    # showed a 3.5F lag at a comparable point in the ramp. Simulated
    # P+I-only (closing_rate_filtered_f_per_h pinned at 0.0 the whole
    # time) gives ~3.32F lag at t=3.33h; the real, D-term-active closed
    # loop should meaningfully beat that.
    trace = _simulate_closed_loop(66.0, _free_rise_target, hours=3.34)
    t_h, beer_temp_f, target = trace[-1]
    lag = target - beer_temp_f
    assert lag < 3.0, f"expected the derivative term to meaningfully improve on P+I-alone's ~3.3F lag, got {lag:.2f}F"


def test_derivative_term_does_not_meaningfully_change_sustained_disturbance_tracking():
    # The property the earlier PI redesign was built to deliver (near-zero
    # steady-state error against a real sustained disturbance) must survive
    # adding the derivative term -- it's referenced against the TARGET's
    # own observed rate specifically so it doesn't fight a converged
    # steady state (see cascade.py's own module docstring). Modeled here
    # as a constant external push (not plant.py's exotherm curve, which
    # peaks and fades -- a constant push is the harder, sustained case),
    # the same way BEER_KI_F_PER_F_H's own derivation reasons about the
    # exotherm's peak rate.
    def held_target(t_h: float) -> float:
        return 66.0

    integral = 0.0
    closing_rate_filtered = 0.0
    prev_beer_temp_f: float | None = None
    prev_beer_target_f: float | None = None
    beer_temp_f = 66.0
    dt_h = 1 / 60
    disturbance_f_per_h = 0.22  # plant.py's ExothermParams.peak_f_per_h
    for _ in range(int(48 / dt_h)):
        target = held_target(0.0)
        integral = update_beer_error_integral(beer_temp_f, target, integral, dt_h)
        closing_rate_filtered = update_closing_rate_filter(
            beer_temp_f, target, prev_beer_temp_f, prev_beer_target_f, closing_rate_filtered, dt_h,
        )
        ctarget = chamber_target_for(beer_temp_f, target, integral, closing_rate_filtered)
        prev_beer_temp_f, prev_beer_target_f = beer_temp_f, target
        beer_temp_f += (_BEER_CHAMBER_COUPLING_PER_H * (ctarget - beer_temp_f) + disturbance_f_per_h) * dt_h
    assert abs(66.0 - beer_temp_f) < 0.05, (
        f"steady-state error against a sustained disturbance grew to {66.0 - beer_temp_f:+.3f}F "
        "-- the derivative term must not degrade this"
    )


def test_derivative_term_stays_bounded_against_the_real_recorded_sensor_artifact():
    # The exact real probe-settling artifact this fermentation's own
    # history produced (see cascade.py's module docstring and
    # MAX_PLAUSIBLE_BEER_RATE_F_PER_H's own comment): beer read 67.94F,
    # then 65.29F ~2 minutes later -- an implied rate of roughly -159F/h.
    # Without the raw-rate clamp, this alone drove the commanded chamber
    # target straight to CHAMBER_TARGET_MAX_F on the very next tick.
    ticks = [
        (67.94, 66.0, 0.0),
        (65.29, 66.0, 2.0 / 60),
        (63.42, 66.0, 1.0 / 60),
        (62.52, 66.0, 1.0 / 60),
        (62.19, 66.0, 1.0 / 60),
    ]
    outputs = _replay(ticks)
    assert max(outputs) < CHAMBER_TARGET_MAX_F - 5.0, (
        f"a single sensor-settling glitch should not slam the chamber target near the envelope edge, "
        f"got max={max(outputs):.2f}F"
    )


def test_closing_rate_filter_rejects_a_rate_faster_than_any_real_beer_could_produce():
    # Direct regression pin on the clamp itself, independent of the
    # artifact scenario above -- an even more extreme implied rate must
    # still be bounded to MAX_PLAUSIBLE_BEER_RATE_F_PER_H before filtering.
    dt_h = 1.0 / 3600.0  # one second
    rate = update_closing_rate_filter(70.0, 66.0, 60.0, 66.0, 0.0, dt_h)  # implied 36000F/h
    alpha = dt_h / (4.0 + dt_h)  # CLOSING_RATE_FILTER_TAU_H
    max_possible_magnitude = alpha * MAX_PLAUSIBLE_BEER_RATE_F_PER_H
    assert abs(rate) <= max_possible_magnitude + 1e-9
