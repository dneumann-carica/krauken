from __future__ import annotations

from krauken.contracts.control_constants import ControlTuning
from krauken.contracts.protection import RelayState, next_relay_state

# Small tuning so table-driven tests can use readable numbers instead of the
# real 45/90-minute constants.
T = ControlTuning(min_on_s=10, min_off_s=20, opposite_lockout_s=40)


def test_engages_from_idle_once_min_off_satisfied():
    state = RelayState(mode="idle", held_s=0, last_run=None)
    state = next_relay_state(state, "cool", dt_s=5, tuning=T)
    assert state.mode == "idle"  # min_off (20s) not yet satisfied, no last_run either
    state = next_relay_state(state, "cool", dt_s=20, tuning=T)
    assert state.mode == "cool"
    assert state.held_s == 0.0


def test_min_on_blocks_release_even_when_demand_drops():
    state = RelayState(mode="cool", held_s=0, last_run="cool")
    state = next_relay_state(state, "idle", dt_s=5, tuning=T)
    assert state.mode == "cool"  # min_on (10s) not yet satisfied
    state = next_relay_state(state, "idle", dt_s=10, tuning=T)
    assert state.mode == "idle"


def test_min_on_blocks_release_even_when_demand_flips_to_opposite():
    # demand can jump straight from cool to heat (a big swing) -- the relay
    # still can't skip straight there without releasing first.
    state = RelayState(mode="cool", held_s=5, last_run="cool")
    state = next_relay_state(state, "heat", dt_s=2, tuning=T)
    assert state.mode == "cool"  # still short of min_on (10s)


def test_opposite_lockout_outranks_min_off_when_switching_sides():
    state = RelayState(mode="idle", held_s=0, last_run="cool")
    # min_off (20s) alone would be satisfied here, but switching to heat
    # after cool requires the longer opposite_lockout_s (40s).
    state = next_relay_state(state, "heat", dt_s=25, tuning=T)
    assert state.mode == "idle"
    state = next_relay_state(state, "heat", dt_s=15, tuning=T)  # total 40s
    assert state.mode == "heat"


def test_min_off_applies_when_re_engaging_the_same_side():
    state = RelayState(mode="idle", held_s=0, last_run="cool")
    state = next_relay_state(state, "cool", dt_s=20, tuning=T)  # same side -- just min_off
    assert state.mode == "cool"


def test_board_not_verified_forces_idle_regardless_of_timers():
    state = RelayState(mode="cool", held_s=999, last_run="cool")
    state = next_relay_state(state, "cool", dt_s=1, tuning=T, board_verified=False)
    assert state.mode == "idle"
    assert state.held_s == 0.0


def test_idle_with_no_demand_just_accumulates_held_time():
    state = RelayState(mode="idle", held_s=3, last_run=None)
    state = next_relay_state(state, "idle", dt_s=4, tuning=T)
    assert state.mode == "idle"
    assert state.held_s == 7


def _run(tuning: ControlTuning, demands: list[str], dt_s: float) -> list[RelayState]:
    state = RelayState()
    history = [state]
    for d in demands:
        state = next_relay_state(state, d, dt_s, tuning)
        history.append(state)
    return history


def test_long_run_never_violates_min_on_min_off_or_lockout():
    """Deterministic stand-in for a property test: drive the state machine
    through every demand transition (idle/cool/heat in all orders) at a
    fine timestep and assert the real invariants hold over the whole run --
    a relay never engages for less than min_on, never re-engages the same
    side sooner than min_off, and never switches sides sooner than the
    opposite lockout."""
    tuning = ControlTuning(min_on_s=45 * 60, min_off_s=90 * 60, opposite_lockout_s=4 * 60 * 60)
    demand_cycle = ["cool", "cool", "idle", "heat", "heat", "idle", "cool", "idle", "idle", "heat"]
    demands = demand_cycle * 200
    dt_s = 30.0
    history = _run(tuning, demands, dt_s)

    # Reconstruct engaged runs: (mode, start_index, end_index)
    runs = []
    run_start = 0
    for i in range(1, len(history)):
        if history[i].mode != history[i - 1].mode:
            runs.append((history[i - 1].mode, run_start, i - 1))
            run_start = i
    runs.append((history[-1].mode, run_start, len(history) - 1))

    engaged_runs = [r for r in runs if r[0] != "idle"]
    for mode, start, end in engaged_runs:
        duration_s = (end - start) * dt_s
        assert duration_s >= tuning.min_on_s - dt_s, f"{mode} run at {start} lasted only {duration_s}s"

    for i in range(1, len(engaged_runs)):
        prev_mode, _, prev_end = engaged_runs[i - 1]
        this_mode, this_start, _ = engaged_runs[i]
        idle_s = (this_start - prev_end) * dt_s
        required = tuning.opposite_lockout_s if this_mode != prev_mode else tuning.min_off_s
        assert idle_s >= required - dt_s, (
            f"only {idle_s}s idle between {prev_mode} run ending at {prev_end} and "
            f"{this_mode} run starting at {this_start} (required {required}s)"
        )
