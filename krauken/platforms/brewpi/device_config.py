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
  (not this module) enforces anti-short-cycle protection.
- BrewPi does **not** enforce one-actuator-per-function on its own --
  confirmed live this session: leaving several same-function candidates
  installed at once (from an earlier design that deliberately never
  reverted a tested candidate) got them all driven together the instant
  that function was commanded, not just the intended one. The corrected
  invariant, load-bearing throughout `_run_identify_relay_pin` below: at
  most one device is ever installed with the `CHAMBER_HEAT` function at a
  time -- uninstall whatever currently holds it before installing the
  next candidate.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
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

IDENTIFY_ONEWIRE_POLL_S = 1.0
IDENTIFY_ONEWIRE_DELTA_F = 3.0
# No user-facing timeout here -- probe identification should stay up until
# the user warms a probe or explicitly cancels setup, not give up on a
# fixed clock (there's no hardware reason it needs one, unlike the relay
# sweep's real anti-short-cycle window below). This backstop exists only
# against a truly orphaned server-side task if a session is abandoned
# mid-poll without ever calling cancel -- never expected to be hit in
# normal use, and never surfaced to the user as a "try again" wall.
IDENTIFY_ONEWIRE_BACKSTOP_S = 3600.0

# Reserved scratch slot for relay pin-identification tests only -- reused
# across every candidate rather than picking a fresh free slot each time,
# so a long sweep doesn't churn through slots. Falls back to a freshly
# picked free slot in the (very unlikely) case something unrelated already
# occupies it.
RELAY_IDENTIFY_SLOT = 15
# Matches MIN_SWITCH_TIME/MIN_COOL_OFF_TIME_FRIDGE_CONSTANT (both confirmed
# 600s in the real firmware's TempControl.h) -- the worst-case wait before
# a relay may first engage in a fresh connection, since Krauken always
# uses fridge-constant mode.
RELAY_IDENTIFY_WINDOW_S = 600.0
RELAY_IDENTIFY_POLL_S = 2.0
# ALWAYS added to the baseline chamber temp, never subtracted -- this
# module must never command a target below the current chamber temp (a
# cooling demand). Forcing "heat" is the sole, universal trigger used to
# energize a candidate pin during discovery; whatever is actually wired
# there responds (a real heater, or the fridge if that's what's on this
# pin), and the human observer -- not the firmware -- says which.
RELAY_IDENTIFY_FORCE_DELTA_F = 15.0

# Raw firmware State codes (TempControl.h's `enum states`) that mean "the
# heat function is actually driven right now" -- includes the
# MIN_TIME-held code (9), not just the freshly-engaged one (3), since both
# mean the relay is physically on. There is no cool-side equivalent here:
# this module never commands a cooling demand at all (see
# RELAY_IDENTIFY_FORCE_DELTA_F above). Deliberately narrower than
# connection.py's own STATES_HEATING/STATES_COOLING (which reference codes
# 13/14 not present in the confirmed firmware enum -- see the plan's
# "residual risks" section) -- this module uses only the directly-verified
# codes.
HEAT_ENGAGED_STATES = frozenset({3, 9})
HEAT_WAITING_STATES = frozenset({6})  # WAITING_TO_HEAT


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


async def _uninstall(conn: Any, slot: int) -> None:
    """`U{"i":<slot>,"f":0,...}` -- confirmed live this session as the
    working uninstall shape. Used everywhere this module needs to clear a
    slot: the relay pin-identification invariant (at most one CHAMBER_HEAT
    device live at a time), and begin_device_config's wipe-to-unassigned."""
    await conn.install_device(BrewPiDevice(slot=slot, function=DEVICE_FUNCTION_NONE))


# --- Baseline snapshot: capture-at-start, restore-on-cancel-or-self-heal ---
#
# Persisted to disk (survives a daemon restart, matching db_path's own
# convention in krauken.conf) so an abandoned wizard session -- tab closed,
# network dropped, no explicit cancel -- can be healed automatically the
# *next* time anyone opens the wizard, without any timeout/watchdog
# machinery: begin_device_config's first action is always "restore a
# leftover snapshot if one exists" before capturing its own fresh one.
BASELINE_SNAPSHOT_PATH = Path("/var/lib/krauken/brewpi-wizard-baseline.json")


def _read_baseline() -> list[BrewPiDevice] | None:
    try:
        raw = json.loads(BASELINE_SNAPSHOT_PATH.read_text())
    except FileNotFoundError:
        return None
    return [BrewPiDevice.from_json(d) for d in raw]


def _write_baseline(devices: list[BrewPiDevice]) -> None:
    BASELINE_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_SNAPSHOT_PATH.write_text(json.dumps([d.to_wire_json() for d in devices]))


def _clear_baseline() -> None:
    with contextlib.suppress(FileNotFoundError):
        BASELINE_SNAPSHOT_PATH.unlink()


