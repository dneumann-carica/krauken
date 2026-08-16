"""daemon/tests_runtime.py's confirm_heater test job: a real functional
test against whatever ChamberDriver is mapped -- forces a setpoint, polls
the driver's own reported ChamberMode for HEAT, always releases back to
idle when it's done. Exercised here against a fake ChamberDriver (not the
real Simulator engine) so the anti-short-cycle timing this job is
specifically built to tolerate (MIN_OFF_S=5min/OPPOSITE_LOCKOUT_S=30min,
see contracts/control_constants.py) doesn't cost real wall-clock test
time -- the API-level suite (tests/api/test_hardware_tests.py) already
covers the real Simulator/Manual round trip for fire_outlet/identify_probes
the same way; this one only needs to prove _run_confirm_heater's own
logic (confirm/timeout/release/cancel), which a fake driver does far
faster and more precisely than waiting out the real protection timers
would."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from krauken.contracts.errors import FermentationAlreadyActive
from krauken.contracts.models import ChamberMode, ChamberReading, Health
from krauken.daemon import tests_runtime


@dataclass
class _FakeChamberDriver:
    """mode_at(call_index) decides what read_chamber() reports on each
    successive call -- call 0 is always the baseline read before a target
    is ever forced, call 1+ are the poll loop's own reads."""

    modes_after_baseline: list[ChamberMode]
    baseline_temp_f: float = 60.0
    target_calls: list[float | None] = field(default_factory=list)
    _read_count: int = 0

    async def read_chamber(self) -> ChamberReading:
        if self._read_count == 0:
            mode = ChamberMode.IDLE
        else:
            idx = min(self._read_count - 1, len(self.modes_after_baseline) - 1)
            mode = self.modes_after_baseline[idx]
        self._read_count += 1
        return ChamberReading(temp_f=self.baseline_temp_f, mode=mode, health=Health.OK, last_good_ts=0.0)

    async def set_target(self, temp_f: float | None) -> None:
        self.target_calls.append(temp_f)


class _FakeClock:
    """Never actually waits -- same shape as SimulatorClock's own
    contract, but this test doesn't need the real Simulator engine's
    protection-timer physics behind it, just something _run_confirm_heater
    can call .sleep() on without spending real wall-clock time."""

    def now(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


class _FakeCtx:
    def __init__(self, driver: Any):
        self.clock = _FakeClock()
        self.jobs: dict[str, Any] = {}
        self._driver = driver

    @property
    def conn(self) -> Any:
        return None  # only touched via queries.active_fermentation, patched per-test


def _patch_driver(monkeypatch: pytest.MonkeyPatch, driver: Any) -> None:
    monkeypatch.setattr(tests_runtime.drivers, "chamber_driver", lambda ctx, platform, device_id=None: driver)


def _patch_no_active_fermentation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tests_runtime.queries, "active_fermentation", lambda conn: None)


async def test_confirm_heater_completes_confirmed_once_the_driver_reports_heat(monkeypatch: pytest.MonkeyPatch):
    driver = _FakeChamberDriver(modes_after_baseline=[ChamberMode.IDLE, ChamberMode.IDLE, ChamberMode.HEAT])
    _patch_driver(monkeypatch, driver)
    _patch_no_active_fermentation(monkeypatch)
    ctx = _FakeCtx(driver)

    result = tests_runtime.start_test(ctx, "brewpi:controller", "confirm_heater", {"window_s": 30.0})
    assert result["state"] == "running"
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["confirmed"] is True
    # Forced the setpoint above baseline, then released back to idle --
    # the LAST call must always be the None release, whatever else ran.
    assert driver.target_calls[0] == driver.baseline_temp_f + tests_runtime.CONFIRM_HEATER_FORCE_DELTA_F
    assert driver.target_calls[-1] is None


async def test_confirm_heater_completes_unconfirmed_when_heat_never_engages(monkeypatch: pytest.MonkeyPatch):
    driver = _FakeChamberDriver(modes_after_baseline=[ChamberMode.IDLE])
    _patch_driver(monkeypatch, driver)
    _patch_no_active_fermentation(monkeypatch)
    ctx = _FakeCtx(driver)

    # A short window -- FakeClock's sleep() doesn't need real time to
    # elapse, so this only bounds how many poll iterations run, not any
    # real wall-clock duration.
    result = tests_runtime.start_test(ctx, "brewpi:controller", "confirm_heater", {"window_s": 6.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["confirmed"] is False
    # Never confirmed, but still released -- a timed-out test must never
    # leave the chamber sitting at the forced setpoint.
    assert driver.target_calls[-1] is None


async def test_confirm_heater_fails_cleanly_when_the_chamber_probe_isnt_reading(monkeypatch: pytest.MonkeyPatch):
    driver = _FakeChamberDriver(modes_after_baseline=[ChamberMode.HEAT], baseline_temp_f=None)  # type: ignore[arg-type]
    _patch_driver(monkeypatch, driver)
    _patch_no_active_fermentation(monkeypatch)
    ctx = _FakeCtx(driver)

    result = tests_runtime.start_test(ctx, "brewpi:controller", "confirm_heater", {"window_s": 6.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "failed"
    assert "chamber probe" in job.error
    # Never forced a target at all -- nothing to release.
    assert driver.target_calls == []


async def test_confirm_heater_releases_the_target_when_cancelled_mid_test(monkeypatch: pytest.MonkeyPatch):
    driver = _FakeChamberDriver(modes_after_baseline=[ChamberMode.IDLE] * 1000)
    _patch_driver(monkeypatch, driver)
    _patch_no_active_fermentation(monkeypatch)
    ctx = _FakeCtx(driver)

    result = tests_runtime.start_test(ctx, "brewpi:controller", "confirm_heater", {"window_s": 600.0})
    job = ctx.jobs[result["test_id"]]
    await asyncio.sleep(0)  # let the task make at least one poll pass

    final = await tests_runtime.cancel_test(ctx, result["test_id"])
    assert final["state"] == "cancelled"
    assert driver.target_calls[-1] is None


async def test_confirm_heater_is_blocked_while_a_fermentation_is_active(monkeypatch: pytest.MonkeyPatch):
    driver = _FakeChamberDriver(modes_after_baseline=[ChamberMode.HEAT])
    _patch_driver(monkeypatch, driver)
    monkeypatch.setattr(tests_runtime.queries, "active_fermentation", lambda conn: {"id": 1})
    ctx = _FakeCtx(driver)

    with pytest.raises(FermentationAlreadyActive):
        tests_runtime.start_test(ctx, "brewpi:controller", "confirm_heater", {"window_s": 6.0})
    # Blocked before ever forcing anything.
    assert driver.target_calls == []
