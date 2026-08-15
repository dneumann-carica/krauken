"""One bad control tick must never permanently stop control of a real,
physical brewery -- see Daemon._control_loop's own docstring for how
silently this used to fail (an uncaught exception died the loop, and
because the Daemon object keeps its own reference to the task, it's never
garbage collected either, so asyncio's own "exception was never retrieved"
logging -- which only fires from Task.__del__ -- never ran). This test
pins the fix at the daemon-composition level, not just contracts/stages.py
or contracts/cascade.py's own pure-function correctness.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

import krauken.daemon.app as app_module
from krauken.daemon.testing import build_scenario_daemon


async def test_control_loop_survives_an_exception_and_keeps_ticking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))  # AF_UNIX path-length limit -- see tests/api/conftest.py
    socket_path = short_tmp / "d.sock"
    # control_tick is fully monkeypatched below -- nothing here ever
    # dispatches to Simulator/Manual, so there's no need to point their
    # HALs at a real process at all (IpcPlatformConnection.start() is
    # non-blocking regardless of whether anything's listening -- see its
    # own docstring); the default (unreachable in this test environment)
    # socket is exactly as harmless as a specific throwaway one would be.

    call_count = 0

    async def flaky_control_tick(ctx):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated tick failure")

    monkeypatch.setattr(app_module, "control_tick", flaky_control_tick)

    daemon, _clock = build_scenario_daemon(
        db_path=db_path, socket_path=socket_path,
        control_tick_interval_s=0.0,
    )
    await daemon.start()
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while call_count < 3 and loop.time() < deadline:
            await asyncio.sleep(0.01)

        assert call_count >= 3, "control loop stopped ticking after the first exception"
        # The background task itself must still be alive, not silently
        # dead from the propagated exception.
        assert daemon._control_task is not None
        assert not daemon._control_task.done()
    finally:
        await daemon.stop()
        shutil.rmtree(short_tmp, ignore_errors=True)