async def _restore_baseline(conn: Any) -> None:
    """Reinstall every device from the snapshot file (if any), then reset --
    a reset (not a soft idle) is the right unconditional follow-up here,
    same reasoning as finalize_device_config's own: a freshly-pushed
    config must never be interpreted by stale in-RAM actuator objects.
    Used by both begin_device_config's self-heal step and reset_brewpi's
    cancel-path restore -- same operation, two callers. No-op if there's
    no snapshot to restore."""
    baseline = _read_baseline()
    if baseline is None:
        return
    for device in baseline:
        await conn.install_device(device)
    await conn.reset_and_reconnect()


def _brewpi_connection(ctx: Any) -> Any | None:
    return ctx.registry.state_for("brewpi")


async def _run_begin_device_config(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """The wizard's very first action. Three steps, in order: (1) self-heal
    -- if a baseline snapshot file already exists (a previous wizard
    session never reached finalize or cancel), restore it first, so an
    abandoned session's mess is cleaned up automatically before this new
    one starts, without any timeout/watchdog machinery. (2) Snapshot the
    now-guaranteed-clean installed-device state as the new baseline. (3)
    Uninstall every device in that snapshot, so every pin and probe the
    wizard subsequently touches starts genuinely unassigned -- this is
    what removes any ambiguity about which physical pin a given sweep
    step just turned on. Does NOT gate on fermentation state -- like
    reset_brewpi, this is session bootstrap, not a hardware test."""
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return
    try:
        await _restore_baseline(conn)
        snapshot = await conn.list_installed_devices(with_values=False)
        _write_baseline(snapshot)
        for device in snapshot:
            await _uninstall(conn, device.slot)
        job.state = "completed"
        job.result = {"wiped": [d.slot for d in snapshot]}
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise


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
      window_s: float, optional -- defaults to IDENTIFY_ONEWIRE_BACKSTOP_S,
        a very long orphaned-task backstop, never a user-facing timeout.
    """
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return

    exclude = set(params.get("exclude_addresses") or [])
    window_s = float(params.get("window_s", IDENTIFY_ONEWIRE_BACKSTOP_S))

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


async def _run_install_probe(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """Installs a single already-identified OneWire probe as CHAMBER_TEMP
    or BEER_TEMP immediately -- called from deviceProbeId's chamber/beer
    confirmation steps, not deferred to finalize_device_config. Fixes a
    real sequencing bug found this session: identify_relay_pin needs a
    live FridgeTemp reading to compute a baseline, but nothing was ever
    installed with the CHAMBER_TEMP function until finalize -- the very
    last wizard step, which runs *after* the relay sweep -- so every sweep
    attempt failed immediately regardless of what was actually wired. No
    reset here -- installing a temp sensor takes effect immediately,
    unlike a relay pin reassignment (which the wizard reasons about via
    RELAY_IDENTIFY_SLOT reuse, not a chip reboot).

    params: role: "chamber"|"beer", address: str, pin: int, optional.
    """
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return

    role = params.get("role")
    address = params.get("address")
    if role not in ("chamber", "beer") or not address:
        job.state = "failed"
        job.error = "install_probe requires role ('chamber'|'beer') and address"
        return
    function = DEVICE_FUNCTION_CHAMBER_TEMP if role == "chamber" else DEVICE_FUNCTION_BEER_TEMP

    installed = await conn.list_all_devices(with_values=False)
    existing = next((d for d in installed if d.function == function), None)
    slot = existing.slot if existing is not None else _pick_free_slot(installed)

    device = BrewPiDevice(
        slot=slot,
        chamber=1,
        beer=1 if role == "beer" else 0,
        function=function,
        hardware=DEVICE_HARDWARE_ONEWIRE_TEMP,
        pin=params.get("pin"),
        address=address,
    )
    await conn.install_device(device)
    job.state = "completed"
    job.result = {"installed": device.to_dict()}


async def _run_identify_relay_pin(ctx: Any, job: Any, device_id: str, params: dict[str, Any]) -> None:
    """Tests exactly one (pin, polarity) combination by forcing a heat
    demand and watching the firmware's own raw State -- never which
    physical thing responds. That's for the human observer to say: forcing
    "heat" and assigning `candidate` to CHAMBER_HEAT just tells the
    firmware "turn this pin on"; if what's actually wired there is the
    fridge relay, the fridge is what turns on. `outcome == "engaged"` means
    only "the firmware just switched this pin on" -- the frontend asks the
    human what physically happened next.

    Invariant, load-bearing (see module docstring): before installing the
    requested candidate, always uninstalls whatever currently holds the
    CHAMBER_HEAT function, so at most one device ever shares that function
    at a time. This is what replaced the old sweep_relay's "leave a
    candidate energized between steps" design -- confirmed live this
    session that BrewPi does not enforce one-actuator-per-function, so
    several stray same-function candidates left installed get driven
    together the instant that function is commanded, not just the intended
    one. Also reports the WAITING_TO_HEAT code explicitly (qualitative
    only -- the firmware never exposes a numeric remaining-wait-seconds
    value on the wire, confirmed via exhaustive search of PiLink.cpp) so
    the wizard can show "waiting on the compressor-protection timer"
    rather than a blank screen.

    Never forces a target below the current chamber temp -- this action
    must never command a cooling demand. Normal and reversed polarity are
    two separate calls to this same action (the caller passes `invert`
    explicitly); there is no "function" param and no per-call polarity
    retry mechanism here -- the frontend drives that sequencing.

    params: candidate: {"pin": int, "invert": 0|1}, window_s: float,
    optional.
    """
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return

    candidate = params.get("candidate") or {}
    pin = candidate.get("pin")
    if pin is None:
        job.state = "failed"
        job.error = "identify_relay_pin requires a candidate with a pin"
        return
    invert = int(candidate.get("invert", 0))
    window_s = float(params.get("window_s", RELAY_IDENTIFY_WINDOW_S))

    installed = await conn.list_all_devices(with_values=False)
    existing_heat = next((d for d in installed if d.function == DEVICE_FUNCTION_CHAMBER_HEAT), None)
    if existing_heat is not None:
        await _uninstall(conn, existing_heat.slot)
        installed = [d for d in installed if d.slot != existing_heat.slot]

    reserved_taken = next((d for d in installed if d.slot == RELAY_IDENTIFY_SLOT), None)
    slot = RELAY_IDENTIFY_SLOT if reserved_taken is None else _pick_free_slot(installed)

    device = BrewPiDevice(
        slot=slot,
        chamber=1,
        beer=0,
        function=DEVICE_FUNCTION_CHAMBER_HEAT,
        hardware=DEVICE_HARDWARE_PIN,
        deactivated=0,
        pin=pin,
        invert=invert,
    )
    await conn.install_device(device)

    reading = await conn.read_temps()
    if reading is None or reading.fridge_temp_f is None:
        job.state = "failed"
        job.error = "chamber probe isn't reading -- can't run a live relay test"
        return
    baseline_f = reading.fridge_temp_f
    # ALWAYS added, never subtracted -- see RELAY_IDENTIFY_FORCE_DELTA_F.
    forced_target = baseline_f + RELAY_IDENTIFY_FORCE_DELTA_F
    await conn.set_fridge_target(forced_target)

    try:
        current_f = baseline_f
        outcome = "waiting"
        job.result = {"outcome": outcome, "baseline_f": baseline_f, "forced_target_f": forced_target, "current_f": current_f, "slot": slot}
        elapsed_s = 0.0
        while elapsed_s < window_s:
            await ctx.clock.sleep(RELAY_IDENTIFY_POLL_S)
            elapsed_s += RELAY_IDENTIFY_POLL_S
            reading = await conn.read_temps()
            if reading is not None:
                current_f = reading.fridge_temp_f
                if reading.state in HEAT_ENGAGED_STATES:
                    outcome = "engaged"
                elif reading.state in HEAT_WAITING_STATES:
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
    reliably clears whatever candidate the relay pin-identification sweep
    last left installed. The `finally` fires a second, safety-net reset
    ONLY if the deliberate one was never reached (an install raised, or the
    job was cancelled mid-loop) -- never both, never neither. On success,
    clears the baseline snapshot file (see begin_device_config) -- the
    wizard's new configuration is the new reality; there's nothing left to
    revert to. Deliberately does NOT clear it on the failure path -- a
    failed finalize should still be recoverable via reset_brewpi or the
    next begin_device_config self-heal, not silently lose the pre-wizard
    state on top of failing.

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
        _clear_baseline()

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
    sweep; gating it would defeat exactly the case it exists for. If a
    baseline snapshot exists (the wizard started but never reached
    finalize), restores it and clears the file -- a real "revert to the
    beginning state," not just a bare reset. Otherwise just resets."""
    conn = _brewpi_connection(ctx)
    if conn is None:
        job.state = "failed"
        job.error = "BrewPi is not the current platform"
        return
    try:
        if _read_baseline() is not None:
            await _restore_baseline(conn)
            _clear_baseline()
        else:
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
    # Session bootstrap, not a hardware test -- deliberately NOT gated,
    # same reasoning as reset_brewpi below.
    "begin_device_config": PlatformTestRunner(run=_run_begin_device_config),
    # Read-only listing -- no hardware mutation, so no fermentation gate.
    "brewpi_devices": PlatformTestRunner(run=_run_brewpi_devices),
    # These four reconfigure real hardware (or force a real setpoint) --
    # blocked while a fermentation is active, checked synchronously by
    # daemon/tests_runtime.py's start_test() before the task is created
    # (see PlatformTestRunner's own docstring for why it can't live here).
    "identify_onewire_probes": PlatformTestRunner(run=_run_identify_onewire_probes, requires_no_active_fermentation=True),
    "install_probe": PlatformTestRunner(run=_run_install_probe, requires_no_active_fermentation=True),
    "identify_relay_pin": PlatformTestRunner(run=_run_identify_relay_pin, requires_no_active_fermentation=True),
    "finalize_device_config": PlatformTestRunner(run=_run_finalize_device_config, requires_no_active_fermentation=True),
    # The unconditional safety-net action -- deliberately NOT gated (see
    # _run_reset_brewpi's own docstring).
    "reset_brewpi": PlatformTestRunner(run=_run_reset_brewpi),
}
