"""Generic IPC-backed implementations of ChamberDriver/BeerTempSource/
GravitySource/PlatformDriver -- the daemon-side half of the out-of-process
pattern. One set of classes, reused verbatim by every platform that runs as
a separate process (Simulator, Manual, and eventually the real Hardware
Supervisor): the wire vocabulary is just the Protocol methods themselves
(see ipc_service.py, the matching server-side half), so there is nothing
platform-specific to write here at all. A platform earns its own classes
only if it needs a driver shape genuinely different from these three
Protocols -- none do today.

Error handling follows one rule throughout: PlatformUnavailable (raised by
PersistentIPCClient.call() whenever the connection is down) degrades a
*read* into a Health.UNREACHABLE reading rather than raising, so the
existing failsafe machinery (contracts/failsafe.py) handles "can't reach
the box" exactly like "the box's own sensor is unreachable" -- one
mechanism, not two. A *write* (set_target, set_ambient_location) logs and
drops instead: the control loop calls set_target every tick regardless of
whether the value changed, so a transient failure here self-heals on the
very next successful tick, and there is nothing more useful to do with a
setpoint nobody's listening for right now. probe_temps() is the deliberate
exception -- it's only ever called from the guided wizard's identify-probes
test, which should see a real failure and report it, not silently pretend
zero probes exist.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from krauken.config import DEFAULT_MANUAL_SOCKET, DEFAULT_SIMULATOR_SOCKET
from krauken.contracts.errors import PlatformUnavailable
from krauken.contracts.models import BeerReading, ChamberMode, ChamberReading, DeviceCandidate, GravityReading, Health
from krauken.ipc.persistent_client import PersistentIPCClient

log = logging.getLogger("krauken.platforms.ipc_driver")


class IpcPlatformConnection:
    """Owns the persistent connection to an out-of-process platform's HAL
    (Simulator, Manual, and eventually the real Hardware Supervisor) --
    the one place "how do I reach this platform's process, including
    which socket" lives. Subclasses set env_var/default_socket_path --
    each knows its OWN path; nothing external resolves or passes one in
    during normal (zero-arg) construction. platforms/registry.py's
    PlatformRegistry is the only thing that ever constructs these, and it
    always does so with zero arguments -- daemon/app.py used to own both
    the raw PersistentIPCClient AND the socket path itself (an
    abstraction violation); this class is where that knowledge now lives,
    the same way BrewPiConnection/TiltScanner already own their own
    connection details.

    The optional `socket_path` override exists ONLY for tests that need a
    specific throwaway socket without env-var choreography (e.g.
    tests/unit/test_ipc_service.py's direct driver tests) -- production
    construction (PlatformBinding.build_state) always calls these with
    zero arguments."""

    env_var: str
    default_socket_path: str

    def __init__(self, socket_path: Path | str | None = None, **client_kwargs: Any):
        if socket_path is None:
            socket_path = os.environ.get(self.env_var, self.default_socket_path)
        self._client = PersistentIPCClient(socket_path, **client_kwargs)

    @property
    def socket_path(self) -> str:
        return self._client.socket_path

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def call(self, op_name: str, args: Mapping[str, Any] | None = None, *, deadline_ms: int = 3000) -> Any:
        return await self._client.call(op_name, args, deadline_ms=deadline_ms)


class ManualIpcConnection(IpcPlatformConnection):
    env_var = "KRAUKEN_MANUAL_SOCKET"
    default_socket_path = DEFAULT_MANUAL_SOCKET


class SimulatorIpcConnection(IpcPlatformConnection):
    env_var = "KRAUKEN_SIMULATOR_SOCKET"
    default_socket_path = DEFAULT_SIMULATOR_SOCKET


async def sync_clock(client: IpcPlatformConnection, *, now: float, monotonic: float) -> None:
    """Tells the remote process's RemoteClock what time the daemon is
    currently using -- see contracts/clock.py's RemoteClock and
    daemon/drivers.py's sync_remote_clocks(), which calls this once per
    control tick against every IPC-backed platform regardless of current
    mapping (cheap and idempotent, same idiom as set_ambient_location).
    Best-effort: a platform that's currently unreachable just stays on
    whatever time it last knew about until it reconnects, same as any
    other write in this module."""
    try:
        await client.call("clock.sync", {"now": now, "monotonic": monotonic})
    except PlatformUnavailable as e:
        log.debug("clock.sync dropped: %s", e)


def _candidate_from_wire(c: Mapping[str, Any]) -> DeviceCandidate:
    return DeviceCandidate(
        device_id=c["device_id"],
        platform=c["platform"],
        display_name=c["display_name"],
        kind_label=c["kind_label"],
        capabilities=frozenset(c["capabilities"]),
        bundled_roles=frozenset(c["bundled_roles"]),
        health=Health(c["health"]),
        health_note=c.get("health_note", ""),
        detail_line=c.get("detail_line", ""),
        reading_summary=c.get("reading_summary"),
        readings=c.get("readings") or {},
        identity=c.get("identity") or {},
        last_seen_ts=c.get("last_seen_ts"),
        requires_setup=c.get("requires_setup", False),
        available_tests=tuple(c.get("available_tests") or ()),
        simulated=c.get("simulated", False),
        platform_config=c.get("platform_config") or {},
    )


class IpcPlatformDriver:
    """Base for a platform's discover()-only driver -- subclasses set
    platform_id/display_name as class attributes, matching every other
    PlatformDriver implementation's own shape."""

    platform_id: str
    display_name: str

    def __init__(self, connection: IpcPlatformConnection):
        self._connection = connection

    async def discover(self, ctx: Mapping[str, Any]) -> Sequence[DeviceCandidate]:
        # Deliberately NOT caught here: PlatformUnavailable propagating out
        # of discover() is exactly what daemon/discovery.py's _discover_one
        # already expects and handles (a platform_status of "unavailable"
        # for this scan, not a crashed scan).
        result = await self._connection.call("platform.discover")
        return [_candidate_from_wire(c) for c in result["candidates"]]


class IpcChamberDriver:
    def __init__(self, connection: IpcPlatformConnection, device_id: str | None = None):
        # device_id unused -- Manual/Simulator each expose exactly one
        # chamber device, so there's nothing to disambiguate (see
        # daemon/drivers.py's own docstring on why every dispatch function
        # passes it uniformly regardless).
        self._connection = connection

    async def read_chamber(self) -> ChamberReading:
        try:
            r = await self._connection.call("chamber.read_chamber")
        except PlatformUnavailable:
            return ChamberReading(temp_f=None, mode=ChamberMode.IDLE, health=Health.UNREACHABLE, last_good_ts=None)
        return ChamberReading(
            temp_f=r["temp_f"],
            mode=ChamberMode(r["mode"]),
            health=Health(r["health"]),
            last_good_ts=r["last_good_ts"],
            commanded_target_f=r.get("commanded_target_f"),
            detail=r.get("detail", ""),
        )

    async def set_target(self, temp_f: float | None) -> None:
        try:
            await self._connection.call("chamber.set_target", {"temp_f": temp_f})
        except PlatformUnavailable as e:
            log.warning("set_target(%s) dropped: %s", temp_f, e)

    async def commanded_target(self) -> float | None:
        try:
            r = await self._connection.call("chamber.commanded_target")
        except PlatformUnavailable:
            return None
        return r["temp_f"]

    async def set_ambient_location(self, location: str | None) -> None:
        try:
            await self._connection.call("chamber.set_ambient_location", {"location": location})
        except PlatformUnavailable as e:
            log.warning("set_ambient_location(%r) dropped: %s", location, e)

    async def probe_temps(self) -> dict[str, float | None]:
        r = await self._connection.call("chamber.probe_temps")
        return r["temps"]


class IpcBeerTempSource:
    def __init__(self, connection: IpcPlatformConnection, device_id: str | None = None):
        self._connection = connection  # device_id unused -- see IpcChamberDriver's own comment

    async def read(self) -> BeerReading:
        try:
            r = await self._connection.call("beer_temp.read")
        except PlatformUnavailable:
            return BeerReading(temp_f=None, health=Health.UNREACHABLE, last_good_ts=None)
        return BeerReading(temp_f=r["temp_f"], health=Health(r["health"]), last_good_ts=r["last_good_ts"])


class IpcGravitySource:
    def __init__(self, connection: IpcPlatformConnection, device_id: str | None = None):
        self._connection = connection  # device_id unused -- see IpcChamberDriver's own comment

    async def read(self) -> GravityReading:
        try:
            r = await self._connection.call("gravity.read")
        except PlatformUnavailable:
            return GravityReading(gravity_sg=None, health=Health.UNREACHABLE, last_good_ts=None)
        return GravityReading(gravity_sg=r["gravity_sg"], health=Health(r["health"]), last_good_ts=r["last_good_ts"])
