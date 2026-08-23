"""db/seed.py's _scan_and_wait polling loop -- the piece that actually
raced against discovery.py's own fire-and-forget scan task on real
Raspberry Pi hardware. The old budget (50 x 0.02s = 1.0s total) gave up
while a real out-of-process scan (separate Simulator/Manual service
processes, real Unix sockets, ARM CPU) was still genuinely running.

Fakes the IPC client's hardware.scan_start/hardware.scan_status calls
rather than spinning up a real daemon+scan (that's covered end-to-end by
tests/scenarios/test_full_fermentation.py and
test_discovery_scan_shutdown.py already) -- this file only needs to prove
_scan_and_wait's own polling/budget arithmetic. asyncio.sleep is
monkeypatched to a no-op so a "slow" scan costs no real wall-clock test
time; only the number of polls survived before completing/giving up
matters here, not real elapsed time.
"""
from __future__ import annotations

from typing import Any

import pytest

from krauken.db import seed


class _FakeScanClient:
    """Reports "running" for hardware.scan_status until `completes_after`
    status polls have happened, then "complete" -- stands in for a real
    AsyncIPCClient talking to a scan that takes a while to actually
    finish."""

    def __init__(self, completes_after: int) -> None:
        self._completes_after = completes_after
        self.status_calls = 0

    async def call(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if op == "hardware.scan_start":
            return {"scan_id": "fake-scan"}
        assert op == "hardware.scan_status"
        self.status_calls += 1
        state = "complete" if self.status_calls >= self._completes_after else "running"
        return {"state": state}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch):
    """The poll loop's own asyncio.sleep(SCAN_WAIT_POLL_INTERVAL_S) is a
    real wall-clock wait by design (it's pacing an actual IPC round trip in
    production) -- faked here to instant so this file can exercise dozens
    of simulated poll intervals without costing real test time."""
    async def _instant_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(seed.asyncio, "sleep", _instant_sleep)


async def test_scan_and_wait_survives_a_scan_slower_than_the_old_one_second_budget():
    # ~2.5 simulated seconds of scan time, expressed in the module's own
    # real poll interval -- comfortably past the OLD budget (50 x 0.02s =
    # 1.0s total simulated time), well inside the new one
    # (DEFAULT_SCAN_BUDGET_S + 5s margin).
    simulated_scan_seconds = 2.5
    assert simulated_scan_seconds > 1.0, "test doesn't actually exceed the old 1.0s budget"
    completes_after = int(simulated_scan_seconds / seed.SCAN_WAIT_POLL_INTERVAL_S)
    client = _FakeScanClient(completes_after=completes_after)

    await seed._scan_and_wait(client)  # must return, not raise

    assert client.status_calls == completes_after


async def test_scan_and_wait_still_gives_up_if_the_scan_never_completes():
    # A scan that never reports "complete" within the new, wider budget
    # must still raise cleanly -- this loop is a bounded wait, not an
    # infinite one.
    client = _FakeScanClient(completes_after=10**9)

    with pytest.raises(AssertionError):
        await seed._scan_and_wait(client)

    expected_attempts = int(seed.SCAN_WAIT_BUDGET_S / seed.SCAN_WAIT_POLL_INTERVAL_S)
    assert client.status_calls == expected_attempts
