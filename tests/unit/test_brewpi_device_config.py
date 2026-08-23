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
    fail_first_n_reads: int = 0  # read_temps() returns None for this many calls before behaving normally
    stale_reads_before_correct: int = 0  # list_installed_devices() returns [] for this many calls first -- simulates a reset-verification miss (a just-pushed device not showing up correctly yet)
    call_order: list[str] = field(default_factory=list)  # e.g. "reset", "install:5", "uninstall:5" -- for ordering assertions
    _state_idx: int = 0
    _read_calls: int = 0
    _list_installed_calls: int = 0

    async def list_installed_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        self._list_installed_calls += 1
        if self._list_installed_calls <= self.stale_reads_before_correct:
            return []
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
        tag = "uninstall" if device.function == device_config.DEVICE_FUNCTION_NONE else "install"
        self.call_order.append(f"{tag}:{device.slot}")
        if self.raise_on_install_call is not None and len(self.install_calls) == self.raise_on_install_call:
            raise RuntimeError("simulated install failure")
        self.installed = [d for d in self.installed if d.slot != device.slot] + [device]
        self.available = [d for d in self.available if d.pin != device.pin or d.address != device.address]

    async def reset_and_reconnect(self) -> bool:
        self.reset_calls += 1
        self.call_order.append("reset")
        return True

    async def read_temps(self) -> BrewPiReading | None:
        self._read_calls += 1
        if self._read_calls <= self.fail_first_n_reads:
            return None  # simulates the confirmed-live transient missed read (gap 4)
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


