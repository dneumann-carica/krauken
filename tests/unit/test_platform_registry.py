"""PlatformRegistry against real Simulator/Manual out-of-process servers --
each platform_id's discover() now genuinely crosses IPC (platforms/
ipc_driver.py/ipc_service.py), so there's no more "static shape, no live
connection" mode to test against (see registry.py's own PlatformRegistry
docstring): every one of these needs a real, connected IpcPlatformConnection,
same shape tests/api/conftest.py's simulator_service/manual_service
fixtures already establish, just self-contained here since this file lives
outside tests/api/'s fixture tree.

state_overrides is PlatformRegistry's test-only escape hatch: it lets these
fixtures point Manual/Simulator's connection at THIS test's own throwaway
socket (not the real KRAUKEN_MANUAL_SOCKET/KRAUKEN_SIMULATOR_SOCKET
default) without needing env-var choreography -- production code never
uses it."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.errors import PlatformUnavailable
from krauken.contracts.roles import CHAMBER_BUNDLE, Role
from krauken.platforms.ipc_driver import ManualIpcConnection, SimulatorIpcConnection
from krauken.platforms.manual.service import build_service as build_manual_service
from krauken.platforms.registry import PlatformRegistry
from krauken.platforms.simulator.service import build_service as build_simulator_service


@pytest_asyncio.fixture
async def registry(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PlatformRegistry]:
    # Restrict to just manual+simulator -- KRAUKEN_PLATFORMS defaults to
    # all four, and brewpi/tilt's own build_state constructs fine with no
    # real hardware attached, but test_default_registry_has_manual_and_
    # simulator below wants an exact {"manual", "simulator"} set.
    monkeypatch.setenv("KRAUKEN_PLATFORMS", "manual,simulator")

    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    simulator_service = build_simulator_service(socket_path=socket_dir / "sim.sock")
    manual_service = build_manual_service(socket_path=socket_dir / "man.sock")
    await simulator_service.start()
    await manual_service.start()

    simulator_connection = SimulatorIpcConnection(simulator_service.server.socket_path)
    manual_connection = ManualIpcConnection(manual_service.server.socket_path)
    await simulator_connection.start()
    await manual_connection.start()
    await asyncio.sleep(0.05)  # start() doesn't wait for the connection -- give it a moment

    reg = PlatformRegistry(
        clock=SimulatorClock(),
        state_overrides={"manual": manual_connection, "simulator": simulator_connection},
    )
    yield reg

    await simulator_connection.stop()
    await manual_connection.stop()
    await simulator_service.stop()
    await manual_service.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_default_registry_has_manual_and_simulator(registry):
    ids = {d.platform_id for d in registry}
    assert ids == {"manual", "simulator"}


async def test_simulator_emits_two_independent_candidates(registry):
    # Per project decision: the simulator must emit a chamber candidate AND
    # a separate hydrometer-like candidate, not one combined device -- so a
    # pure-sim dev environment still exercises the bundle rule and the
    # independent-beer-temp-source path, not a shortcut around them.
    simulator = next(d for d in registry if d.platform_id == "simulator")
    candidates = await simulator.discover({})
    assert len(candidates) == 2
    by_id = {c.device_id: c for c in candidates}
    assert by_id["simulator:chamber"].bundled_roles == CHAMBER_BUNDLE
    assert by_id["simulator:tilt"].bundled_roles == frozenset()
    assert Role.BEER_GRAVITY in by_id["simulator:tilt"].capabilities


async def test_manual_candidates_are_marked_simulated(registry):
    manual = next(d for d in registry if d.platform_id == "manual")
    candidates = await manual.discover({})
    assert all(c.simulated for c in candidates)


def test_registry_includes_brewpi_and_tilt_via_their_own_build_state(monkeypatch: pytest.MonkeyPatch):
    # BrewPi/Tilt are in-process (no IPC -- see registry.py's own
    # docstring) -- unlike manual/simulator above, this needs no real
    # sockets/services at all: PlatformRegistry constructs both directly
    # via PLATFORM_BINDINGS' own build_state factories, no overrides
    # needed, confirming build_state/PlatformBinding never actually
    # assumed an IPC connection specifically.
    monkeypatch.setenv("KRAUKEN_PLATFORMS", "brewpi,tilt")
    reg = PlatformRegistry(clock=SimulatorClock())
    assert {d.platform_id for d in reg} == {"brewpi", "tilt"}


def test_daemon_drivers_dispatch_generically_for_brewpi_and_tilt(monkeypatch: pytest.MonkeyPatch):
    # The same chamber_driver()/beer_temp_source()/gravity_source()
    # functions daemon/control_loop.py calls for every platform -- proof
    # this needed zero special-casing for a non-IPC platform. ctx.registry
    # is a real PlatformRegistry now (dispatch asks it for each platform's
    # state object by name), not a fake object with named attributes.
    from krauken.daemon import drivers
    from krauken.platforms.brewpi.live import BrewPiBeerTempSource, BrewPiChamberDriver
    from krauken.platforms.tilt.live import TiltBeerTempSource, TiltGravitySource

    monkeypatch.setenv("KRAUKEN_PLATFORMS", "brewpi,tilt")

    class _Ctx:
        registry = PlatformRegistry(clock=SimulatorClock())

    ctx = _Ctx()
    assert isinstance(drivers.chamber_driver(ctx, "brewpi"), BrewPiChamberDriver)
    assert isinstance(drivers.beer_temp_source(ctx, "brewpi"), BrewPiBeerTempSource)
    assert drivers.gravity_source(ctx, "brewpi") is None  # BrewPi has no hydrometer

    assert drivers.chamber_driver(ctx, "tilt") is None  # Tilt never fills the chamber role
    assert isinstance(drivers.beer_temp_source(ctx, "tilt"), TiltBeerTempSource)
    assert isinstance(drivers.gravity_source(ctx, "tilt"), TiltGravitySource)


@pytest_asyncio.fixture
async def all_four_registry() -> AsyncIterator[PlatformRegistry]:
    """All four platforms at once -- proof that enabling the real `tilt`
    platform never shadows or interferes with Simulator's own pre-existing
    `simulator:tilt` mock hydrometer (see platform.py's own docstring:
    that device has always computed beer temp/gravity from the shared
    SimPlantEngine, completely independent of the real Tilt BLE scanner --
    nothing about adding a real `tilt` platform touches it). brewpi/tilt
    need no override at all -- their own build_state constructs fine
    against SimulatorClock with no real hardware attached; only manual/
    simulator need pointing at this test's own throwaway sockets."""
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    simulator_service = build_simulator_service(socket_path=socket_dir / "sim.sock")
    manual_service = build_manual_service(socket_path=socket_dir / "man.sock")
    await simulator_service.start()
    await manual_service.start()

    simulator_connection = SimulatorIpcConnection(simulator_service.server.socket_path)
    manual_connection = ManualIpcConnection(manual_service.server.socket_path)
    await simulator_connection.start()
    await manual_connection.start()
    await asyncio.sleep(0.05)

    reg = PlatformRegistry(
        clock=SimulatorClock(),
        state_overrides={"manual": manual_connection, "simulator": simulator_connection},
    )
    yield reg

    await simulator_connection.stop()
    await manual_connection.stop()
    await simulator_service.stop()
    await manual_service.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_simulated_tilt_and_real_tilt_platform_coexist(all_four_registry):
    ids = {d.platform_id for d in all_four_registry}
    assert ids == {"manual", "simulator", "brewpi", "tilt"}

    simulator = next(d for d in all_four_registry if d.platform_id == "simulator")
    tilt = next(d for d in all_four_registry if d.platform_id == "tilt")

    sim_candidates = await simulator.discover({})
    try:
        # No real Tilt in range on this test machine -- either an empty
        # list (Linux, real hardware, no beacon seen) or PlatformUnavailable
        # (this dev Mac: aioblescan's raw HCI socket is Linux-only). Either
        # way, this must never affect the simulator's own candidates --
        # that's the actual thing being proven here, same as
        # discovery.py's own per-platform isolation.
        tilt_candidates = await tilt.discover({})
    except PlatformUnavailable:
        tilt_candidates = []

    sim_tilt = next(c for c in sim_candidates if c.device_id == "simulator:tilt")
    assert Role.BEER_TEMP in sim_tilt.capabilities
    assert Role.BEER_GRAVITY in sim_tilt.capabilities
    assert sim_tilt.simulated is True
    assert tilt_candidates == []  # unaffected either way -- proves no shadowing/interference


