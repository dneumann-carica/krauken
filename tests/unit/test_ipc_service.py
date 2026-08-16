"""Round-trip coverage for platforms/ipc_service.py (server) +
platforms/ipc_driver.py (client) together -- the real SimChamberDriver/
SimBeerTempSource/SimGravitySource/SimulatorPlatform running behind a real
IPCServer, read back through a real PersistentIPCClient, asserting the
IPC-wrapped behavior matches calling the underlying driver directly. This
is the exact shape platforms/simulator/service.py and
platforms/manual/service.py wrap into a real process."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from krauken.contracts.clock import RemoteClock, SimulatorClock
from krauken.contracts.control_constants import ControlTuning
from krauken.contracts.errors import PlatformUnavailable
from krauken.contracts.models import ChamberMode, Health
from krauken.ipc.persistent_client import PersistentIPCClient
from krauken.ipc.server import IPCServer
from krauken.platforms.ipc_driver import (
    IpcBeerTempSource,
    IpcChamberDriver,
    IpcGravitySource,
    IpcPlatformDriver,
    sync_clock,
)
from krauken.platforms.ipc_service import ServiceContext
from krauken.platforms.simulator.live import SimBeerTempSource, SimChamberDriver, SimGravitySource, SimPlantEngine
from krauken.platforms.simulator.platform import SimulatorPlatform


class _IpcSimulatorPlatform(IpcPlatformDriver):
    platform_id = "simulator"
    display_name = "Simulator"


@pytest_asyncio.fixture
async def rig():
    """A real SimPlantEngine wrapped behind a real IPCServer, plus a
    connected PersistentIPCClient and the four Ipc* classes wrapping it --
    everything a test needs to compare "called directly" vs "called over
    IPC" behavior."""
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    # Short timers, matching test_sim_live.py's own convention -- the
    # production defaults are minutes long, which a plain clock.advance(60)
    # would never satisfy.
    engine = SimPlantEngine(SimulatorClock(), tuning=ControlTuning(min_on_s=10, min_off_s=10, opposite_lockout_s=20))
    ctx = ServiceContext(
        platform=SimulatorPlatform(engine),
        chamber=SimChamberDriver(engine),
        beer_temp=SimBeerTempSource(engine),
        gravity=SimGravitySource(engine),
        clock=engine.clock,
    )
    server = IPCServer(socket_dir / "sim.sock", ctx=ctx)
    await server.start()

    client = PersistentIPCClient(server.socket_path, heartbeat_interval_s=100)
    await client.start()
    await asyncio.sleep(0.05)

    yield engine, client

    await client.stop()
    await server.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_discover_round_trips(rig):
    _engine, client = rig
    driver = _IpcSimulatorPlatform(client)
    candidates = await driver.discover({})
    ids = {c.device_id for c in candidates}
    assert ids == {"simulator:chamber", "simulator:tilt"}


async def test_chamber_read_and_set_target_round_trip(rig):
    engine, client = rig
    driver = IpcChamberDriver(client)

    await driver.set_target(55.0)
    engine.clock.advance(60)
    reading = await driver.read_chamber()
    assert reading.mode == ChamberMode.COOL
    assert reading.commanded_target_f == 55.0
    assert await driver.commanded_target() == 55.0


async def test_beer_and_gravity_read_round_trip(rig):
    _engine, client = rig
    beer = await IpcBeerTempSource(client).read()
    assert beer.health == Health.OK
    assert beer.temp_f is not None

    gravity = await IpcGravitySource(client).read()
    assert gravity.health == Health.OK
    assert gravity.gravity_sg is not None


async def test_probe_temps_round_trip(rig):
    engine, client = rig
    engine.set_probe2_enabled(True)
    engine.set_probe2_temp(70.0)
    temps = await IpcChamberDriver(client).probe_temps()
    assert set(temps) == {"sim-probe-1", "sim-probe-2"}
    assert temps["sim-probe-2"] == 70.0


