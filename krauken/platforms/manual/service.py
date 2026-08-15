"""The Manual driver's own out-of-process entry point -- mirrors
platforms/simulator/service.py exactly (see its module docstring for the
full rationale): a ManualPanel behind the generic IPC service scaffolding
(platforms/ipc_service.py) plus this platform's own dev-panel ops
(manual.get_readings/set_reading), which the API talks to DIRECTLY (see
api/routers/dev_panel.py), never through the daemon.

Meant to run as its own OS process (krauken-manual, deploy/
krauken-manual.service) -- see __main__.py. The panel's clock is always a
RemoteClock (contracts/clock.py): this process never decides real-time vs.
compressed for itself, though in practice Manual only ever runs under
ProductionClock in the daemon (daemon/app.py's _select_clock selects
SimulatorClock only when EVERY mapped role is Simulator) -- it still goes
through the same daemon-driven clock.sync as Simulator, rather than a
special case, so ManualChamberDriver/etc.'s last_good_ts readings stay on
the daemon's own timeline like everything else does."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Mapping

import logging

from krauken.contracts.clock import RemoteClock
from krauken.contracts.errors import ValidationError
from krauken.contracts.models import Health
from krauken.ipc.server import IPCServer, op
from krauken.platforms.ipc_service import ServiceContext
from krauken.platforms.manual.live import ManualBeerTempSource, ManualChamberDriver, ManualGravitySource, ManualPanel
from krauken.platforms.manual.platform import ManualPlatform

log = logging.getLogger("krauken.platforms.manual.service")

_EDITABLE_FIELDS = {
    "chamber": {
        "temp_f", "mode", "health", "cooling_on", "heating_on", "heating_enabled",
        "probe2_enabled", "probe2_temp_f",
    },
    "tilt": {"temp_f", "gravity_sg", "health", "available"},
}

_BOOL_FIELDS = {"cooling_on", "heating_on", "heating_enabled", "probe2_enabled", "available"}


class ManualServiceContext(ServiceContext):
    """Adds the raw ManualPanel -- needed by this platform's own dev-panel
    ops below, which reach past the generic ChamberDriver/BeerTempSource/
    GravitySource Protocols on purpose. Nothing in ipc_service.py's shared
    ops ever touches this attribute."""

    def __init__(self, panel: ManualPanel):
        super().__init__(
            platform=ManualPlatform(panel),
            chamber=ManualChamberDriver(panel),
            beer_temp=ManualBeerTempSource(panel),
            gravity=ManualGravitySource(panel),
            clock=panel.clock,
        )
        self.panel = panel


def _panel_state(ctx: ManualServiceContext, field: str) -> Any:
    if field == "chamber":
        return ctx.panel.chamber
    if field == "tilt":
        return ctx.panel.tilt
    raise ValidationError(f"unknown manual panel field {field!r} -- expected chamber/tilt")


def _coerce(key: str, value: Any) -> Any:
    if key == "health":
        try:
            return Health(value)
        except ValueError:
            raise ValidationError(f"invalid health {value!r}") from None
    if key == "mode":
        if value not in ("idle", "cool", "heat"):
            raise ValidationError(f"mode must be idle/cool/heat, got {value!r}")
        return value
    if key in _BOOL_FIELDS:
        return bool(value)
    return None if value is None else float(value)


@op("manual.get_readings")
async def _get_readings(ctx: ManualServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chamber": dataclasses.asdict(ctx.panel.chamber),
        "tilt": dataclasses.asdict(ctx.panel.tilt),
    }


@op("manual.set_reading", mutating=True)
async def _set_reading(ctx: ManualServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    field = args["field"]
    values = args.get("values") or {}
    state = _panel_state(ctx, field)

    allowed = _EDITABLE_FIELDS[field]
    unknown = set(values) - allowed
    if unknown:
        raise ValidationError(f"{field}: unsupported fields {sorted(unknown)} -- allowed: {sorted(allowed)}")

    for key, raw_value in values.items():
        setattr(state, key, _coerce(key, raw_value))

    if field == "chamber" and not state.heating_enabled:
        state.heating_on = False  # no heater wired -- can't be on

    return dataclasses.asdict(state)


class ManualService:
    def __init__(self, *, socket_path: Path):
        self.clock = RemoteClock()
        self.panel = ManualPanel(self.clock)
        self.ctx = ManualServiceContext(self.panel)
        self.server = IPCServer(socket_path, self.ctx)

    async def start(self) -> None:
        await self.server.start()
        log.info("manual service started")

    async def stop(self) -> None:
        await self.server.stop()
        log.info("manual service stopped")


def build_service(*, socket_path: Path) -> ManualService:
    return ManualService(socket_path=socket_path)
