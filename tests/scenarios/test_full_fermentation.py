"""End-to-end M2 scenario: real IPC -> real daemon -> real control loop ->
real SQLite, against the live Simulator driver, with a ManualClock
compressing the whole run into real test time. This is the "compressed
multi-week simulated fermentation runs clean" gate from the project plan,
scaled down to a couple of hours of simulated time so the test itself
stays fast -- the timing *mechanism* being exercised (ManualClock.sleep()
never really waiting) is exactly what a multi-week run would also use.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from krauken.daemon.testing import build_scenario_daemon
from krauken.db.connection import open_ro
from krauken.ipc.client import AsyncIPCClient
from krauken.platforms.manual.service import ManualService
from krauken.platforms.manual.service import build_service as build_manual_service
from krauken.platforms.simulator.service import SimulatorService
from krauken.platforms.simulator.service import build_service as build_simulator_service

FULL_PROFILE_STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        # A realistic profile: gravity must (a) be at/below gravity_hi, (b)
        # stay there continuously, (c) for a full 24h -- not a shortened
        # window. See GravityGate/stage_finished in contracts/stages.py.
        "end_mode": "gravity", "gravity_hi": 1.015, "gravity_stable_hours": 24.0,
        "advance_mode": "auto",
    },
    {
        "name": "Diacetyl rest", "temp_mode": "constant", "temp_f": 72.0,
        "end_mode": "time", "end_hours": 48.0, "advance_mode": "auto",
    },
    {
        "name": "Cold crash", "temp_mode": "constant", "temp_f": 34.0,
        "end_mode": "time", "end_hours": 96.0, "advance_mode": "auto",
    },
]

MAX_SIMULATED_DAYS = 30

TEST_STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 4.0, "advance_mode": "auto",
    },
    {
        "name": "Cold crash", "temp_mode": "constant", "temp_f": 55.0,
        "end_mode": "time", "end_hours": 2.0, "advance_mode": "auto",
    },
]


async def _start_platform_services(short_tmp: Path) -> tuple[SimulatorService, ManualService, Path, Path]:
    """Simulator's/Manual's own out-of-process servers, for
    build_scenario_daemon()'s daemon to reach over IPC -- Manual's is
    started too (build_daemon() always wants both clients) even though
    these scenarios never map it to anything."""
    simulator_socket = short_tmp / "sim.sock"
    manual_socket = short_tmp / "man.sock"
    simulator_service = build_simulator_service(socket_path=simulator_socket)
    manual_service = build_manual_service(socket_path=manual_socket)
    await simulator_service.start()
    await manual_service.start()
    return simulator_service, manual_service, simulator_socket, manual_socket


async def _scan_and_wait(client: AsyncIPCClient) -> None:
    result = await client.call("hardware.scan_start")
    scan_id = result["scan_id"]
    for _ in range(50):
        status = await client.call("hardware.scan_status", {"scan_id": scan_id})
        if status["state"] == "complete":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("scan never completed")


async def test_compressed_two_stage_fermentation_runs_to_completion(tmp_path: Path):
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))  # AF_UNIX path-length limit -- see tests/api/conftest.py
    socket_path = short_tmp / "d.sock"
    simulator_service, manual_service, simulator_socket, manual_socket = await _start_platform_services(short_tmp)

    daemon, clock = build_scenario_daemon(
        db_path=db_path, socket_path=socket_path, simulator_socket=simulator_socket, manual_socket=manual_socket,
        control_tick_interval_s=600.0,
    )
    await daemon.start()
    client = AsyncIPCClient(socket_path)
    try:
        await _scan_and_wait(client)
        mapping = await client.call(
            "hardware.mapping_save",
            {"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
        )
        assert mapping["valid"] is True

        started = await client.call(
            "fermentation.start", {"name": "Scenario batch", "stages": TEST_STAGES, "og": 1.050}
        )
        fermentation_id = started["fermentation_id"]

        # ManualClock.sleep() never really waits -- the control loop's own
        # background task races through the whole 6-simulated-hour profile
        # (600s ticks -> ~36 ticks) well inside real wall-clock time. A
        # short real-time budget with cooperative yields lets the event
        # loop actually execute those iterations; it's a safety timeout
        # against a genuine hang, not the mechanism doing the compression.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 10.0
        status = None
        while loop.time() < deadline:
            await asyncio.sleep(0.05)
            async with daemon.ctx.db_lock:
                row = daemon.ctx.conn.execute(
                    "SELECT status FROM fermentations WHERE id = ?", (fermentation_id,)
                ).fetchone()
            status = row["status"]
            if status != "active":
                break
        assert status == "completed", f"fermentation never completed in time (last status: {status})"
    finally:
        await daemon.stop()
        await simulator_service.stop()
        await manual_service.stop()
        shutil.rmtree(short_tmp, ignore_errors=True)

    conn = open_ro(db_path)
    try:
        fermentation = dict(conn.execute("SELECT * FROM fermentations WHERE id = ?", (fermentation_id,)).fetchone())
        assert fermentation["status"] == "completed"
        assert fermentation["ended_at"] is not None

        stages = [dict(r) for r in conn.execute(
            "SELECT * FROM fermentation_stages WHERE fermentation_id = ? ORDER BY seq", (fermentation_id,)
        ).fetchall()]
        assert len(stages) == 2
        for stage in stages:
            assert stage["state"] == "finished"
            assert stage["end_actual_reason"] == "time"
            assert stage["started_at"] is not None
            assert stage["ended_at"] is not None
            assert stage["criteria_met_at"] is not None

        samples = [dict(r) for r in conn.execute(
            "SELECT * FROM samples WHERE fermentation_id = ? ORDER BY ts", (fermentation_id,)
        ).fetchall()]
        assert len(samples) > 0
        assert samples[0]["write_reason"] == "boot"
        # The happy path (healthy drivers throughout) must never fall into
        # the beer-temp-lost failsafe branch.
        assert all(s["target_source"] == "profile" for s in samples)
        assert all(s["beer_temp_ok"] == 1 and s["chamber_temp_ok"] == 1 for s in samples)

        event_types = {r["type"] for r in conn.execute(
            "SELECT type FROM events WHERE fermentation_id = ?", (fermentation_id,)
        ).fetchall()}
        assert {"fermentation_started", "stage_advanced", "fermentation_completed"} <= event_types
    finally:
        conn.close()


async def test_control_tick_writes_rich_live_state_telemetry(tmp_path: Path):
    """A dedicated, non-racing check for live_state's content while a
    fermentation is active: calling control_tick() directly, once, side-
    steps the scenario test's own background loop (which -- being driven by
    a ManualClock that never really waits -- can race straight through an
    entire short profile within a single event-loop turn, making "catch it
    mid-run by polling" unreliable to assert against)."""
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))
    socket_path = short_tmp / "d.sock"
    simulator_service, manual_service, simulator_socket, manual_socket = await _start_platform_services(short_tmp)

    daemon, clock = build_scenario_daemon(
        db_path=db_path, socket_path=socket_path, simulator_socket=simulator_socket, manual_socket=manual_socket,
        control_tick_interval_s=600.0,
    )
    await daemon.start()
    client = AsyncIPCClient(socket_path)
    try:
        await _scan_and_wait(client)
        await client.call(
            "hardware.mapping_save",
            {"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
        )
        await client.call("fermentation.start", {"name": "Scenario batch", "stages": TEST_STAGES, "og": 1.050})

        from krauken.daemon.control_loop import control_tick

        async with daemon.server.state_lock:
            await control_tick(daemon.ctx)

        async with daemon.ctx.db_lock:
            row = dict(daemon.ctx.conn.execute("SELECT payload FROM live_state WHERE id = 1").fetchone())
        payload = json.loads(row["payload"])
        assert payload["fermentation_id"] is not None
        assert payload["stage_name"] == "Primary"
        assert payload["target_source"] == "profile"
        assert payload["beer_temp_ok"] is True
    finally:
        await daemon.stop()
        await simulator_service.stop()
        await manual_service.stop()
        shutil.rmtree(short_tmp, ignore_errors=True)


async def test_full_fermentation_profile_completes_with_correct_decisions(tmp_path: Path):
    """The actual "did the daemon make the right calls" gate: a real
    multi-stage profile -- gravity-gated Primary, then two time-gated
    stages spanning both a heat demand (72F, warmer than Primary's 68F
    hold) and a strong cool demand (34F cold crash) -- raced to completion
    by SimulatorClock as fast as the simulator can run, then asserted
    against for correct decisions, not just "it finished." The
    30-simulated-day cap is checked against the clock's own elapsed time
    and is independent of whatever end_hours/max_hours the profile itself
    sets -- it exists specifically to catch a logic bug (e.g. gravity never
    stabilizing) hanging the test, not to double as a normal completion
    path.

    control_tick_interval_s=300 (5 simulated minutes/tick), not something
    coarser: the sampler's gap-detection (db/queries.py's
    fermentation_series, gap_threshold_s = 2.5 * heartbeat_s = 3000s)
    assumes a gap always means the daemon stopped. A coarser interval
    (e.g. 3600s) exceeds that threshold on literally every single tick,
    so the chart would render its ENTIRE line as gap-bridge dots even
    though the daemon never stopped -- SimulatorClock's near-zero per-tick
    cost means there's no real performance reason to keep ticks coarse
    anymore, so keep them well under the gap threshold instead.
    """
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))
    socket_path = short_tmp / "d.sock"
    simulator_service, manual_service, simulator_socket, manual_socket = await _start_platform_services(short_tmp)

    daemon, clock = build_scenario_daemon(
        db_path=db_path, socket_path=socket_path, simulator_socket=simulator_socket, manual_socket=manual_socket,
        control_tick_interval_s=300.0,
    )
    await daemon.start()
    client = AsyncIPCClient(socket_path)
    try:
        await _scan_and_wait(client)
        mapping = await client.call(
            "hardware.mapping_save",
            {"roles": {
                "chamber_temp": "simulator:chamber",
                "beer_temp": "simulator:tilt",
                "beer_gravity": "simulator:tilt",
            }},
        )
        assert mapping["valid"] is True

        started = await client.call(
            "fermentation.start",
            {"name": "Full profile", "stages": FULL_PROFILE_STAGES, "og": 1.050},
        )
        fermentation_id = started["fermentation_id"]
        start_now = clock.now()

        status = None
        while True:
            await asyncio.sleep(0.01)  # cooperative yield -- SimulatorClock does the actual compression
            async with daemon.ctx.db_lock:
                row = daemon.ctx.conn.execute(
                    "SELECT status FROM fermentations WHERE id = ?", (fermentation_id,)
                ).fetchone()
            status = row["status"]
            if status != "active":
                break
            elapsed_days = (clock.now() - start_now) / 86400.0
            if elapsed_days > MAX_SIMULATED_DAYS:
                raise AssertionError(
                    f"fermentation did not complete within {MAX_SIMULATED_DAYS} simulated days "
                    f"(last status: {status}) -- likely a stuck stage-advance or gravity-gate bug"
                )
        assert status == "completed"
    finally:
        await daemon.stop()
        await simulator_service.stop()
        await manual_service.stop()
        shutil.rmtree(short_tmp, ignore_errors=True)

    conn = open_ro(db_path)
    try:
        fermentation = dict(conn.execute("SELECT * FROM fermentations WHERE id = ?", (fermentation_id,)).fetchone())
        assert fermentation["status"] == "completed"
        assert fermentation["ended_at"] is not None

        stages = [dict(r) for r in conn.execute(
            "SELECT * FROM fermentation_stages WHERE fermentation_id = ? ORDER BY seq", (fermentation_id,)
        ).fetchall()]
        assert len(stages) == 3
        assert stages[0]["end_actual_reason"] == "gravity"
        assert stages[1]["end_actual_reason"] == "time"
        assert stages[2]["end_actual_reason"] == "time"
        for stage in stages:
            assert stage["state"] == "finished"
            assert stage["started_at"] is not None and stage["ended_at"] is not None

        samples = [dict(r) for r in conn.execute(
            "SELECT * FROM samples WHERE fermentation_id = ? ORDER BY ts", (fermentation_id,)
        ).fetchall()]
        assert len(samples) > 0
        # The cascade must have genuinely engaged BOTH directions across the
        # profile. If this ever comes back all-idle or all-cool, the
        # cascade/relay wiring broke -- not just "the test got lucky."
        modes_seen = {s["chamber_mode"] for s in samples}
        assert "cool" in modes_seen
        assert "heat" in modes_seen

        # Gravity should have genuinely dropped toward the new terminal
        # value, not sat at OG or drifted toward the old 1.011 floor.
        final_gravity = samples[-1]["gravity"]
        assert final_gravity is not None and final_gravity < 1.02

        event_types = {r["type"] for r in conn.execute(
            "SELECT type FROM events WHERE fermentation_id = ?", (fermentation_id,)
        ).fetchall()}
        assert {"fermentation_started", "stage_advanced", "fermentation_completed"} <= event_types
    finally:
        conn.close()


