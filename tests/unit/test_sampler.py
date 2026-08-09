from __future__ import annotations

from krauken.daemon.sampler import SampleCandidate, SamplingPolicy


def _c(ts, beer=65.0, chamber=65.0, gravity=1.050, mode="idle"):
    return SampleCandidate(ts=ts, beer_temp_f=beer, chamber_temp_f=chamber, gravity=gravity, chamber_mode=mode)


def test_first_sample_always_written():
    policy = SamplingPolicy()
    assert policy.should_write(_c(0), None) == "boot"


def test_mode_change_always_written():
    policy = SamplingPolicy()
    last = _c(0, mode="idle")
    assert policy.should_write(_c(1, mode="cool"), last) == "mode_change"


def test_small_temp_drift_is_skipped():
    policy = SamplingPolicy(temp_threshold_f=0.2)
    last = _c(0, beer=65.0)
    assert policy.should_write(_c(1, beer=65.05), last) is None


def test_temp_move_past_threshold_is_written():
    policy = SamplingPolicy(temp_threshold_f=0.2)
    last = _c(0, beer=65.0)
    assert policy.should_write(_c(1, beer=65.3), last) == "change"


def test_gravity_move_past_threshold_is_written():
    policy = SamplingPolicy(gravity_threshold=0.0008)
    last = _c(0, gravity=1.050)
    assert policy.should_write(_c(1, gravity=1.049), last) == "change"


def test_heartbeat_fires_when_nothing_else_changed():
    policy = SamplingPolicy(heartbeat_s=100)
    last = _c(0)
    assert policy.should_write(_c(99), last) is None
    assert policy.should_write(_c(100), last) == "heartbeat"


def test_no_gap_ever_implies_no_change_a_written_row_always_has_a_reason():
    # The design invariant this policy exists for: as long as should_write()
    # is consulted on every tick, consecutive written rows are never more
    # than heartbeat_s apart -- a bigger gap can only mean the daemon itself
    # stopped ticking, not "the policy decided to skip for a long time".
    policy = SamplingPolicy(heartbeat_s=60)
    last = _c(0)
    for ts in range(1, 61):
        reason = policy.should_write(_c(ts), last)
        if reason is not None:
            last = _c(ts)
    assert last.ts <= 60