@pytest.fixture(autouse=True)
def _isolated_baseline_path(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Every test in this file runs against a tmp_path-based baseline
    snapshot file, never the real /var/lib/krauken path -- begin_device_config/
    reset_brewpi/finalize_device_config all read or write it."""
    monkeypatch.setattr(device_config, "BASELINE_SNAPSHOT_PATH", tmp_path / "brewpi-wizard-baseline.json")


# --- identify_relay_pin ---


async def test_identify_relay_pin_rejects_a_reserved_pin_before_touching_the_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    # Confirmed live 2026-08-22: pin 2 is permanently off-limits on this
    # Arduino Uno-based shield (see RESERVED_RELAY_PINS's own comment).
    # Must fail before making any hardware calls at all -- not even
    # set_fridge_target(None) -- since there's nothing to gain from
    # forcing the chamber off just to reject the candidate anyway.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 2, "invert": 0}})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "failed"
    assert "pin 2" in job.error
    assert "reserved" in job.error
    assert conn.install_calls == []
    assert conn.set_target_calls == []


async def test_identify_relay_pin_forces_the_chamber_off_and_installs_the_candidate(monkeypatch: pytest.MonkeyPatch):
    # Redesigned 2026-08-18 (see plans/jiggly-bubbling-popcorn.md): no more
    # forced heat demand / State polling -- just force the chamber off
    # (set_fridge_target(None) -> j{mode:"o"}) and install the candidate.
    # There's no `outcome` for software to conclude any more; the human
    # observer (via the frontend) decides what happened next.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 6, "invert": 1}})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result == {"pin": 6, "invert": 1, "slot": device_config.RELAY_IDENTIFY_SLOT}
    assert conn.set_target_calls[-1] is None  # forced off, never a raised target
    assert len(conn.install_calls) == 1
    assert conn.install_calls[0].pin == 6
    assert conn.install_calls[0].invert == 1
    assert conn.install_calls[0].function == device_config.DEVICE_FUNCTION_CHAMBER_HEAT


async def test_identify_relay_pin_never_leaves_two_simultaneous_chamber_heat_devices(monkeypatch: pytest.MonkeyPatch):
    # The key invariant, unchanged by the redesign: at most one device is
    # ever installed with the CHAMBER_HEAT function at a time -- BrewPi
    # does not enforce one-actuator-per-function on its own, confirmed live
    # this session.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result1 = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 2, "invert": 0}})
    await ctx.jobs[result1["test_id"]]._task

    result2 = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 6, "invert": 0}})
    await ctx.jobs[result2["test_id"]]._task

    heat_devices = [d for d in conn.installed if d.function == device_config.DEVICE_FUNCTION_CHAMBER_HEAT]
    assert len(heat_devices) == 1
    assert heat_devices[0].pin == 6


async def test_identify_relay_pin_reuses_the_reserved_scratch_slot(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 5, "invert": 0}})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert conn.install_calls[0].slot == device_config.RELAY_IDENTIFY_SLOT


async def test_identify_relay_pin_is_blocked_while_a_fermentation_is_active(monkeypatch: pytest.MonkeyPatch):
    _patch_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    with pytest.raises(FermentationAlreadyActive):
        tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 2}})
    assert conn.install_calls == []


async def test_identify_relay_pin_corrects_a_resolved_pin_before_uninstalling_it(monkeypatch: pytest.MonkeyPatch):
    # Confirmed live 2026-08-18: bare-uninstalling a pin that's currently
    # sitting at the WRONG (energized) invert leaves it stuck on forever.
    # `resolved` is how the wizard tells this action which currently-
    # installed pin is at a wrong invert and what its confirmed-safe one
    # is -- the human already knows this (they just watched it react), the
    # firmware's own State can no longer tell the two apart under the
    # off-based design.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)
    result1 = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 5, "invert": 1}})
    await ctx.jobs[result1["test_id"]]._task

    # Pin 5 is currently installed at invert 1, but that's the WRONG one
    # (it's what just reacted) -- its confirmed-safe invert is 0, passed
    # via resolved on the next call, which swaps to pin 6.
    result2 = tests_runtime.start_test(
        ctx, DEVICE_ID, "identify_relay_pin",
        {"candidate": {"pin": 6, "invert": 0}, "resolved": [{"pin": 5, "safe_invert": 0}]},
    )
    await ctx.jobs[result2["test_id"]]._task

    calls = conn.install_calls
    assert len(calls) == 4
    original, corrected, uninstall, new_candidate = calls
    assert original.pin == 5 and original.invert == 1
    # The correction: same slot/pin, invert flipped to the confirmed-safe one.
    assert corrected.slot == original.slot
    assert corrected.pin == 5
    assert corrected.invert == 0
    assert corrected.function == device_config.DEVICE_FUNCTION_CHAMBER_HEAT
    # Then a real uninstall, only after the correction.
    assert uninstall.slot == original.slot
    assert uninstall.function == device_config.DEVICE_FUNCTION_NONE
    # Then, and only then, the new candidate.
    assert new_candidate.pin == 6
    assert new_candidate.function == device_config.DEVICE_FUNCTION_CHAMBER_HEAT


async def test_identify_relay_pin_bare_uninstalls_a_pin_not_yet_resolved(monkeypatch: pytest.MonkeyPatch):
    # A pin the wizard hasn't resolved yet -- still mid-sweep, or
    # genuinely nothing wired there -- gets a bare uninstall with no
    # correction step; there's nothing confirmed to protect.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)
    result1 = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 5, "invert": 1}})
    await ctx.jobs[result1["test_id"]]._task

    # Swap to pin 6 -- pin 5 was never resolved (no `resolved` passed), so
    # no correction install should occur.
    result2 = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 6, "invert": 0}})
    await ctx.jobs[result2["test_id"]]._task

    calls = conn.install_calls
    assert len(calls) == 3  # original pin5, bare uninstall, new candidate pin6 -- no correction install
    original, uninstall, new_candidate = calls
    assert original.pin == 5
    assert uninstall.function == device_config.DEVICE_FUNCTION_NONE
    assert new_candidate.pin == 6


async def test_identify_relay_pin_skips_the_correction_when_already_at_the_safe_invert(monkeypatch: pytest.MonkeyPatch):
    # If the currently-installed invert already matches the resolved
    # safe_invert, there's nothing to correct -- straight to bare uninstall.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)
    result1 = tests_runtime.start_test(ctx, DEVICE_ID, "identify_relay_pin", {"candidate": {"pin": 5, "invert": 0}})
    await ctx.jobs[result1["test_id"]]._task

    result2 = tests_runtime.start_test(
        ctx, DEVICE_ID, "identify_relay_pin",
        {"candidate": {"pin": 6, "invert": 0}, "resolved": [{"pin": 5, "safe_invert": 0}]},
    )
    await ctx.jobs[result2["test_id"]]._task

    calls = conn.install_calls
    assert len(calls) == 3  # no correction install -- already safe
    original, uninstall, new_candidate = calls
    assert uninstall.function == device_config.DEVICE_FUNCTION_NONE
    assert new_candidate.pin == 6


# --- _abandon_heat ---


async def test_abandon_heat_corrects_invert_before_uninstalling_when_resolved_and_wrong():
    conn = _FakeBrewPiConnection()
    device = BrewPiDevice(
        slot=15, chamber=1, beer=0, function=device_config.DEVICE_FUNCTION_CHAMBER_HEAT,
        hardware=device_config.DEVICE_HARDWARE_PIN, pin=5, invert=1,
    )

    await device_config._abandon_heat(conn, device, {5: 0})

    assert len(conn.install_calls) == 2
    corrected, uninstall = conn.install_calls
    assert corrected.slot == 15
    assert corrected.pin == 5
    assert corrected.invert == 0
    assert corrected.function == device_config.DEVICE_FUNCTION_CHAMBER_HEAT
    assert uninstall.slot == 15
    assert uninstall.function == device_config.DEVICE_FUNCTION_NONE


async def test_abandon_heat_bare_uninstalls_when_pin_not_in_resolved():
    conn = _FakeBrewPiConnection()
    device = BrewPiDevice(
        slot=15, chamber=1, beer=0, function=device_config.DEVICE_FUNCTION_CHAMBER_HEAT,
        hardware=device_config.DEVICE_HARDWARE_PIN, pin=2, invert=0,
    )

    await device_config._abandon_heat(conn, device, {})

    assert len(conn.install_calls) == 1
    assert conn.install_calls[0].function == device_config.DEVICE_FUNCTION_NONE


async def test_abandon_heat_bare_uninstalls_when_already_at_the_resolved_safe_invert():
    conn = _FakeBrewPiConnection()
    device = BrewPiDevice(
        slot=15, chamber=1, beer=0, function=device_config.DEVICE_FUNCTION_CHAMBER_HEAT,
        hardware=device_config.DEVICE_HARDWARE_PIN, pin=6, invert=1,
    )

    await device_config._abandon_heat(conn, device, {6: 1})

    assert len(conn.install_calls) == 1  # no correction -- already safe
    assert conn.install_calls[0].function == device_config.DEVICE_FUNCTION_NONE


# --- install_probe ---


async def test_install_probe_installs_the_requested_role(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "install_probe", {"role": "chamber", "address": "AAA", "pin": 18})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert len(conn.install_calls) == 1
    assert conn.install_calls[0].function == device_config.DEVICE_FUNCTION_CHAMBER_TEMP
    assert conn.install_calls[0].address == "AAA"


async def test_install_probe_reuses_the_existing_slot_for_the_same_function(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(
        installed=[BrewPiDevice(slot=4, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="OLD")],
    )
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "install_probe", {"role": "chamber", "address": "NEW", "pin": 18})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert conn.install_calls[0].slot == 4
    assert conn.install_calls[0].address == "NEW"


async def test_install_probe_is_blocked_while_a_fermentation_is_active(monkeypatch: pytest.MonkeyPatch):
    _patch_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    with pytest.raises(FermentationAlreadyActive):
        tests_runtime.start_test(ctx, DEVICE_ID, "install_probe", {"role": "chamber", "address": "AAA"})
    assert conn.install_calls == []


# --- begin_device_config ---


async def test_begin_device_config_snapshots_and_wipes_when_no_prior_baseline(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeBrewPiConnection(
        installed=[
            BrewPiDevice(slot=0, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="AAA"),
            BrewPiDevice(slot=1, function=device_config.DEVICE_FUNCTION_CHAMBER_COOL, hardware=1, pin=5, invert=1),
        ],
    )
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "begin_device_config", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert sorted(job.result["wiped"]) == [0, 1]
    assert all(d.function == device_config.DEVICE_FUNCTION_NONE for d in conn.installed if d.slot in (0, 1))
    saved = device_config._read_baseline()
    assert saved is not None
    assert {d.slot for d in saved} == {0, 1}


async def test_begin_device_config_self_heals_a_prior_incomplete_session_first(monkeypatch: pytest.MonkeyPatch):
    # Simulate an abandoned prior session: a leftover baseline snapshot
    # naming a device that isn't currently installed at all (the previous
    # wizard run wiped it and never reached finalize or cancel).
    leftover = [BrewPiDevice(slot=0, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="AAA")]
    device_config._write_baseline(leftover)
    conn = _FakeBrewPiConnection(installed=[])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "begin_device_config", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    # The leftover device was reinstalled (self-heal) before the fresh
    # snapshot/wipe -- confirmed via the reset that follows a restore.
    assert conn.reset_calls == 1
    reinstalls = [c for c in conn.install_calls if c.slot == 0 and c.function == device_config.DEVICE_FUNCTION_CHAMBER_TEMP]
    assert len(reinstalls) == 1
    # The new snapshot reflects the just-restored (then wiped) state.
    final_snapshot = device_config._read_baseline()
    assert final_snapshot is not None
    assert {d.slot for d in final_snapshot} == {0}


async def test_restore_baseline_resets_before_reinstalling_not_after(monkeypatch: pytest.MonkeyPatch):
    # Gap 3, confirmed live 2026-08-18: the original install-then-reset
    # order leaves any currently-engaged stray device energized throughout
    # the whole reinstall step. The reset must come first.
    leftover = [BrewPiDevice(slot=0, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="AAA")]
    device_config._write_baseline(leftover)
    conn = _FakeBrewPiConnection(installed=[])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "begin_device_config", {})
    await ctx.jobs[result["test_id"]]._task

    assert "reset" in conn.call_order
    assert "install:0" in conn.call_order
    assert conn.call_order.index("reset") < conn.call_order.index("install:0")


async def test_restore_baseline_uninstalls_a_stray_device_not_in_the_snapshot(monkeypatch: pytest.MonkeyPatch):
    # Gap 3, confirmed live 2026-08-18: reinstalling only the snapshot's
    # own devices was never enough -- a stray device left installed
    # outside the snapshot (e.g. the sweep's own scratch-slot candidate)
    # survived a restore completely untouched, leaving two conflicting
    # device definitions possibly bound to the same physical pin.
    leftover_snapshot = [BrewPiDevice(slot=0, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="AAA")]
    device_config._write_baseline(leftover_snapshot)
    stray = BrewPiDevice(slot=15, function=device_config.DEVICE_FUNCTION_CHAMBER_HEAT, hardware=1, pin=6, invert=1)
    conn = _FakeBrewPiConnection(installed=[stray])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "begin_device_config", {})
    await ctx.jobs[result["test_id"]]._task

    assert "uninstall:15" in conn.call_order  # the stray device got uninstalled during the restore/reconcile step
    assert "install:0" in conn.call_order  # the baseline's own device still got (re)installed


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


async def test_finalize_device_config_reuses_a_free_slot_for_a_probe_only_ever_seen_available(
    monkeypatch: pytest.MonkeyPatch,
):
    # Confirmed live 2026-08-22: _slot_for() matched an AVAILABLE (not
    # yet installed) entry by address -- which always reports slot -1,
    # since list_all_devices() is installed+available combined -- and
    # sent "i":-1 to the Arduino for the beer probe. Silently rejected,
    # no echo, never actually installed. The ordinary case for every
    # probe finalize ever installs is exactly this: it was only ever
    # seen in the available list before now.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(
        available=[
            BrewPiDevice(slot=-1, function=device_config.DEVICE_FUNCTION_NONE, hardware=2, pin=18, address="BEER-ADDR"),
        ],
    )
    ctx = _FakeCtx(conn)
    config = {**_FULL_CONFIG, "beer_probe": {"address": "BEER-ADDR", "pin": 18}}

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": config})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    beer_install = next(d for d in conn.install_calls if d.function == device_config.DEVICE_FUNCTION_BEER_TEMP)
    assert beer_install.slot >= 0


async def test_finalize_device_config_retries_verification_before_resetting(monkeypatch: pytest.MonkeyPatch):
    # Confirmed live 2026-08-22: sending R too soon after the last
    # install risked resetting before an EEPROM write had actually
    # committed. A transient miss on the first couple of verification
    # reads (simulated here) must not fail the job -- just delay the
    # reset until the pushed devices actually verify.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(stale_reads_before_correct=2)
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert conn.reset_calls == 1
    assert conn._list_installed_calls >= 3  # 2 misses + the one that finally verified


async def test_finalize_device_config_still_resets_when_verification_never_succeeds(monkeypatch: pytest.MonkeyPatch):
    # Best-effort past the retry budget: this function's own guarantee is
    # "always reset exactly once" regardless -- a persistent verification
    # mismatch is logged, not fatal, and must never skip or hang the
    # reset it exists specifically to always perform.
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(stale_reads_before_correct=999)
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert conn.reset_calls == 1
    # The full retry budget, plus the one extra list_installed_devices()
    # call this function always makes after resetting to report the final
    # device table in job.result -- exhausting the budget must not skip
    # that either.
    assert conn._list_installed_calls == device_config.FINALIZE_VERIFY_RETRIES + 1


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


async def test_finalize_device_config_clears_the_baseline_snapshot_on_success(monkeypatch: pytest.MonkeyPatch):
    device_config._write_baseline(
        [BrewPiDevice(slot=0, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="AAA")],
    )
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    # The wizard's new configuration is the new reality -- nothing left to
    # revert to.
    assert device_config._read_baseline() is None


async def test_finalize_device_config_keeps_the_baseline_snapshot_when_an_install_fails(monkeypatch: pytest.MonkeyPatch):
    device_config._write_baseline(
        [BrewPiDevice(slot=0, function=device_config.DEVICE_FUNCTION_CHAMBER_TEMP, hardware=2, pin=18, address="AAA")],
    )
    _patch_no_active_fermentation(monkeypatch)
    conn = _FakeBrewPiConnection(raise_on_install_call=2)
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "finalize_device_config", {"config": _FULL_CONFIG})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "failed"
    # A failed finalize must still be recoverable via reset_brewpi or the
    # next begin_device_config self-heal -- never silently lose the
    # pre-wizard state on top of failing.
    assert device_config._read_baseline() is not None


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
    assert conn.install_calls == []  # no baseline to restore -- plain reset


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


async def test_reset_brewpi_restores_baseline_and_clears_the_file_when_one_exists(monkeypatch: pytest.MonkeyPatch):
    device_config._write_baseline(
        [BrewPiDevice(slot=1, function=device_config.DEVICE_FUNCTION_CHAMBER_COOL, hardware=1, pin=5, invert=1)],
    )
    conn = _FakeBrewPiConnection()
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "reset_brewpi", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert len(conn.install_calls) == 1
    assert conn.install_calls[0].slot == 1
    assert conn.reset_calls == 1
    # A real "revert to the beginning state" -- not just a bare reset.
    assert device_config._read_baseline() is None


# --- identify_onewire_probes ---


async def test_identify_onewire_probes_completes_immediately_with_zero_sensors(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    conn = _SequencedOneWireConnection(snapshots=[[]])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["readable"] == {}


async def test_identify_onewire_probes_surfaces_a_single_readable_sensor(monkeypatch: pytest.MonkeyPatch):
    # No identification logic lives here any more (see the function's own
    # docstring) -- this just confirms a lone candidate's reading is
    # surfaced correctly. Which candidate (if any) counts as "selected" is
    # entirely HardwareWizard.tsx's call.
    _patch_no_active_fermentation(monkeypatch)
    device = BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0)
    conn = _SequencedOneWireConnection(snapshots=[[device]])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {"window_s": 2.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["baseline_f"] == {"AAA": 60.0}
    assert job.result["current_f"] == {"AAA": 60.0}
    assert job.result["readable"] == {"AAA": True}


async def test_identify_onewire_probes_keeps_current_f_updated_across_polls(monkeypatch: pytest.MonkeyPatch):
    # The frontend does its own delta-vs-baseline_f comparison against
    # whatever current_f reads on each poll -- this just confirms current_f
    # actually tracks a later reading rather than freezing at the first one.
    _patch_no_active_fermentation(monkeypatch)
    baseline = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=61.0),
    ]
    warmed = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=90.0),
    ]
    conn = _SequencedOneWireConnection(snapshots=[baseline, warmed, warmed])
    ctx = _FakeCtx(conn)

    result = tests_runtime.start_test(ctx, DEVICE_ID, "identify_onewire_probes", {"window_s": 2.0})
    job = ctx.jobs[result["test_id"]]
    await job._task

    assert job.state == "completed"
    assert job.result["baseline_f"] == {"AAA": 60.0, "BBB": 61.0}
    assert job.result["current_f"] == {"AAA": 60.0, "BBB": 90.0}


async def test_identify_onewire_probes_tracks_readable_false_when_a_sensor_goes_null_mid_poll(monkeypatch: pytest.MonkeyPatch):
    _patch_no_active_fermentation(monkeypatch)
    baseline = [
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="AAA", value=60.0),
        BrewPiDevice(hardware=device_config.DEVICE_HARDWARE_ONEWIRE_TEMP, address="BBB", value=61.0),
    ]
    # BBB goes null -- a confirmed-real "detected but unreadable" state on
    # real hardware this session, not a transient hiccup.
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
    assert job.result["readable"]["BBB"] is False
    assert job.result["current_f"]["BBB"] is None


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
