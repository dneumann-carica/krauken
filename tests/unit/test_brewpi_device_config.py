"""platforms/brewpi/device_config.py's five test-job runners, dispatched
through the REAL daemon/tests_runtime.py:start_test() fallback path (the
same PLATFORM_BINDINGS["brewpi"].test_runners lookup production code
uses) against a fake BrewPiConnection -- fast and deterministic, unlike
waiting out the real anti-short-cycle timers would be. Mirrors
test_confirm_heater.py's fake-driver/fake-clock shape."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from krauken.contracts.errors import FermentationAlreadyActive
from krauken.daemon import tests_runtime
from krauken.platforms.brewpi import device_config
from krauken.platforms.brewpi.connection import BrewPiReading
from krauken.platforms.brewpi.device_config import BrewPiDevice

DEVICE_ID = "brewpi:controller"


@dataclass
class _FakeBrewPiConnection:
    """Reflects install_device() calls back into installed/available
    (mimicking real EEPROM persistence) so subsequent free-slot picks
    within the same test see a consistent picture, same as the real
    Arduino would report on a follow-up 'd'/'h' query."""

    installed: list[BrewPiDevice] = field(default_factory=list)
    available: list[BrewPiDevice] = field(default_factory=list)
    fridge_temp_f: float | None = 65.0
    state_sequence: list[int] = field(default_factory=lambda: [0])
    install_calls: list[BrewPiDevice] = field(default_factory=list)
    reset_calls: int = 0
    set_target_calls: list[float | None] = field(default_factory=list)
    raise_on_install_call: int | None = None  # 1-indexed -- which install_device() call raises
    _state_idx: int = 0

    async def list_installed_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        return list(self.installed)

    async def list_available_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        return list(self.available)

    async def list_all_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        return list(self.installed) + list(self.available)

    async def install_device(self, device: BrewPiDevice) -> None:
        # A real yield point -- lets a test interleave a cancel_test() call
        # between installs (a fake with no internal await at all would run
        # the whole runner to completion in one scheduling turn, making
        # "cancelled mid-loop" untestable).
        await asyncio.sleep(0)
        self.install_calls.append(device)
        if self.raise_on_install_call is not None and len(self.install_calls) == self.raise_on_install_call:
            raise RuntimeError("simulated install failure")
        self.installed = [d for d in self.installed if d.slot != device.slot] + [device]
        self.available = [d for d in self.available if d.pin != device.pin or d.address != device.address]

    async def reset_and_reconnect(self) -> bool:
        self.reset_calls += 1
        return True

    async def read_temps(self) -> BrewPiReading | None:
        if self.fridge_temp_f is None:
            return None
        state = self.state_sequence[min(self._state_idx, len(self.state_sequence) - 1)]
        self._state_idx += 1
        return BrewPiReading(beer_temp_f=None, fridge_temp_f=self.fridge_temp_f, fridge_set_f=None, state=state)

    async def set_fridge_target(self, temp_f: float | None) -> None:
        self.set_target_calls.append(temp_f)


@dataclass
class _SequencedOneWireConnection:
    """A different device-list snapshot on each successive call, repeating
    the last once exhausted -- same index-based approach as
    test_confirm_heater.py's _FakeChamberDriver."""

    snapshots: list[list[BrewPiDevice]]
    _idx: int = 0

    async def list_all_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        idx = min(self._idx, len(self.snapshots) - 1)
        self._idx += 1
        return list(self.snapshots[idx])


class _FakeClock:
    def now(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


class _FakeRegistry:
    def __init__(self, connection: Any):
        self._connection = connection

    def state_for(self, platform_id: str) -> Any | None:
        return self._connection if platform_id == "brewpi" else None


class _FakeCtx:
    def __init__(self, connection: Any):
        self.clock = _FakeClock()
        self.jobs: dict[str, Any] = {}
        self.registry = _FakeRegistry(connection)

    @property
    def conn(self) -> Any:
        return None  # only touched via queries.active_fermentation, patched per-test


def _patch_no_active_fermentation(monkeypatch: pytest.MonkeyPatch) -> None:
    # The check now lives in tests_runtime.py's dispatch itself (checked
    # synchronously before the task is created -- see PlatformTestRunner's
    # docstring), not inside device_config.py at all.
    monkeypatch.setattr(tests_runtime.queries, "active_fermentation", lambda conn: None)


def _patch_active_fermentation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tests_runtime.queries, "active_fermentation", lambda conn: {"id": 1})