async def test_full_fermentation_auto_detects_og_when_not_supplied(tmp_path: Path):
    """OG auto-detection (contracts/og_detection.py): a fermentation
    started without an explicit og should end up with one locked in on
    its own, close to the simulator's true GravityParams.og (1.052) --
    without needing to run the whole profile to completion, since
    detection settles well within the first simulated hour (see plant.py's
    settling_duration_h=0.25h and og_detection.py's
    OG_STABLE_WINDOW_H=0.5h). The three OTHER tests in this file all pass
    an explicit og, so they keep exercising the "explicit og bypasses
    detection entirely" path unchanged -- this is the one test for the
    detection path itself."""
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))
    socket_path = short_tmp / "d.sock"
    simulator_service, manual_service, simulator_socket, manual_socket = await _start_platform_services(short_tmp)

    daemon, clock = build_scenario_daemon(
        db_path=db_path, socket_path=socket_path, simulator_socket=simulator_socket, manual_socket=manual_socket,
        control_tick_interval_s=300.0,
    )
    await daemon.start()
    client = AsyncIPCClient(socket_path)
    og = None
    try:
        await _scan_and_wait(client)
        mapping = await client.call(
            "hardware.mapping_save",
            {"roles": {
                "chamber_temp": "simulator:chamber",
                "beer_temp": "simulator:tilt",
                "beer_gravity": "simulator:tilt",
            }},
        )
        assert mapping["valid"] is True

        started = await client.call(
            "fermentation.start",
            {"name": "Auto-OG batch", "stages": FULL_PROFILE_STAGES, "og": None},
        )
        fermentation_id = started["fermentation_id"]
        start_now = clock.now()

        while True:
            await asyncio.sleep(0.01)  # cooperative yield -- SimulatorClock does the actual compression
            async with daemon.ctx.db_lock:
                row = daemon.ctx.conn.execute(
                    "SELECT og FROM fermentations WHERE id = ?", (fermentation_id,)
                ).fetchone()
            og = row["og"]
            if og is not None:
                break
            elapsed_h = (clock.now() - start_now) / 3600.0
            # Well past OG_DETECTION_MAX_H (6h)'s own worst-case ceiling --
            # this timeout exists to catch a genuine hang, not to double as
            # the normal completion path.
            if elapsed_h > 12.0:
                raise AssertionError("OG never locked in within 12 simulated hours")
    finally:
        await daemon.stop()
        await simulator_service.stop()
        await manual_service.stop()
        shutil.rmtree(short_tmp, ignore_errors=True)

    assert og is not None
    assert abs(og - 1.052) < 0.01  # true simulator OG, allowing for settling/jitter noise

    conn = open_ro(db_path)
    try:
        event_types = {r["type"] for r in conn.execute(
            "SELECT type FROM events WHERE fermentation_id = ?", (fermentation_id,)
        ).fetchall()}
        assert "og_locked" in event_types
    finally:
        conn.close()
