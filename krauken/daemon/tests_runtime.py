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
import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

from krauken.contracts.errors import TestAlreadyRunning, UnknownTest, ValidationError
from krauken.db import queries

FIRE_OUTLET_DURATION_S = 10.0
# A genuine "hold it in your hand" interaction needs real time -- 3s was
# only ever enough for the old instant-stub result. One probe (nothing to
# compare against) still completes near-instantly; see _run_identify_probes.
IDENTIFY_PROBES_WINDOW_S = 90.0
IDENTIFY_PROBES_POLL_S = 1.0
IDENTIFY_PROBES_DELTA_F = 3.0


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


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
    elif action == "live_read":
        job = TestJob(job_id=job_id, kind=action, device_id=device_id, started_ts=now, ends_ts=now)
        job.state = "completed"
        job.result = {"message": "live_read has no countdown -- poll the device's own readings instead"}
    else:
        raise ValidationError(f"unknown test action {action!r}")

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
        import contextlib

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


def _read_probe_temps(ctx: Any, device_id: str) -> dict[str, float | None]:
    """Real per-probe temps for whichever device/platform is running this
    test -- the one place identify_probes' delta detection touches live
    driver state. Simulator/Manual are the only platforms with a live probe
    model today; an unknown device_id just reads as no probes at all."""
    if device_id == "simulator:chamber":
        engine = ctx.sim_engine
        chamber_f = engine.read_chamber().temp_f  # ticks the engine forward too
        temps = {"sim-probe-1": chamber_f}
        if engine.probe2_enabled:
            temps["sim-probe-2"] = engine.probe2_temp_f
        return temps
    if device_id == "manual:chamber":
        chamber = ctx.manual_panel.chamber
        temps = {"manual-probe-1": chamber.temp_f}
        if chamber.probe2_enabled:
            temps["manual-probe-2"] = chamber.probe2_temp_f
        return temps
    return {}


async def _run_identify_probes(ctx: Any, job: TestJob, device_id: str, probe_addresses: list[str], window_s: float) -> None:
    try:
        if len(probe_addresses) <= 1:
            # Nothing to compare against -- this is just "confirm it
            # responds", not a real identify. Still ticks on a short
            # cadence (rather than one sleep-then-read) and publishes each
            # reading to job.result while running, so the wizard's UI has a
            # live number to show -- proof the test is doing something even
            # with only one probe.
            baseline = _read_probe_temps(ctx, device_id)
            current = dict(baseline)
            job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current)}
            fast_window_s = min(window_s, 2.0)
            elapsed_s = 0.0
            while elapsed_s < fast_window_s:
                step_s = min(0.5, fast_window_s - elapsed_s)
                await ctx.clock.sleep(step_s)
                elapsed_s += step_s
                current = _read_probe_temps(ctx, device_id)
                job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current)}
            job.state = "completed"
            job.result = {
                "identified_address": probe_addresses[0] if probe_addresses else None,
                "baseline_f": baseline,
                "current_f": current,
            }
            return

        baseline = _read_probe_temps(ctx, device_id)
        current = dict(baseline)
        identified: str | None = None
        job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current)}
        elapsed_s = 0.0
        while elapsed_s < window_s:
            await ctx.clock.sleep(IDENTIFY_PROBES_POLL_S)
            elapsed_s += IDENTIFY_PROBES_POLL_S
            current = _read_probe_temps(ctx, device_id)
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