async def test_device_id_reaches_the_tilt_driver_through_the_real_dispatch_chain(monkeypatch: pytest.MonkeyPatch):
    # End-to-end proof for the actual bug fix: hardware_config's device_id
    # column -> drivers.py's dispatch functions -> the driver constructor
    # -> which color gets read. Two Tilts detected; daemon/drivers.py must
    # deliver the one that was actually mapped, not just "whichever sorts
    # first" (see test_tilt_live.py for that fallback's own coverage).
    from krauken.daemon import drivers
    from krauken.platforms.tilt.scanner import TiltReading, TiltScanner

    monkeypatch.setenv("KRAUKEN_PLATFORMS", "tilt")
    clock = SimulatorClock()
    scanner = TiltScanner(clock, hci_device=0)
    scanner._readings["purple"] = TiltReading(70.0, 1.020, -60, clock.monotonic())
    scanner._readings["black"] = TiltReading(65.0, 1.010, -55, clock.monotonic())

    class _Ctx:
        registry = PlatformRegistry(clock=clock, state_overrides={"tilt": scanner})

    ctx = _Ctx()
    purple_source = drivers.beer_temp_source(ctx, "tilt", "tilt:purple")
    black_gravity = drivers.gravity_source(ctx, "tilt", "tilt:black")

    assert (await purple_source.read()).temp_f == 70.0
    assert (await black_gravity.read()).gravity_sg == 1.010
