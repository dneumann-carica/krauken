"""The variable-interval sampling policy (design doc section 9): a `samples`
row is written when a tracked value moves beyond a small threshold, the
chamber mode changes, a discrete event occurs, or a heartbeat interval
elapses with nothing else to write -- never on a fixed cadence. A data gap
in the resulting series always means the daemon stopped, never "nothing
changed."

Pure and reusable: the live control loop (M2) and the offline demo-batch
generator (this milestone) both decide "should I write a row right now"
through the exact same function, so the demo batch's data shape is a
genuine exercise of the real policy, not a separately-invented one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WriteReason = Literal["change", "mode_change", "event", "heartbeat", "boot"]

DEFAULT_TEMP_THRESHOLD_F = 0.2
DEFAULT_GRAVITY_THRESHOLD = 0.0008
DEFAULT_HEARTBEAT_S = 20 * 60


@dataclass(frozen=True, slots=True)
class SampleCandidate:
    ts: float
    beer_temp_f: float | None
    chamber_temp_f: float | None
    gravity: float | None
    chamber_mode: str


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    temp_threshold_f: float = DEFAULT_TEMP_THRESHOLD_F
    gravity_threshold: float = DEFAULT_GRAVITY_THRESHOLD
    heartbeat_s: float = DEFAULT_HEARTBEAT_S

    def should_write(
        self, candidate: SampleCandidate, last_written: SampleCandidate | None
    ) -> WriteReason | None:
        """None = skip this candidate. A non-None return is the write_reason
        to persist."""
        if last_written is None:
            return "boot"
        if candidate.chamber_mode != last_written.chamber_mode:
            return "mode_change"
        if _moved(candidate.beer_temp_f, last_written.beer_temp_f, self.temp_threshold_f):
            return "change"
        if _moved(candidate.chamber_temp_f, last_written.chamber_temp_f, self.temp_threshold_f):
            return "change"
        if _moved(candidate.gravity, last_written.gravity, self.gravity_threshold):
            return "change"
        if candidate.ts - last_written.ts >= self.heartbeat_s:
            return "heartbeat"
        return None


def _moved(current: float | None, previous: float | None, threshold: float) -> bool:
    if current is None or previous is None:
        return current is not previous
    return abs(current - previous) >= threshold
