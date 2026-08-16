"""Outlet fire-tests and probe identify-tests: job-shaped (start now, poll
status, explicit cancel) exactly like discovery scans, and for the same
reason -- a 10-second countdown must never block the IPC server or a
caller's HTTP request, and the countdown must be server-owned so a relay
can't stay energized because a browser tab got closed mid-test.

M1 only has Manual/Simulator platforms, which have no real relay or probe
to actuate/read -- these tests still run a real server-owned timer and
report a plausible synthetic outcome, so the guided wizard's full
countdown/poll/cancel lifecycle is genuinely exercised even with zero real
hardware. When the real Krauken platform lands, this module's job/timer
mechanics stay the same; only the "what actually happens at the hardware
layer" piece changes.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from krauken.contracts.errors import FermentationAlreadyActive, TestAlreadyRunning, UnknownTest, ValidationError
from krauken.contracts.models import ChamberMode
from krauken.daemon import drivers
from krauken.daemon.timefmt import iso as _iso
from krauken.db import queries
from krauken.platforms.registry import PLATFORM_BINDINGS

FIRE_OUTLET_DURATION_S = 10.0
# A genuine "hold it in your hand" interaction needs real time -- 3s was
# only ever enough for the old instant-stub result. One probe (nothing to
# compare against) still completes near-instantly; see _run_identify_probes.
IDENTIFY_PROBES_WINDOW_S = 90.0
IDENTIFY_PROBES_POLL_S = 1.0
IDENTIFY_PROBES_DELTA_F = 3.0

# BrewPi (and any future firmware-managed chamber controller) never lets us
# fire a relay directly -- the firmware's own fridge-constant mode decides
# that. Instead we push a setpoint far enough past the current reading to
# force the firmware into HEATING, then watch its own reported state --
# this is a REAL functional test (the relay either engages or it doesn't),
# not a simulated countdown. +15F is comfortably past any realistic
# ambient/ferment-temp gap without being an unsafe target for the few
# minutes this runs.
CONFIRM_HEATER_FORCE_DELTA_F = 15.0
CONFIRM_HEATER_POLL_S = 2.0
# Real BrewPi firmware enforces anti-short-cycle protection -- a minimum
# time between switching relays, and a separate minimum wait since boot --
# commonly several minutes (confirmed via BrewPi's own community docs, not
# guessed; this project's own contracts/control_constants.py models the
# same shape for the Simulator -- MIN_OFF_S=5min, OPPOSITE_LOCKOUT_S=30min).
# A from-cold-start heat demand only ever needs to clear MIN_OFF_S (no
# prior relay direction to lock out against), so this window sits well
# past that with real margin, not right on the boundary -- a short timeout
# would misreport "no heater" whenever this rig happens to still be inside
# that window. The wizard's own UI carries an explicit "no heater detected
# / skip anyway" escape hatch for the rarer case (this test re-run soon
# after a real relay just ran the opposite direction) where even this
# isn't enough.
CONFIRM_HEATER_WINDOW_S = 600.0


@dataclass
class TestJob:
    job_id: str
    kind: str  # "fire_outlet" | "identify_probes" | "live_read"
    device_id: str
    state: str = "running"  # running | completed | cancelled | failed
    started_ts: float = 0.0
    ends_ts: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.job_id,
            "kind": self.kind,
            "device_id": self.device_id,
            "state": self.state,
            "started_at": _iso(self.started_ts),
            "ends_at": _iso(self.ends_ts) if self.ends_ts is not None else None,
            "result": self.result,
            "error": self.error,
        }


def _device_running_test(ctx: Any, device_id: str) -> TestJob | None:
    for job in ctx.jobs.values():
        if isinstance(job, TestJob) and job.device_id == device_id and job.state == "running":
            return job
    return None


def start_test(ctx: Any, device_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    if _device_running_test(ctx, device_id) is not None:
        raise TestAlreadyRunning(f"a test is already running for {device_id}")

    job_id = uuid.uuid4().hex[:12]
    now = ctx.clock.now()

    if action == "fire_outlet":
        duration_s = float(params.get("duration_s", FIRE_OUTLET_DURATION_S))
        job = TestJob(job_id=job_id, kind=action, device_id=device_id, started_ts=now, ends_ts=now + duration_s)
        job._task = asyncio.create_task(_run_fire_outlet(ctx, job, duration_s, params.get("outlet")))
    elif action == "identify_probes":
        # The device's own last-scanned metadata is the source of truth for
        # which probes it has -- not a caller-supplied param, so the wizard
        # never has to already know something discover() exists to tell it.
        device = queries.device_by_id(ctx.conn, device_id)
        probe_addresses = list((device or {}).get("metadata", {}).get("probe_addresses") or [])
        window_s = float(params.get("window_s", IDENTIFY_PROBES_WINDOW_S))
        job = TestJob(job_id=job_id, kind=action, device_id=device_id, started_ts=now, ends_ts=now + window_s)
        job._task = asyncio.create_task(_run_identify_probes(ctx, job, device_id, probe_addresses, window_s))
    elif action == "confirm_heater":
        # Forces a real setpoint on the mapped chamber driver -- blocked
        # while a fermentation is active for the same reason
        # hardware.py's stop_chamber() is: exactly one active batch is a
        # DB-level invariant, and this driver is the one it's still using.
        if queries.active_fermentation(ctx.conn) is not None:
            raise FermentationAlreadyActive("a fermentation is active -- can't run a live heat test on the chamber right now")
        window_s = float(params.get("window_s", CONFIRM_HEATER_WINDOW_S))
        job = TestJob(job_id=job_id, kind=action, device_id=device_id, started_ts=now, ends_ts=now + window_s)
        job._task = asyncio.create_task(_run_confirm_heater(ctx, job, device_id, window_s))
    elif action == "live_read":
        job = TestJob(job_id=job_id, kind=action, device_id=device_id, started_ts=now, ends_ts=now)
        job.state = "completed"
        job.result = {"message": "live_read has no countdown -- poll the device's own readings instead"}
    else:
        # Platform-owned actions that don't fit the ChamberDriver/
        # BeerTempSource/GravitySource Protocols above -- hardware
        # CONFIGURATION, not hardware OPERATION (installing a device,
        # reading raw firmware state, pushing OneWire addresses). No
        # per-platform knowledge lives here; PLATFORM_BINDINGS is the one
        # place that maps a platform_id to its own test_runners (see
        # platforms/registry.py, platforms/brewpi/device_config.py).
        platform = device_id.split(":", 1)[0]
        binding = PLATFORM_BINDINGS.get(platform)
        entry = binding.test_runners.get(action) if binding else None
        if entry is None:
            raise ValidationError(f"unknown test action {action!r}")
        # Checked HERE, synchronously, before the task is ever created --
        # same reason confirm_heater's own check above is inline rather
        # than inside its task body: an async check buried inside the
        # runner would only ever surface as an unhandled exception in a
        # Task, never as something this function's caller can catch.
        if entry.requires_no_active_fermentation and queries.active_fermentation(ctx.conn) is not None:
            raise FermentationAlreadyActive(f"a fermentation is active -- can't run {action!r} right now")
        window_s = float(params.get("window_s", 600.0))
        job = TestJob(job_id=job_id, kind=action, device_id=device_id, started_ts=now, ends_ts=now + window_s)
        job._task = asyncio.create_task(entry.run(ctx, job, device_id, params))

    ctx.jobs[job_id] = job
    return job.to_dict()


def test_status(ctx: Any, job_id: str) -> dict[str, Any]:
    job = ctx.jobs.get(job_id)
    if job is None or not isinstance(job, TestJob):
        raise UnknownTest(f"no test job {job_id}")
    return job.to_dict()


async def cancel_test(ctx: Any, job_id: str) -> dict[str, Any]:
    job = ctx.jobs.get(job_id)
    if job is None or not isinstance(job, TestJob):
        raise UnknownTest(f"no test job {job_id}")
    if job.state == "running" and job._task is not None:
        job._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job._task
        job.state = "cancelled"
    return job.to_dict()


async def _run_fire_outlet(ctx: Any, job: TestJob, duration_s: float, outlet: Any) -> None:
    try:
        await ctx.clock.sleep(duration_s)
        job.state = "completed"
        job.result = {"outlet": outlet, "message": "outlet fired for the full duration"}
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise


async def _run_confirm_heater(ctx: Any, job: TestJob, device_id: str, window_s: float) -> None:
    """Forces the mapped chamber driver's setpoint to CONFIRM_HEATER_FORCE_DELTA_F
    above the current reading, then polls read_chamber()'s own reported
    ChamberMode -- confirmed the moment the firmware itself reports HEAT,
    same as identify_probes trusts the driver's own readings rather than
    the caller's say-so. Dispatches through drivers.chamber_driver()
    exactly like the control loop does, so this has no hardcoded knowledge
    of which platform is running it (BrewPi today; any future
    firmware-managed bundle device tomorrow)."""
    platform = device_id.split(":", 1)[0]
    driver = drivers.chamber_driver(ctx, platform, device_id)
    if driver is None:
        job.state = "failed"
        job.error = "no chamber driver mapped for this device"
        return

    baseline = await driver.read_chamber()
    if baseline.temp_f is None:
        job.state = "failed"
        job.error = "chamber probe isn't reading -- can't run a live heat test"
        return

    forced_target = baseline.temp_f + CONFIRM_HEATER_FORCE_DELTA_F
    await driver.set_target(forced_target)
    try:
        confirmed = False
        current_f = baseline.temp_f
        job.result = {"confirmed": confirmed, "baseline_f": baseline.temp_f, "forced_target_f": forced_target, "current_f": current_f}
        elapsed_s = 0.0
        while elapsed_s < window_s:
            await ctx.clock.sleep(CONFIRM_HEATER_POLL_S)
            elapsed_s += CONFIRM_HEATER_POLL_S
            reading = await driver.read_chamber()
            current_f = reading.temp_f
            confirmed = reading.mode == ChamberMode.HEAT
            job.result = {"confirmed": confirmed, "baseline_f": baseline.temp_f, "forced_target_f": forced_target, "current_f": current_f}
            if confirmed:
                break
        job.state = "completed"
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
    finally:
        # Always release back to idle -- confirmed, timed out, or
        # cancelled -- never leave the forced setpoint behind. Matches
        # hardware.py's stop_chamber() convention: an explicit
        # set_target(None) release is the only contract a chamber driver
        # makes; nothing auto-restores "whatever it was before".
        with contextlib.suppress(Exception):
            await driver.set_target(None)


async def _read_probe_temps(ctx: Any, device_id: str) -> dict[str, float | None]:
    """Real per-probe temps for whichever device/platform is running this
    test -- the one place identify_probes' delta detection touches live
    driver state. Dispatches through daemon/drivers.py's chamber_driver()
    exactly like the control loop does, so this has no hardcoded knowledge
    of which platforms exist or what their probe-address strings look like
    (ChamberDriver.probe_temps()) -- an unmapped/unknown platform just reads
    as no probes at all."""
    platform = device_id.split(":", 1)[0]
    driver = drivers.chamber_driver(ctx, platform, device_id)
    if driver is None:
        return {}
    return await driver.probe_temps()


async def _run_identify_probes(ctx: Any, job: TestJob, device_id: str, probe_addresses: list[str], window_s: float) -> None:
    try:
        if len(probe_addresses) <= 1:
            # Nothing to compare against -- this is just "confirm it
            # responds", not a real identify. Still ticks on a short
            # cadence (rather than one sleep-then-read) and publishes each
            # reading to job.result while running, so the wizard's UI has a
            # live number to show -- proof the test is doing something even
            # with only one probe.
            baseline = await _read_probe_temps(ctx, device_id)
            current = dict(baseline)
            job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current)}
            fast_window_s = min(window_s, 2.0)
            elapsed_s = 0.0
            while elapsed_s < fast_window_s:
                step_s = min(0.5, fast_window_s - elapsed_s)
                await ctx.clock.sleep(step_s)
                elapsed_s += step_s
                current = await _read_probe_temps(ctx, device_id)
                job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current)}
            job.state = "completed"
            job.result = {
                "identified_address": probe_addresses[0] if probe_addresses else None,
                "baseline_f": baseline,
                "current_f": current,
            }
            return

        baseline = await _read_probe_temps(ctx, device_id)
        current = dict(baseline)
        identified: str | None = None
        job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current)}
        elapsed_s = 0.0
        while elapsed_s < window_s:
            await ctx.clock.sleep(IDENTIFY_PROBES_POLL_S)
            elapsed_s += IDENTIFY_PROBES_POLL_S
            current = await _read_probe_temps(ctx, device_id)
            for addr in probe_addresses:
                b, c = baseline.get(addr), current.get(addr)
                if b is not None and c is not None and (c - b) >= IDENTIFY_PROBES_DELTA_F:
                    identified = addr
                    break
            job.result = {"identified_address": identified, "baseline_f": dict(baseline), "current_f": dict(current)}
            if identified is not None:
                break

        job.state = "completed"
        job.result = {"identified_address": identified, "baseline_f": baseline, "current_f": current}
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
