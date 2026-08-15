"""The Manual driver's dev panel: lets a developer hand-set exactly what
the Manual chamber/beer/gravity "sensors" report, so the control loop's
response to a specific reading (or a lost one) can be exercised without
real hardware -- see platforms/manual/live.py's module docstring.

Talks DIRECTLY to Simulator's/Manual's own out-of-process servers
(deps.simulator()/deps.manual()) -- not through the daemon. See
api/deps.py's own module docstring for why: these ops mutate state that
belongs to the platform process itself, and proxying them through the
daemon would mean either relay code with nothing daemon-specific to add, or
leaking dev-tooling concerns into the ChamberDriver/BeerTempSource/
GravitySource Protocols those two processes also serve to the daemon for
real control. get_clock() is the one exception -- it reports the DAEMON's
own clock selection (real vs. compressed), a daemon-level fact, not
Manual/Simulator state, so it stays on deps.daemon().

Gated behind Config.dev_panel_enabled (KRAUKEN_DEV_PANEL=1) -- off by
default, since a zero-auth LAN device shouldn't let just anyone on the
network start feeding the control loop fake readings.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from krauken.api import deps
from krauken.api.schemas import (
    ClockResponse,
    ManualReadingResponse,
    ManualReadingsResponse,
    ManualSetReadingRequest,
    SimulatorReadingResponse,
    SimulatorSetProbe2Request,
)
from krauken.contracts.errors import DevPanelDisabled
from krauken.ipc.client import AsyncIPCClient

router = APIRouter()


def _require_enabled() -> None:
    if not deps.get_config().dev_panel_enabled:
        raise DevPanelDisabled("the dev panel is disabled -- set KRAUKEN_DEV_PANEL=1 to enable it")


@router.get("/dev/manual", response_model=ManualReadingsResponse)
async def get_manual_readings(client: AsyncIPCClient = Depends(deps.manual)) -> ManualReadingsResponse:
    _require_enabled()
    result = await client.call("manual.get_readings")
    return ManualReadingsResponse(**result)


@router.put("/dev/manual/{field}", response_model=ManualReadingResponse)
async def set_manual_reading(
    field: str, body: ManualSetReadingRequest, client: AsyncIPCClient = Depends(deps.manual)
) -> ManualReadingResponse:
    _require_enabled()
    # exclude_unset, not exclude_none -- explicitly setting a field to null
    # (e.g. temp_f: null) is a real dev-panel action (simulate a sensor
    # that stopped reporting); omitting the field entirely means "leave it
    # alone". Only the far side's own op actually knows which fields are
    # valid for which target (chamber/beer/gravity), so an unsupported
    # field for this `field` still round-trips here and gets rejected there.
    values = body.model_dump(exclude_unset=True)
    result = await client.call("manual.set_reading", {"field": field, "values": values})
    return ManualReadingResponse(**result)


@router.get("/dev/simulator", response_model=SimulatorReadingResponse)
async def get_simulator_readings(client: AsyncIPCClient = Depends(deps.simulator)) -> SimulatorReadingResponse:
    _require_enabled()
    result = await client.call("simulator.get_readings")
    return SimulatorReadingResponse(**result)


@router.put("/dev/simulator/probe2", response_model=SimulatorReadingResponse)
async def set_simulator_probe2(
    body: SimulatorSetProbe2Request, client: AsyncIPCClient = Depends(deps.simulator)
) -> SimulatorReadingResponse:
    _require_enabled()
    args = body.model_dump(exclude_unset=True)
    await client.call("simulator.set_probe2", args)
    result = await client.call("simulator.get_readings")
    return SimulatorReadingResponse(**result)


@router.get("/dev/clock", response_model=ClockResponse)
async def get_clock(client: AsyncIPCClient = Depends(deps.daemon)) -> ClockResponse:
    _require_enabled()
    result = await client.call("dev_panel.get_clock")
    return ClockResponse(**result)
