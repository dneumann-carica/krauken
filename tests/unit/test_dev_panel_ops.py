from __future__ import annotations

import pytest

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.errors import ValidationError
from krauken.contracts.models import Health
from krauken.daemon.ops.dev_panel import (
    _get_clock,
    _get_readings,
    _get_simulator_readings,
    _set_reading,
    _set_simulator_probe2,
)
from krauken.platforms.manual.live import ManualPanel
from krauken.platforms.simulator.live import SimPlantEngine


class _FakeCtx:
    def __init__(self):
        self.manual_panel = ManualPanel(clock=None)  # clock unused by these ops
        self.clock = SimulatorClock()
        self.sim_engine = SimPlantEngine(self.clock)


async def test_get_readings_reflects_current_panel_state():
    ctx = _FakeCtx()
    ctx.manual_panel.tilt.temp_f = 71.5
    result = await _get_readings(ctx, {})
    assert result["tilt"]["temp_f"] == 71.5
    assert result["chamber"]["health"] == "ok"


async def test_set_reading_updates_temp_and_health():
    ctx = _FakeCtx()
    result = await _set_reading(ctx, {"field": "tilt", "values": {"temp_f": 80.0, "health": "degraded"}})
    assert result["temp_f"] == 80.0
    assert result["health"] == "degraded"
    assert ctx.manual_panel.tilt.health == Health.DEGRADED


async def test_set_reading_can_explicitly_null_out_a_temp():
    ctx = _FakeCtx()
    ctx.manual_panel.tilt.gravity_sg = 1.040
    await _set_reading(ctx, {"field": "tilt", "values": {"gravity_sg": None}})
    assert ctx.manual_panel.tilt.gravity_sg is None


async def test_set_reading_rejects_a_field_not_valid_for_the_target():
    ctx = _FakeCtx()
    with pytest.raises(ValidationError):
        await _set_reading(ctx, {"field": "tilt", "values": {"mode": "cool"}})  # tilt has no `mode`


async def test_set_reading_rejects_an_unknown_target():
    ctx = _FakeCtx()
    with pytest.raises(ValidationError):
        await _set_reading(ctx, {"field": "nonsense", "values": {}})


async def test_set_reading_rejects_an_invalid_health_value():
    ctx = _FakeCtx()
    with pytest.raises(ValidationError):
        await _set_reading(ctx, {"field": "tilt", "values": {"health": "on_fire"}})


async def test_set_reading_rejects_an_invalid_mode_value():
    ctx = _FakeCtx()
    with pytest.raises(ValidationError):
        await _set_reading(ctx, {"field": "chamber", "values": {"mode": "vaporize"}})


async def test_disabling_heating_forces_heating_on_off():
    ctx = _FakeCtx()
    ctx.manual_panel.chamber.heating_on = True
    result = await _set_reading(ctx, {"field": "chamber", "values": {"heating_enabled": False}})
    assert result["heating_on"] is False
    assert ctx.manual_panel.chamber.heating_enabled is False


async def test_chamber_probe2_fields_round_trip():
    ctx = _FakeCtx()
    result = await _set_reading(
        ctx, {"field": "chamber", "values": {"probe2_enabled": True, "probe2_temp_f": 72.5}}
    )
    assert result["probe2_enabled"] is True
    assert result["probe2_temp_f"] == 72.5


async def test_tilt_availability_toggle_round_trips():
    ctx = _FakeCtx()
    result = await _set_reading(ctx, {"field": "tilt", "values": {"available": False}})
    assert result["available"] is False


async def test_simulator_probe2_toggle_round_trips():
    ctx = _FakeCtx()
    assert (await _get_simulator_readings(ctx, {}))["probe2_enabled"] is False

    result = await _set_simulator_probe2(ctx, {"enabled": True, "temp_f": 72.5})
    assert result == {"probe2_enabled": True, "probe2_temp_f": 72.5}

    again = await _get_simulator_readings(ctx, {})
    assert again["probe2_enabled"] is True
    assert again["probe2_temp_f"] == 72.5


async def test_get_clock_reads_the_current_time():
    ctx = _FakeCtx()
    ctx.clock.advance(3600.0)
    result = await _get_clock(ctx, {})
    assert result["now"]  # a real ISO timestamp string, non-empty