# --- sweep_relay ---


async def test_sweep_relay_reports_engaged_once_state_enters_the_engaged_set(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(state_sequence=[0, 0, 4])  # idle, idle, COOLING
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "sweep_relay", {"function": "cool", "candidate": {"pin": 6, "invert": 1}, "window_s": 30.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["outcome"] == "engaged"
    # Installed exactly once, on the requested pin with the requested invert.
    assert len(conn.install_calls) == 1
    assert conn.install_calls[0].pin == 6
    assert conn.install_calls[0].invert == 1
    assert conn.install_calls[0].function == device_config.DEVICE_FUNCTION_CHAMBER_COOL
    # A real setpoint was forced -- below baseline, for a cool test.
    assert conn.set_target_calls[-1] < conn.fridge_temp_f
    # Deliberately no revert -- the candidate stays installed/energized.
    assert conn.reset_calls == 0


async def test_sweep_relay_reports_waiting_before_engaging(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(state_sequence=[6, 6, 3])  # WAITING_TO_HEAT, WAITING_TO_HEAT, HEATING
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "sweep_relay", {"function": "heat", "candidate": {"pin": 19}, "window_s": 30.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["outcome"] == "engaged"
    assert conn.set_target_calls[-1] > conn.fridge_temp_f  # forced above baseline, for a heat test


async def test_sweep_relay_times_out_cleanly_when_never_engaged(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(state_sequence=[0])  # never anything but idle
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "sweep_relay", {"function": "cool", "candidate": {"pin": 2}, "window_s": 6.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["outcome"] == "timeout"
    # Still installed and left as-is -- no revert on timeout either.
    assert len(conn.install_calls) == 1
    assert conn.reset_calls == 0


async def test_sweep_relay_reuses_the_existing_slot_for_the_same_pin_and_function(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(
        installed=[BrewPiDevice(slot=3, function=device_config.DEVICE_FUNCTION_CHAMBER_COOL, hardware=1, pin=5, invert=1)],
        state_sequence=[4],
    )
    ctx = _FakeCtx(conn)

    # Retry with reversed polarity on the SAME pin -- should land on the
    # same slot (3), not consume a new one.
    result = tests_runtime.start_test(ctx, DEVICE_ID, "sweep_relay", {"function": "cool", "candidate": {"pin": 5, "invert": 0}, "window_s": 30.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert conn.install_calls[0].slot == 3
    assert conn.install_calls[0].invert == 0  # the flipped polarity took effect


async def test_sweep_relay_fails_cleanly_when_the_chamber_probe_isnt_reading(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(fridge_temp_f=None)
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "sweep_relay", {"function": "cool", "candidate": {"pin": 2}, "window_s": 6.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "failed"
    assert "chamber probe" in job.error


async def test_sweep_relay_is_blocked_while_a_fermentation_is_active(monkeypatch: pytest.MonkeyPatch):
    _patch_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    with pytest.raises(FermentationAlreadyActive):
        tests_runtime.start_test(ctx, DEVICE_ID, "sweep_relay", {"function": "cool", "candidate": {"pin": 2}})
    assert conn.install_calls == []


# --- finalize_device_config ---


_FULL_CONFIG = {
    "chamber_probe": {"address": "28FFCDAB94160574", "pin": 18},
    "beer_probe": None,
    "cool": {"pin": 5, "invert": 1},
    "heat": {"pin": 6, "invert": 0},
}


async def test_finalize_device_config_installs_each_device_then_resets_exactly_once(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert len(conn.install_calls) == 3  # chamber_probe, cool, heat -- beer_probe is None
    assert conn.reset_calls == 1
    assert "installed" in job.result


async def test_finalize_device_config_still_resets_exactly_once_when_an_install_fails(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(raise_on_install_call=2)  # fails on the second install (cool)
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "failed"
    # The safety-net reset fired exactly once -- not zero, not twice.
    assert conn.reset_calls == 1


async def test_finalize_device_config_resets_exactly_once_when_cancelled(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    # Let the task actually start running -- install_device()'s own
    # asyncio.sleep(0) gives it a real suspension point, landing inside
    # the try block after at least one install but before the deliberate
    # reset. Cancelling before this point would never even enter the
    # runner's try/finally at all (a not-yet-started coroutine that's
    # cancelled never runs any of its own body).
    await asyncio.sleep(0)
    final = await tests_runtime.cancel_test(ctx, result["test_id"])

    assert final["state"] == "cancelled"
    # Exactly one reset -- from the safety net, since the deliberate
    # reset_and_reconnect() call (after all installs) was never reached.
    assert conn.reset_calls == 1
    assert conn.reset_calls == 1


async def test_finalize_device_config_is_blocked_while_a_fermentation_is_active(monkeypatch: pytest.MonkeyPatch):
    _patch_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    with pytest.raises(FermentationAlreadyActive):
        tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    assert conn.install_calls == []
    assert conn.reset_calls == 0


# --- reset_brewpi ---


async def test_reset_brewpi_completes_and_calls_reset(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "reset_brewpi", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["reset"] is True
    assert conn.reset_calls == 1


async def test_reset_brewpi_is_not_gated_by_active_fermentation(monkeypatch: pytest.MonkeyPatch):
    # Deliberate: this is the unconditional safety-net action for an
    # abandoned wizard -- gating it would defeat exactly the case it
    # exists for.
    _patch_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "reset_brewpi", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert conn.reset_calls == 1


# --- identify_onewire_probes ---


async def test_identify_onewire_probes_completes_immediately_with_zero_sensors(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _SequencedOneWireConnection(snapshots=[[]])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["identified_address"] is None
    assert job.result["readable"] == {}


async def test_identify_onewire_probes_confirms_a_single_readable_sensor(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    device = BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0)
    conn = _SequencedOneWireConnection(snapshots=[[device]])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {"window_s": 2.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["identified_address"] == "AAA"
    assert job.result["readable"] == {"AAA": True}


async def test_identify_onewire_probes_identifies_the_one_that_warms_up(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    baseline = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=61.0),
    ]
    warmed = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=90.0),  # +29F, well past the 3F delta
    ]
    conn = _SequencedOneWireConnection(snapshots=[baseline, warmed, warmed])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {"window_s": 10.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["identified_address"] == "BBB"


async def test_identify_onewire_probes_tracks_readable_false_when_a_sensor_goes_null_mid_poll(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    baseline = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=61.0),
    ]
    # BBB goes null (the confirmed-real "detected but unreadable" state) --
    # must never be reported as identified while null, regardless of what
    # its last real value was.
    bbb_null = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=None),
    ]
    conn = _SequencedOneWireConnection(snapshots=[baseline, bbb_null, bbb_null])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {"window_s": 6.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["identified_address"] is None
    assert job.result["readable"]["BBB"] is False


async def test_identify_onewire_probes_excludes_the_already_identified_chamber_address(monkeypatch: pytest.MonkeyPatch):
    # The second call (identifying beer) must never re-consider whatever
    # the first call already identified as chamber.
    _patch_no_active_fermentation(monkeypatch)
    device = BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0)
    conn = _SequencedOneWireConnection(snapshots=[[device]])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {"exclude_addresses": ["AAA"]})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["identified_address"] is None
    assert job.result["readable"] == {}


async def test_identify_onewire_probes_is_blocked_while_a_fermentation_is_active(monkeypatch: pytest.MonkeyPatch):
    _patch_active_fermentation(monkeypatch)
    conn = _SequencedOneWireConnection(snapshots=[[]])
    ctx = _FakeCtx(conn)

    with pytest.raises(FermentationAlreadyActive):
        tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {})


# --- brewpi_devices ---


async def test_brewpi_devices_returns_the_full_merged_list(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeBrewPiConnection(
        installed=[BrewPiDevice(slot=1, function=5, hardware=2, pin=18, address="AAA")],
        available=[BrewPiDevice(slot=-1, hardware=1, pin=2)],
    )
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "brewpi_devices", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert len(job.result["devices"]) == 2


# --- _pick_free_slot ---


def test_pick_free_slot_returns_the_lowest_unused_slot():
    installed = [BrewPiDevice(slot=1), BrewPiDevice(slot=3), BrewPiDevice(slot=0)]
    assert device_config._pick_free_slot(installed) == 2


def test_pick_free_slot_raises_when_all_16_slots_are_taken():
    installed = [BrewPiDevice(slot=i) for i in range(device_config.MAX_DEVICE_SLOT)]
    with pytest.raises(Exception):
        device_config._pick_free_slot(installed)
