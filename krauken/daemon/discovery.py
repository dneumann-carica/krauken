"""Hardware discovery: one action, fans out across every registered
platform (real or mock) concurrently. No platform can fail the whole scan --
a broken/unavailable driver contributes an empty candidate list plus a
status, never an exception that takes down the others. Job-shaped (start
now, poll status) so a slow platform (a real BLE scan, later) can never
block the IPC server or a caller's HTTP request.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from krauken.contracts.errors import PlatformUnavailable
from krauken.contracts.interfaces import PlatformDriver
from krauken.contracts.models import DeviceCandidate
from krauken.daemon.timefmt import iso as _iso
from krauken.db import writes

log = logging.getLogger("krauken.daemon.discovery")

DEFAULT_SCAN_BUDGET_S = 10.0


@dataclass
class ScanJob:
    job_id: str
    state: str = "running"  # running | complete | failed
    started_ts: float = 0.0
    platform_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None
    # Tracks the actual asyncio.Task running _run_scan, the same way
    # tests_runtime.py's TestJob tracks its own -- so a shutdown in
    # progress (app.py's Daemon.stop()) can find, cancel, and await this
    # job before the db connection it writes through gets closed out from
    # under it. Not otherwise read by scan_status()/callers.
    _task: asyncio.Task | None = field(default=None, repr=False)


def _candidate_to_row(c: DeviceCandidate, as_of: str) -> dict[str, Any]:
    """The one place DeviceCandidate (the platform-driver contract) gets
    projected onto the `devices` table's row shape. If a future real
    platform's candidate fields drift from this shape, this is the only
    function that needs to change.

    capabilities' elements are Role enum members from an in-process
    platform driver, but plain strings once a candidate has crossed IPC
    (platforms/ipc_driver.py's _candidate_from_wire -- JSON has no enum
    type, so a Role only ever survives the wire as its own .value) --
    str(r) handles both identically, since Role is a StrEnum whose str()
    already IS its value."""
    return {
        "device_id": c.device_id,
        "platform": c.platform,
        "name": c.display_name,
        "kind": c.kind_label,
        "capabilities": [str(r) for r in c.capabilities],
        "is_bundle": 1 if c.bundled_roles else 0,
        "health": c.health.value,
        "first_seen_at": as_of,
        "last_seen_at": as_of,
        "metadata": {
            **c.identity,
            "simulated": c.simulated,
            "detail_line": c.detail_line,
            "reading_summary": c.reading_summary,
            "available_tests": list(c.available_tests),
        },
        "last_reading": dict(c.readings),
    }


class DiscoveryService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def start_scan(self) -> dict[str, Any]:
        job = ScanJob(job_id=uuid.uuid4().hex[:12], started_ts=self.ctx.clock.now())
        self.ctx.jobs[job.job_id] = job
        job._task = asyncio.create_task(self._run_scan(job))
        return {"scan_id": job.job_id, "state": job.state}

    def scan_status(self, scan_id: str) -> dict[str, Any]:
        job = self.ctx.jobs.get(scan_id)
        if job is None or not isinstance(job, ScanJob):
            return {"scan_id": scan_id, "state": "unknown", "platform_status": {}}
        return {"scan_id": job.job_id, "state": job.state, "platform_status": job.platform_status, "error": job.error}

    async def _discover_one(self, platform: PlatformDriver) -> tuple[str, list[DeviceCandidate], dict[str, Any]]:
        try:
            candidates = list(await platform.discover({}))
            return platform.platform_id, candidates, {"state": "ok", "candidate_count": len(candidates)}
        except PlatformUnavailable as e:
            return platform.platform_id, [], {"state": "unavailable", "message": str(e)}
        except Exception as e:  # noqa: BLE001 -- one platform's bug must never sink the whole scan
            log.exception("discover() failed for platform %s", platform.platform_id)
            return platform.platform_id, [], {"state": "error", "message": str(e)}

    async def _run_scan(self, job: ScanJob) -> None:
        try:
            results = await asyncio.gather(*(self._discover_one(p) for p in self.ctx.registry))
            as_of = _iso(self.ctx.clock.now())
            async with self.ctx.db_lock:
                for platform_id, candidates, status in results:
                    job.platform_status[platform_id] = status
                    for c in candidates:
                        writes.upsert_device(self.ctx.conn, _candidate_to_row(c, as_of))
            job.state = "complete"
        except Exception as e:  # noqa: BLE001 -- last-resort boundary for the background task
            log.exception("scan %s failed", job.job_id)
            job.state = "failed"
            job.error = str(e)
