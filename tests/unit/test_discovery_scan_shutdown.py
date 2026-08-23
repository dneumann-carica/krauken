"""discovery.py's ScanJob now tracks its own asyncio.Task (the same shape
tests_runtime.py's TestJob already used) so app.py's Daemon.stop() can
cancel and await it before closing the db connection it writes through.

This is the exact race that used to crash db/seed.py's demo-batch
generation on real Raspberry Pi hardware: a caller (there, seed.py's
_scan_and_wait) gives up on a scan that is still genuinely running, tears
the daemon down, and the still-running scan task then reaches
writes.upsert_device() against an already-closed sqlite3.Connection --
"Cannot operate on a closed database", on top of whatever the real failure
was. Proven here at the daemon level, independent of seed.py's own
timeout, since ANY caller giving up early (a slower Pi still, a busier
system) can trigger the identical race unless Daemon.stop() itself refuses
to close the connection out from under an outstanding job.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from krauken.contracts.clock import SimulatorClock
from krauken.daemon.app import build_daemon
from krauken.daemon.discovery import DiscoveryService, ScanJob


class _NeverFinishesPlatform:
    """A fake PlatformDriver whose discover() never returns on its own --
    stands in for a real platform that's simply slower than whatever
    budget a caller gave the scan (the actual real-Pi failure mode), so the
    scan job is still genuinely "running" at the moment daemon.stop() is
    called."""

    platform_id = "slow"

    def __init__(self) -> None:
        self.discover_started = asyncio.Event()
        self.cancelled = False

    async def discover(self, args: dict[str, Any]) -> list[Any]:
        self.discover_started.set()
        try:
            await asyncio.sleep(3600.0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []  # pragma: no cover -- never reached


class _FakeRegistry:
    """Just enough of PlatformRegistry's shape (iterable of PlatformDriver,
    start_all()/stop_all()) for Daemon.start()/stop() to run against it --
    same substitution pattern test_platform_registry.py's own _Ctx classes
    use for ctx.registry, just swapped in post-construction here since
    build_daemon() doesn't take a registry override."""

    def __init__(self, platforms: list[Any]) -> None:
        self._platforms = platforms

    def __iter__(self):
        return iter(self._platforms)

    async def start_all(self) -> None:
        pass

    async def stop_all(self) -> None:
        pass


async def test_daemon_stop_cancels_an_outstanding_scan_before_closing_the_db(tmp_path: Path):
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))  # AF_UNIX path-length limit -- see tests/api/conftest.py
    socket_path = short_tmp / "d.sock"
    try:
        daemon = build_daemon(
            db_path=db_path, clock=SimulatorClock(), socket_path=socket_path,
            # Both loops paced far apart -- this test only cares about the
            # scan job, not a heartbeat/control tick racing in mid-test.
            heartbeat_interval_s=3600.0, control_tick_interval_s=3600.0,
        )
        slow_platform = _NeverFinishesPlatform()
        daemon.ctx.registry = _FakeRegistry([slow_platform])
        await daemon.start()

        result = DiscoveryService(daemon.ctx).start_scan()
        scan_id = result["scan_id"]
        job = daemon.ctx.jobs[scan_id]
        assert isinstance(job, ScanJob)
        assert job._task is not None  # the fix: start_scan() now keeps a handle to it

        # Let the scan's own background task actually start before we race
        # shutdown against it -- otherwise this would trivially pass by
        # accident (nothing in flight yet).
        await asyncio.wait_for(slow_platform.discover_started.wait(), timeout=5.0)
        assert job.state == "running"

        # The fix under test: this must neither raise (the old bug: the
        # orphaned task hit the closed connection and logged a second,
        # confusing traceback) nor hang.
        await asyncio.wait_for(daemon.stop(), timeout=5.0)

        assert slow_platform.cancelled is True
        # Cancelled mid-flight, never ran to completion -- state never
        # flips to "complete" (which would mean it raced past the cancel
        # and reached the db write below).
        assert job.state == "running"
    finally:
        shutil.rmtree(short_tmp, ignore_errors=True)
