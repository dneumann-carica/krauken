"""IPC ops for the Manual driver's dev panel (platforms/manual/live.py).
Gated by Config.dev_panel_enabled at the API tier ONLY (see
api/routers/dev_panel.py) -- the op handlers here have no opinion about
whether this is "allowed", only whether the requested field/values are
well-formed. The daemon's IPC socket is already a local, permission-
restricted channel (0660, unix socket) that only the API tier is expected
to talk to; double-gating it here was judged not worth the added state
(DaemonContext would need to know about a Config it otherwise never sees).
"""
from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Mapping

from krauken.contracts.errors import ValidationError
from krauken.contracts.models import Health
from krauken.ipc.server import op

_EDITABLE_FIELDS = {
    "chamber": {
        "temp_f", "mode", "health", "cooling_on", "heating_on", "heating_enabled",
        "probe2_enabled", "probe2_temp_f",
    },
    "tilt": {"temp_f", "gravity_sg", "health", "available"},
}

_BOOL_FIELDS = {"cooling_on", "heating_on", "heating_enabled", "probe2_enabled", "available"}


def _panel_state(ctx: Any, field: str) -> Any:
    panel = ctx.manual_panel
    if field == "chamber":
        return panel.chamber
    if field == "tilt":
        return panel.tilt
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
async def _get_readings(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    panel = ctx.manual_panel
    return {
        "chamber": dataclasses.asdict(panel.chamber),
        "tilt": dataclasses.asdict(panel.tilt),
    }


@op("manual.set_reading", mutating=True)
async def _set_reading(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
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


@op("simulator.get_readings")
async def _get_simulator_readings(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    engine = ctx.sim_engine
    chamber = engine.read_chamber()
    return {
        "chamber_temp_f": chamber.temp_f,
        "mode": chamber.mode.value,
        "probe2_enabled": engine.probe2_enabled,
        "probe2_temp_f": engine.probe2_temp_f,
    }


@op("simulator.set_probe2", mutating=True)
async def _set_simulator_probe2(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    engine = ctx.sim_engine
    if "enabled" in args:
        engine.set_probe2_enabled(bool(args["enabled"]))
    if "temp_f" in args:
        value = args["temp_f"]
        engine.set_probe2_temp(None if value is None else float(value))
    return {"probe2_enabled": engine.probe2_enabled, "probe2_temp_f": engine.probe2_temp_f}


@op("dev_panel.get_clock")
async def _get_clock(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"now": _iso(ctx.clock.now())}


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
