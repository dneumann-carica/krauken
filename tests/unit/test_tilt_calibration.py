"""platforms/tilt/calibration.py's calibrate_gravity test runner,
dispatched through the REAL daemon/tests_runtime.py:start_test() fallback
path (the same PLATFORM_BINDINGS["tilt"].test_runners lookup production
code uses) against a real (tmp_path) sqlite db and a TiltScanner with
hand-seeded readings -- mirrors test_brewpi_device_config.py's own
"dispatch through the real thing, fake only the hardware" shape."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from krauken.contracts.clock import SimulatorClock
from krauken.daemon import tests_runtime
from krauken.db import queries, writes
from krauken.db.connection import open_rw
from krauken.db.migrate import migrate
from krauken.platforms.tilt import calibration
from krauken.platforms.tilt.calibration import TILT_CALIBRATION_SETTING_KEY
from krauken.platforms.tilt.scanner import TiltReading, TiltScanner

DEVICE_ID = "tilt:orange"


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "krauken.db"
    migrate(db_path)
    c = open_rw(db_path)
    yield c
    c.close()


def _scanner_with(clock: SimulatorClock, color: str, temp_f: float, gravity_sg: float) -> TiltScanner:
    s = TiltScanner(clock, hci_device=0)
    s._readings[color] = TiltReading(temp_f, gravity_sg, rssi=-58, last_seen_monotonic=clock.monotonic())
    return s


class _FakeRegistry:
    def __init__(self, scanner: TiltScanner):
        self._scanner = scanner

    def state_for(self, platform_id: str) -> Any | None:
        return self._scanner if platform_id == "tilt" else None


class _Ctx:
    def __init__(self, conn: Any, scanner: TiltScanner):
        self.clock = SimulatorClock()
        self.jobs: dict[str, Any] = {}
        self.conn = conn
        self.db_lock = asyncio.Lock()
        self.registry = _FakeRegistry(scanner)


def _run(ctx: _Ctx, params: dict[str, Any]) -> Any:
    job_dict = tests_runtime.start_test(ctx, DEVICE_ID, "calibrate_gravity", params)
    return ctx.jobs[job_dict["test_id"]]


async def test_calibrate_gravity_computes_offset_from_the_true_raw_reading(conn):
    clock = SimulatorClock()
    scanner = _scanner_with(clock, "orange", 65.0, 1.033)
    ctx = _Ctx(conn, scanner)

    job = _run(ctx, {"actual_sg": 1.042})
    await job._task

    assert job.state == "completed"
    assert job.result["color"] == "orange"
    assert job.result["raw_sg"] == pytest.approx(1.033)
    assert job.result["offset_sg"] == pytest.approx(0.009)
    assert job.result["calibrated_sg"] == pytest.approx(1.042)


async def test_calibrate_gravity_applies_immediately_to_the_live_scanner(conn):
    clock = SimulatorClock()
    scanner = _scanner_with(clock, "orange", 65.0, 1.033)
    ctx = _Ctx(conn, scanner)

    await _run(ctx, {"actual_sg": 1.042})._task

    assert scanner.latest("orange").gravity_sg == pytest.approx(1.042)
    # raw_latest() must stay untouched -- the calibration only ever
    # applies at the latest()/read boundary, never to the cache itself.
    assert scanner.raw_latest("orange").gravity_sg == pytest.approx(1.033)


async def test_calibrate_gravity_persists_through_the_settings_table(conn):
    clock = SimulatorClock()
    scanner = _scanner_with(clock, "orange", 65.0, 1.033)
    ctx = _Ctx(conn, scanner)

    await _run(ctx, {"actual_sg": 1.042})._task

    persisted = queries.setting(conn, TILT_CALIBRATION_SETTING_KEY)
    assert persisted["orange"] == pytest.approx(0.009)


async def test_recalibrating_does_not_compound_the_offset(conn):
    """A second calibration against the SAME underlying raw reading, now
    targeting a different actual_sg, must compute a fresh offset from the
    true raw value (1.033) -- not from the already-calibrated 1.042 --
    or repeated recalibration would silently compound."""
    clock = SimulatorClock()
    scanner = _scanner_with(clock, "orange", 65.0, 1.033)
    ctx = _Ctx(conn, scanner)

    await _run(ctx, {"actual_sg": 1.042})._task
    assert scanner.latest("orange").gravity_sg == pytest.approx(1.042)

    await _run(ctx, {"actual_sg": 1.040})._task
    assert scanner.latest("orange").gravity_sg == pytest.approx(1.040)


async def test_calibrate_gravity_fails_with_no_live_reading(conn):
    clock = SimulatorClock()
    scanner = TiltScanner(clock, hci_device=0)  # nothing ever seen for "orange"
    ctx = _Ctx(conn, scanner)

    job = _run(ctx, {"actual_sg": 1.042})
    await job._task

    assert job.state == "failed"
    assert "no live reading" in job.error
    assert queries.setting(conn, TILT_CALIBRATION_SETTING_KEY) is None


async def test_calibrate_gravity_rejects_non_numeric_actual_sg(conn):
    clock = SimulatorClock()
    scanner = _scanner_with(clock, "orange", 65.0, 1.033)
    ctx = _Ctx(conn, scanner)

    job = _run(ctx, {"actual_sg": "not a number"})
    await job._task

    assert job.state == "failed"
    assert "numeric" in job.error


# --- reload_calibration (daemon/app.py's startup counterpart) ---


def test_reload_calibration_pushes_the_persisted_map_into_the_scanner(conn):
    writes.write_setting(conn, TILT_CALIBRATION_SETTING_KEY, {"orange": 0.009}, "2026-01-01T00:00:00Z")
    scanner = TiltScanner(SimulatorClock(), hci_device=0)
    ctx = _Ctx(conn, scanner)

    calibration.reload_calibration(ctx)

    assert scanner.gravity_offset("orange") == pytest.approx(0.009)


def test_reload_calibration_is_a_no_op_with_nothing_persisted(conn):
    scanner = TiltScanner(SimulatorClock(), hci_device=0)
    ctx = _Ctx(conn, scanner)

    calibration.reload_calibration(ctx)  # must not raise

    assert scanner.gravity_offset("orange") == 0.0


def test_reload_calibration_is_a_no_op_when_tilt_is_not_the_platform(conn):
    class _NoTiltRegistry:
        def state_for(self, platform_id: str) -> Any | None:
            return None

    writes.write_setting(conn, TILT_CALIBRATION_SETTING_KEY, {"orange": 0.009}, "2026-01-01T00:00:00Z")
    ctx = _Ctx(conn, TiltScanner(SimulatorClock(), hci_device=0))
    ctx.registry = _NoTiltRegistry()

    calibration.reload_calibration(ctx)  # must not raise, nothing to push into
