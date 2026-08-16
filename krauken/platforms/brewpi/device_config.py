"""Krauken takes over BrewPi's own classic Device Configuration step
entirely -- mapping which OneWire probe is chamber/beer, and which pin
drives cooling/heating -- so a user never needs BrewPi's separate web UI.
Everything here operates one layer below the abstract ChamberDriver/
BeerTempSource Protocols (platforms/brewpi/live.py): those Protocols read/
write an ALREADY-configured device; this module is what does the
configuring, via the Arduino's own 'd'/'h'/'U'/'R' commands
(platforms/brewpi/connection.py).

These five test-job runners are registered under
platforms/registry.py's PLATFORM_BINDINGS["brewpi"].test_runners, NOT
hardcoded into daemon/tests_runtime.py -- installing a device, reading raw
firmware State integers, and pushing OneWire ROM addresses has no
equivalent in the ChamberDriver/BeerTempSource/GravitySource Protocols at
all (hardware CONFIGURATION, not hardware OPERATION), so there's no
existing abstraction to dispatch through the way confirm_heater/
identify_probes already do. daemon/tests_runtime.py's start_test() falls
back to PLATFORM_BINDINGS[platform].test_runners for exactly this reason.

Protocol facts baked into this module, all confirmed against the real
Arduino (firmware 0.2.13) and brewpi-remix/brewpi-firmware-rmx's actual
source, not guessed:

- Installing a device requires an EXPLICIT, currently-unused slot number
  (0-15) as "i" -- NOT -1, and NOT omitted. DeviceManager.cpp's
  parseDeviceDefinition() rejects both of those identically and silently
  (its very first check, `inRangeInt8(dev.id, 0, MAX_DEVICE_SLOT)`, fails
  before any real processing or response). Confirmed live: "i":0 installs
  cleanly, "f":0 uninstalls cleanly.
- The read-side "t" field is NOT part of DeviceManager.cpp's
  DeviceDefinition struct at all -- purely informational on reads, never
  sent on writes.
- Once a relay's actuator is installed, the firmware's own state machine
  (not this module) enforces anti-short-cycle protection; this module
  deliberately does NOT revert/uninstall a candidate mid-sweep (see
  _run_sweep_relay) -- a real, agreed design decision, not an oversight.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Mapping

from krauken.contracts.errors import ValidationError
from krauken.contracts.interfaces import PlatformTestRunner

# DeviceFunction (the "f" field) -- confirmed via BrewPiLess's wire-compatible
# EepromStructs.h plus real captured data from the reference rig.
DEVICE_FUNCTION_NONE = 0
DEVICE_FUNCTION_CHAMBER_DOOR = 1
DEVICE_FUNCTION_CHAMBER_HEAT = 2
DEVICE_FUNCTION_CHAMBER_COOL = 3
DEVICE_FUNCTION_CHAMBER_LIGHT = 4
DEVICE_FUNCTION_CHAMBER_TEMP = 5
DEVICE_FUNCTION_BEER_TEMP = 9

# DeviceHardware (the "h" field).
DEVICE_HARDWARE_NONE = 0
DEVICE_HARDWARE_PIN = 1
DEVICE_HARDWARE_ONEWIRE_TEMP = 2
DEVICE_HARDWARE_ONEWIRE_2413 = 3

_ONEWIRE_HARDWARE = frozenset({DEVICE_HARDWARE_ONEWIRE_TEMP, DEVICE_HARDWARE_ONEWIRE_2413})

# Confirmed via DeviceManager.h: `const device_slot_t MAX_DEVICE_SLOT = 16;`
# -- valid slots are 0-15 inclusive.
MAX_DEVICE_SLOT = 16

IDENTIFY_ONEWIRE_WINDOW_S = 90.0
IDENTIFY_ONEWIRE_POLL_S = 1.0
IDENTIFY_ONEWIRE_DELTA_F = 3.0

# Same shape as daemon/tests_runtime.py's CONFIRM_HEATER_FORCE_DELTA_F/
# CONFIRM_HEATER_WINDOW_S -- a real functional test, not a synthetic
# countdown. 600s matches MIN_SWITCH_TIME/MIN_COOL_OFF_TIME_FRIDGE_CONSTANT
# (both confirmed 600s in the real firmware's TempControl.h) -- the
# worst-case wait before EITHER relay may first engage in a fresh
# connection, since Krauken always uses fridge-constant mode.
SWEEP_FORCE_DELTA_F = 15.0
SWEEP_WINDOW_S = 600.0
SWEEP_POLL_S = 2.0

# Raw firmware State codes (TempControl.h's `enum states`) that mean "this
# relay is actually driven right now" for each function -- includes the
# MIN_TIME-held codes (8/9), not just the freshly-engaged ones (3/4),
# since both mean the relay is physically on. Deliberately narrower than
# connection.py's own STATES_HEATING/STATES_COOLING (which reference
# codes 13/14 not present in the confirmed firmware enum -- see the plan's
# "residual risks" section) -- this module uses only the directly-verified
# codes.
ENGAGED_STATES: Mapping[str, frozenset[int]] = {
    "cool": frozenset({4, 8}),
    "heat": frozenset({3, 9}),
}
WAITING_STATES = frozenset({5, 6})  # WAITING_TO_COOL, WAITING_TO_HEAT


@dataclass
class BrewPiDevice:
    """One entry from a 'd'/'h' device-list response, or one device about
    to be installed via 'U' -- the same shape either way. slot=-1 means
    "not installed" on a READ; it is never a valid value to WRITE (see
    module docstring)."""

    slot: int = -1
    category: int | None = None  # "t" -- read-side only, never written
    chamber: int = 1
    beer: int = 0
    function: int = DEVICE_FUNCTION_NONE
    hardware: int = DEVICE_HARDWARE_NONE
    deactivated: int = 0
    pin: int | None = None
    address: str | None = None
    calibration: float | None = None
    invert: int | None = None
    value: float | int | None = None  # "v" -- only present when read with r:1/v:1

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "BrewPiDevice":
        return cls(
            slot=int(raw.get("i", -1)),
            category=raw.get("t"),
            chamber=int(raw.get("c", 1)),
            beer=int(raw.get("b", 0)),
            function=int(raw.get("f", DEVICE_FUNCTION_NONE)),
            hardware=int(raw.get("h", DEVICE_HARDWARE_NONE)),
            deactivated=int(raw.get("d", 0)),
            pin=raw.get("p"),
            address=raw.get("a"),
            calibration=raw.get("j"),
            invert=raw.get("x"),
            value=raw.get("v"),
        )

    def to_wire_json(self) -> dict[str, Any]:
        """Only the fields DeviceManager's parseDeviceDefinition() actually
        reads (its DeviceDefinition struct: id/chamber/beer/deviceFunction/
        deviceHardware/pinNr/invert/pio/deactivate/calibrationAdjust/
        address) -- no "t" (confirmed not part of that struct at all) and
        no "v" (live telemetry, never echoed back on a write)."""
        body: dict[str, Any] = {
            "i": self.slot,
            "c": self.chamber,
            "b": self.beer,
            "f": self.function,
            "h": self.hardware,
            "d": self.deactivated,
        }
        if self.pin is not None:
            body["p"] = self.pin
        if self.address is not None:
            body["a"] = self.address
        if self.calibration is not None:
            body["j"] = self.calibration
        if self.invert is not None:
            body["x"] = self.invert
        return body

    def to_dict(self) -> dict[str, Any]:
        """Full shape including live 'v', for job-result JSON to the
        frontend -- distinct from to_wire_json(), which is write-only."""
        return {
            "slot": self.slot,
            "chamber": self.chamber,
            "beer": self.beer,
            "function": self.function,
            "hardware": self.hardware,
            "deactivated": self.deactivated,
            "pin": self.pin,
            "address": self.address,
            "calibration": self.calibration,
            "invert": self.invert,
            "value": self.value,
        }

    @property
    def is_onewire(self) -> bool:
        return self.hardware in _ONEWIRE_HARDWARE


def _pick_free_slot(installed: list[BrewPiDevice]) -> int:
    """The firmware does NOT auto-assign a slot on install (confirmed via
    DeviceManager.cpp -- see module docstring) -- the caller picks an
    explicit, currently-unused slot number. Lowest free integer in
    range(MAX_DEVICE_SLOT) not already claimed."""
    used = {d.slot for d in installed}
    for slot in range(MAX_DEVICE_SLOT):
        if slot not in used:
            return slot
    raise ValidationError("no free BrewPi device slots left (0-15 all in use)")


def _brewpi_connection(ctx: Any) -> Any | None:
    return ctx.registry.state_for("brewpi")


async def _run_brewpi_devices(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """One round of the full device list (installed + available, with live
    values) -- the ONLY way any caller should enumerate devices, so an
    already-installed device is never invisible to the wizard (the bug
    found this session: asking only 'h' or only 'd' misses half the
    picture)."""
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return
    devices = await conn.list_all_devices(with_values=True)
    job.state = "completed"
    job.result = {"devices": [d.to_dict() for d in devices]}


async def _read_onewire_values(conn: Any, exclude: set[str]) -> dict[str, float | None]:
    devices = await conn.list_all_devices(with_values=True)
    return {
        d.address: d.value
        for d in devices
        if d.is_onewire and d.address is not None and d.address not in exclude
    }


async def _run_identify_onewire_probes(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """Generalizes daemon/tests_runtime.py's _run_identify_probes delta
    logic to raw OneWire ROM addresses (before any role is assigned to
    them at all), with one addition: "readable" is tracked explicitly per
    address, since a detected-but-unreadable probe (v: null) is a real,
    confirmed, ongoing state on real hardware this session -- not a
    transient hiccup to silently retry into "reading..." forever.

    params:
      exclude_addresses: list[str] -- already-identified addresses to
        leave out (the second call, identifying beer, excludes whatever
        the first call identified as chamber).
      window_s: float, optional.
    """
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return

    exclude = set(params.get("exclude_addresses") or [])
    window_s = float(params.get("window_s", IDENTIFY_ONEWIRE_WINDOW_S))

    try:
        baseline = await _read_onewire_values(conn, exclude)
        current = dict(baseline)
        readable = {addr: (v is not None) for addr, v in current.items()}
        job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current), "readable": dict(readable)}

        if not baseline:
            job.state = "completed"
            job.result = {"identified_address": None, "baseline_f": {}, "current_f": {}, "readable": {}}
            return

        if len(baseline) == 1:
            # Nothing to compare against -- just confirm it responds, same
            # fast-path shape as the existing identify_probes.
            only_addr = next(iter(baseline))
            fast_window_s = min(window_s, 2.0)
            elapsed_s = 0.0
            while elapsed_s < fast_window_s:
                step_s = min(0.5, fast_window_s - elapsed_s)
                await ctx.clock.sleep(step_s)
                elapsed_s += step_s
                current = await _read_onewire_values(conn, exclude)
                readable = {addr: (v is not None) for addr, v in current.items()}
                job.result = {"identified_address": None, "baseline_f": dict(baseline), "current_f": dict(current), "readable": dict(readable)}
            job.state = "completed"
            job.result = {
                "identified_address": only_addr if current.get(only_addr) is not None else None,
                "baseline_f": baseline,
                "current_f": current,
                "readable": readable,
            }
            return

        identified: str | None = None
        elapsed_s = 0.0
        while elapsed_s < window_s:
            await ctx.clock.sleep(IDENTIFY_ONEWIRE_POLL_S)
            elapsed_s += IDENTIFY_ONEWIRE_POLL_S
            current = await _read_onewire_values(conn, exclude)
            readable = {addr: (v is not None) for addr, v in current.items()}
            for addr, b in baseline.items():
                c = current.get(addr)
                if b is not None and c is not None and (c - b) >= IDENTIFY_ONEWIRE_DELTA_F:
                    identified = addr
                    break
            job.result = {"identified_address": identified, "baseline_f": dict(baseline), "current_f": dict(current), "readable": dict(readable)}
            if identified is not None:
                break

        job.state = "completed"
        job.result = {"identified_address": identified, "baseline_f": baseline, "current_f": current, "readable": readable}
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise


async def _run_sweep_relay(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """Installs `candidate` as the cool/heat function (reusing its slot if
    it's already installed for that same pin+function, else picking a
    free one), forces a real setpoint, and polls the firmware's own raw
    State for the function's engaged codes -- confirmed the moment the
    firmware itself reports it, never assumed. Also reports the
    WAITING_TO_x codes explicitly (qualitative only -- the firmware never
    exposes a numeric remaining-wait-seconds value on the wire, confirmed
    via exhaustive search of PiLink.cpp) so the wizard can show "waiting on
    the compressor-protection timer" rather than a blank screen.

    Deliberately no finally-revert: leaving a candidate installed and
    possibly energized between sweep steps is the agreed design (the
    anti-short-cycle timers protect against RAPID CYCLING, not continuous
    running) -- do not add a release here.

    params: function: "cool"|"heat", candidate: raw device dict (pin,
    invert), window_s: float, optional.
    """
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return

    function = params.get("function")
    if function not in ENGAGED_STATES:
        job.state = "failed"
        job.error = f"invalid sweep function {function!r}"
        return
    candidate = params.get("candidate") or {}
    pin = candidate.get("pin")
    if pin is None:
        job.state = "failed"
        job.error = "sweep_relay requires a candidate with a pin"
        return
    window_s = float(params.get("window_s", SWEEP_WINDOW_S))
    device_function = DEVICE_FUNCTION_CHAMBER_COOL if function == "cool" else DEVICE_FUNCTION_CHAMBER_HEAT

    installed = await conn.list_all_devices(with_values=False)
    existing = next((d for d in installed if d.pin == pin and d.function == device_function), None)
    slot = existing.slot if existing is not None else _pick_free_slot(installed)

    device = BrewPiDevice(
        slot=slot,
        chamber=1,
        beer=0,
        function=device_function,
        hardware=DEVICE_HARDWARE_PIN,
        deactivated=0,
        pin=pin,
        invert=int(candidate.get("invert", 0)),
    )
    await conn.install_device(device)

    reading = await conn.read_temps()
    if reading is None or reading.fridge_temp_f is None:
        job.state = "failed"
        job.error = "chamber probe isn't reading -- can't run a live relay test"
        return
    baseline_f = reading.fridge_temp_f
    forced_target = baseline_f + SWEEP_FORCE_DELTA_F if function == "heat" else baseline_f - SWEEP_FORCE_DELTA_F
    await conn.set_fridge_target(forced_target)

    try:
        current_f = baseline_f
        outcome = "waiting"
        job.result = {"outcome": outcome, "baseline_f": baseline_f, "forced_target_f": forced_target, "current_f": current_f, "slot": slot}
        elapsed_s = 0.0
        while elapsed_s < window_s:
            await ctx.clock.sleep(SWEEP_POLL_S)
            elapsed_s += SWEEP_POLL_S
            reading = await conn.read_temps()
            if reading is not None:
                current_f = reading.fridge_temp_f
                if reading.state in ENGAGED_STATES[function]:
                    outcome = "engaged"
                elif reading.state in WAITING_STATES:
                    outcome = "waiting"
            job.result = {"outcome": outcome, "baseline_f": baseline_f, "forced_target_f": forced_target, "current_f": current_f, "slot": slot}
            if outcome == "engaged":
                break
        else:
            # while...else: only reached if the loop exhausted window_s
            # without ever breaking -- i.e. never engaged.
            outcome = "timeout"
            job.result = {"outcome": outcome, "baseline_f": baseline_f, "forced_target_f": forced_target, "current_f": current_f, "slot": slot}
        job.state = "completed"
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise


async def _run_finalize_device_config(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """Installs the complete final configuration in one pass, then always
    performs the deliberate reset -- a real chip reboot, not a soft idle,
    because DigitalPinActuator's constructor (confirmed via ActuatorPin.h)
    forces every reconstructed-from-EEPROM actuator to its correct "off"
    level before pinMode(OUTPUT), so this is the only mechanism that
    reliably clears any candidate pins orphaned energized during the
    cool/heat sweeps. The `finally` fires a second, safety-net reset ONLY
    if the deliberate one was never reached (an install raised, or the job
    was cancelled mid-loop) -- never both, never neither.

    params: config: {"chamber_probe": {"address","pin"},
    "beer_probe": {...}|None, "cool": {"pin","invert"},
    "heat": {...}|None}.
    """
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return

    config = params.get("config") or {}
    reset_done = False
    try:
        installed = await conn.list_all_devices(with_values=False)

        def _slot_for(pin: int | None, address: str | None, function: int) -> int:
            for d in installed:
                if function in (DEVICE_FUNCTION_CHAMBER_TEMP, DEVICE_FUNCTION_BEER_TEMP):
                    if d.address == address:
                        return d.slot
                elif d.pin == pin and d.function == function:
                    return d.slot
            return _pick_free_slot(installed)

        pushed: list[BrewPiDevice] = []

        chamber_probe = config.get("chamber_probe")
        if chamber_probe:
            slot = _slot_for(None, chamber_probe.get("address"), DEVICE_FUNCTION_CHAMBER_TEMP)
            dev = BrewPiDevice(
                slot=slot, chamber=1, beer=0, function=DEVICE_FUNCTION_CHAMBER_TEMP,
                hardware=DEVICE_HARDWARE_ONEWIRE_TEMP, pin=chamber_probe.get("pin"), address=chamber_probe.get("address"),
            )
            await conn.install_device(dev)
            pushed.append(dev)
            installed.append(dev)

        beer_probe = config.get("beer_probe")
        if beer_probe:
            slot = _slot_for(None, beer_probe.get("address"), DEVICE_FUNCTION_BEER_TEMP)
            dev = BrewPiDevice(
                slot=slot, chamber=1, beer=1, function=DEVICE_FUNCTION_BEER_TEMP,
                hardware=DEVICE_HARDWARE_ONEWIRE_TEMP, pin=beer_probe.get("pin"), address=beer_probe.get("address"),
            )
            await conn.install_device(dev)
            pushed.append(dev)
            installed.append(dev)

        cool = config.get("cool")
        if cool:
            slot = _slot_for(cool.get("pin"), None, DEVICE_FUNCTION_CHAMBER_COOL)
            dev = BrewPiDevice(
                slot=slot, chamber=1, beer=0, function=DEVICE_FUNCTION_CHAMBER_COOL,
                hardware=DEVICE_HARDWARE_PIN, pin=cool.get("pin"), invert=int(cool.get("invert", 0)),
            )
            await conn.install_device(dev)
            pushed.append(dev)
            installed.append(dev)

        heat = config.get("heat")
        if heat:
            slot = _slot_for(heat.get("pin"), None, DEVICE_FUNCTION_CHAMBER_HEAT)
            dev = BrewPiDevice(
                slot=slot, chamber=1, beer=0, function=DEVICE_FUNCTION_CHAMBER_HEAT,
                hardware=DEVICE_HARDWARE_PIN, pin=heat.get("pin"), invert=int(heat.get("invert", 0)),
            )
            await conn.install_device(dev)
            pushed.append(dev)
            installed.append(dev)

        await conn.reset_and_reconnect()
        reset_done = True

        final_installed = await conn.list_installed_devices(with_values=False)
        job.state = "completed"
        job.result = {"pushed": [d.to_dict() for d in pushed], "installed": [d.to_dict() for d in final_installed]}
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001 -- report failure; still guaranteed-reset via finally below
        job.state = "failed"
        job.error = str(e)
    finally:
        if not reset_done:
            with contextlib.suppress(Exception):
                await conn.reset_and_reconnect()


async def _run_reset_brewpi(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """Best-effort, always completes -- registered below with
    requires_no_active_fermentation=False, since this is the unconditional
    safety-net action the wizard's cancel path fires for an abandoned
    sweep; gating it would defeat exactly the case it exists for."""
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return
    try:
        await conn.reset_and_reconnect()
        job.state = "completed"
        job.result = {"reset": True}
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001 -- best-effort; always reports completed, never failed
        job.state = "completed"
        job.result = {"reset": False, "error": str(e)}


TEST_RUNNERS: Mapping[str, PlatformTestRunner] = {
    # Read-only listing -- no hardware mutation, so no fermentation gate.
    "brewpi_devices": PlatformTestRunner(run=_run_brewpi_devices),
    # These three reconfigure real hardware (or force a real setpoint) --
    # blocked while a fermentation is active, checked synchronously by
    # daemon/tests_runtime.py's start_test() before the task is created
    # (see PlatformTestRunner's own docstring for why it can't live here).
    "identify_onewire_probes": PlatformTestRunner(run=_run_identify_onewire_probes, requires_no_active_fermentation=True),
    "sweep_relay": PlatformTestRunner(run=_run_sweep_relay, requires_no_active_fermentation=True),
    "finalize_device_config": PlatformTestRunner(run=_run_finalize_device_config, requires_no_active_fermentation=True),
    # The unconditional safety-net action -- deliberately NOT gated (see
    # _run_reset_brewpi's own docstring).
    "reset_brewpi": PlatformTestRunner(run=_run_reset_brewpi),
}