async def test_clock_sync_drives_the_remote_engines_own_timeline():
    # A separate rig, deliberately NOT sharing the `rig` fixture's
    # SimulatorClock -- this is the actual out-of-process shape: the
    # engine's own clock is a RemoteClock, driven only by clock.sync, the
    # same op daemon/drivers.py's sync_remote_clocks() calls every tick.
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    remote_clock = RemoteClock()
    engine = SimPlantEngine(remote_clock, tuning=ControlTuning(min_on_s=10, min_off_s=10, opposite_lockout_s=20))
    ctx = ServiceContext(
        platform=SimulatorPlatform(engine),
        chamber=SimChamberDriver(engine),
        beer_temp=SimBeerTempSource(engine),
        gravity=SimGravitySource(engine),
        clock=remote_clock,
    )
    server = IPCServer(socket_dir / "sim.sock", ctx=ctx)
    await server.start()
    client = PersistentIPCClient(server.socket_path, heartbeat_interval_s=100)
    await client.start()
    try:
        await asyncio.sleep(0.05)
        driver = IpcChamberDriver(client)
        await driver.set_target(55.0)

        start_mono = remote_clock.monotonic()
        # Nothing has advanced the remote clock yet -- no dt for the engine
        # to have moved the chamber temp with.
        before = await driver.read_chamber()

        await sync_clock(client, now=remote_clock.now() + 60, monotonic=start_mono + 60)
        after = await driver.read_chamber()

        assert after.mode == ChamberMode.COOL
        assert after.temp_f < before.temp_f
    finally:
        await client.stop()
        await server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)


async def test_stop_does_not_hang_on_a_still_open_client_connection():
    # The exact real-world scenario every existing test above sidesteps:
    # their own teardown always closes the CLIENT first (client.stop()
    # before server.stop()), so the server never has to deal with a
    # connection nobody told it to close -- exactly what happens in
    # production when krauken-manual/krauken-simulator receive SIGTERM
    # while the daemon's own persistent IpcPlatformConnection is still
    # open (the daemon isn't shutting down at the same time). Confirmed
    # on real hardware: Server.close() only stops the listening socket
    # (a client connection is left open per Python's own docs), and
    # since Python 3.12, Server.wait_closed() also waits for that
    # connection to finish -- which it never will on its own if nothing
    # is sent and nothing closes it, hanging until systemd's
    # TimeoutStopSec (90s) gives up and SIGKILLs the process. This test
    # opens a raw connection and deliberately never closes it or sends
    # anything, then asserts stop() still returns promptly.
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    engine = SimPlantEngine(SimulatorClock())
    ctx = ServiceContext(
        platform=SimulatorPlatform(engine),
        chamber=SimChamberDriver(engine),
        beer_temp=SimBeerTempSource(engine),
        gravity=SimGravitySource(engine),
        clock=engine.clock,
    )
    server = IPCServer(socket_dir / "sim.sock", ctx=ctx)
    await server.start()

    _reader, writer = await asyncio.open_unix_connection(path=str(server.socket_path))
    # Give the server's own connection-handler task an actual scheduling
    # turn before calling stop() -- without this, the task may not have
    # started (reached its blocking read) yet, and there'd be nothing
    # for the bug to hang on regardless of whether the fix is present,
    # making the test pass for the wrong reason either way.
    await asyncio.sleep(0.1)
    try:
        await asyncio.wait_for(server.stop(), timeout=2.0)
    finally:
        writer.close()
        shutil.rmtree(socket_dir, ignore_errors=True)


async def test_reads_degrade_to_unreachable_when_disconnected():
    # No server at all -- this is the "process is down" case reads should
    # survive without throwing, per ipc_driver.py's own module docstring.
    client = PersistentIPCClient("/tmp/krauken-test-nonexistent.sock", heartbeat_interval_s=100)
    chamber = IpcChamberDriver(client)
    beer = IpcBeerTempSource(client)
    gravity = IpcGravitySource(client)

    reading = await chamber.read_chamber()
    assert reading.health == Health.UNREACHABLE
    assert reading.temp_f is None

    assert (await beer.read()).health == Health.UNREACHABLE
    assert (await gravity.read()).health == Health.UNREACHABLE
    assert await chamber.commanded_target() is None
    await chamber.set_target(60.0)  # must not raise -- logs and drops

    with pytest.raises(PlatformUnavailable):
        # probe_temps is the deliberate exception -- it propagates.
        await chamber.probe_temps()
