"""Owns the one persistent serial connection to a BrewPi Arduino -- shared
by BrewPiPlatform.discover() (identification) and BrewPiChamberDriver (the
live role driver), same "one shared connection object, thin driver
wrappers around it" shape as SimPlantEngine/ManualPanel elsewhere in
platforms/.

Protocol (verified 2026-08-11 against a real BrewPi Remix rig over SSH --
see the implementation-decisions doc for the full session; nothing below
is guessed from documentation alone): 57600 baud; opening the port toggles
the Arduino's DTR line and triggers a bootloader reset, so the firmware
needs a few seconds after open before it answers anything. Commands are
single ASCII characters sent with a trailing '\\n'; responses are
newline-terminated lines shaped `<LETTER>:<payload>`, where <payload> is
strict JSON for the two read commands this driver uses ('n' version, 't'
temperatures) -- confirmed with the real Arduino: `n` -> `N:{"v":"0.2.13",
"n":"da7e14a9","s":2,"y":0,"b":"s","l":"3"}`, `t` -> `T:{"BeerTemp":null,
"BeerSet":null,"BeerAnn":null,"FridgeTemp":76.55,"FridgeSet":76.00,
"FridgeAnn":null,"State":0}`. The 'j' settings-write command instead takes
a relaxed/unquoted-key JS-object-literal body, not strict JSON --
`j{mode:"f", fridgeSet:65.5}` / `j{mode:"o"}` -- matching the exact syntax
BrewPi Remix's own brewpi.py sends in production against this same
firmware, copied rather than guessed.

One asyncio.Lock serializes command/response pairs on the shared serial
line -- without it, a control-tick read_chamber() racing a discover()
identify call (or a future concurrent set_target()) could send two
commands before either response arrives, with no way to tell which
response belongs to which request.
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from krauken.contracts.clock import Clock
from krauken.contracts.errors import PlatformUnavailable
from krauken.platforms.base import requires_optional

log = logging.getLogger("krauken.platforms.brewpi")

BAUD_RATE = 57600
# Measured boot time after the DTR-triggered reset was 2-3s; padded for
# margin. Paid once per connection (persistent thereafter), not per
# command, so a couple of extra seconds of headroom costs nothing.
BOOT_DELAY_S = 4.0
# Applied AFTER the boot delay above, so this only covers "the Arduino is
# booted but a reply is momentarily late" -- not "still booting". Kept
# short (not the reference script's own 10s/10-retry budget for
# getVersionFromSerial) specifically so probing several unrelated serial
# devices during discover()'s auto-scan can't burn through
# discovery.py's DEFAULT_SCAN_BUDGET_S (10s total, across every platform)
# on BrewPi alone.
QUERY_RETRIES = 4
QUERY_RETRY_INTERVAL_S = 0.5

# State codes from the classic BrewPi Arduino firmware (brewpi-firmware's
# TemperatureControl.h) -- only naming what read_chamber()'s ChamberMode
# mapping actually consumes, not the full enum.
STATE_IDLE = 0
STATES_HEATING = frozenset({3, 9, 14})
STATES_COOLING = frozenset({4, 8, 13})


@dataclass
class BrewPiReading:
    beer_temp_f: float | None
    fridge_temp_f: float | None
    fridge_set_f: float | None
    state: int | None


def _candidate_ports() -> list[str]:
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))


class BrewPiConnection:
    """Lazily connects -- constructed with no open port at all, since the
    real port isn't known until something (discover()'s auto-scan)
    identifies it; there's deliberately no config-specified port (project
    decision: auto-scan, since a single-chamber install has at most one
    BrewPi to find, so there's nothing a fixed port setting would need to
    disambiguate). `port` stays set across calls once a connect succeeds,
    so read_temps()/set_fridge_target() never re-scan themselves --
    identify_and_connect() is the only thing that does, and only discover()
    calls it (once per Hardware Setup scan).
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._serial: Any = None
        self.port: str | None = None
        self.version_info: dict | None = None
        # Tracked here (not just "whatever set_fridge_target last sent"
        # forgotten the moment the call returns) because ChamberDriver's
        # own commanded_target() and read_chamber()'s commanded_target_f
        # field both need to report it back later, from a fresh
        # BrewPiChamberDriver instance -- daemon/drivers.py constructs a
        # new driver object per call (`cls(state_obj)`), so nothing
        # instance-local to the driver survives between calls; only state
        # on this shared connection object does.
        self.commanded_target_f: float | None = None
        self._lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._serial is not None

    @requires_optional("serial", extra="pi")
    async def identify_and_connect(self, *, candidate_ports: list[str] | None = None) -> bool:
        """Tries the currently-connected port first (cheap, and avoids
        dropping a good connection just because a scan ran again), then
        every /dev/ttyACM*/ttyUSB* present. The current port is tried
        FIRST unconditionally, even if it doesn't happen to appear in this
        fresh os.glob() -- a transient enumeration gap must never drop an
        already-working connection just because rescanning it here
        couldn't re-derive that exact path a second time. Returns True
        iff a real BrewPi answered the version query on some port."""
        import serial as pyserial

        ports = candidate_ports if candidate_ports is not None else _candidate_ports()
        if self.port:
            ports = [self.port] + [p for p in ports if p != self.port]

        async with self._lock:
            for port in ports:
                if port != self.port or self._serial is None:
                    self._close_locked()
                    try:
                        ser = await asyncio.to_thread(pyserial.Serial, port, BAUD_RATE, timeout=1.0)
                    except (pyserial.SerialException, OSError):
                        continue
                    self._serial = ser
                    self.port = port
                    await asyncio.sleep(BOOT_DELAY_S)
                info = await self._query_locked("n", "N")
                if info is not None:
                    self.version_info = info
                    log.info("BrewPi identified on %s: %s", port, info)
                    return True
                self._close_locked()
        return False

    async def read_temps(self) -> BrewPiReading | None:
        """None means "couldn't get a reading right now" (not connected,
        or the Arduino didn't answer within the retry budget) -- the
        caller (BrewPiChamberDriver.read_chamber()) turns that into a
        Health.UNREACHABLE reading rather than raising, the same
        convention every other driver in this codebase follows for
        "hardware exists but isn't answering right now" (PlatformUnavailable
        is reserved for "the platform can't be used at all", e.g. no
        adapter, no dependency -- not a single missed poll)."""
        async with self._lock:
            data = await self._query_locked("t", "T")
        if data is None:
            return None
        return BrewPiReading(
            beer_temp_f=data.get("BeerTemp"),
            fridge_temp_f=data.get("FridgeTemp"),
            fridge_set_f=data.get("FridgeSet"),
            state=data.get("State"),
        )

    async def set_fridge_target(self, temp_f: float | None) -> None:
        """temp_f=None -> `j{mode:"o"}` (off/idle -- the Arduino stops
        driving its relays entirely, matching ChamberDriver.set_target()'s
        own "None = idle" contract exactly). Otherwise fridge-constant
        mode, `j{mode:"f", fridgeSet:<temp_f>}` -- deliberately NOT
        beer-constant mode: the daemon's own control loop already computes
        the fridge target it wants, so handing the Arduino a beer setpoint
        and letting its on-board beer-constant logic compute a second,
        independent fridge target on top would be two controllers
        disagreeing about the same knob -- exactly what the design doc's
        "compressor protection lives exactly once, never stacked" rule
        forbids one layer up."""
        body = 'j{mode:"o"}' if temp_f is None else f'j{{mode:"f", fridgeSet:{temp_f}}}'
        self.commanded_target_f = temp_f
        async with self._lock:
            if self._serial is None:
                return
            try:
                await asyncio.to_thread(self._serial.write, f"{body}\n".encode("ascii"))
            except Exception:  # noqa: BLE001 -- best-effort; next read_temps() surfaces UNREACHABLE
                log.warning("failed to write set_fridge_target(%r) to BrewPi", temp_f)

    async def start(self) -> None:
        """Backgrounds identify_and_connect() -- it can take several real
        seconds (the Arduino's DTR-triggered boot delay, times however many
        candidate serial ports get tried), and platforms/registry.py's
        PlatformRegistry.start_all() calls start() on every enabled
        platform generically, uniformly, without waiting for any one of
        them. This used to be daemon/app.py's own responsibility
        (asyncio.create_task(self._connect_brewpi()), a background task it
        tracked itself) -- moved here so the daemon never needs to know
        BrewPi specifically might be slow to connect. identify_and_connect()
        itself is untouched and still directly awaitable/callable on its
        own (see discover(), and tests that call it directly for its real
        boolean return)."""
        self._connect_task = asyncio.create_task(self._background_connect())

    async def _background_connect(self) -> None:
        try:
            found = await self.identify_and_connect()
            if not found:
                log.info("BrewPi not found at startup -- will retry on next Hardware Setup scan")
        except PlatformUnavailable as e:
            log.warning("BrewPi connection not attempted at startup: %s", e)

    async def stop(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
            self._connect_task = None
        await self.close()

    async def close(self) -> None:
        async with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Caller must hold self._lock."""
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001 -- best-effort cleanup
                pass
        self._serial = None
        self.port = None

    async def _query_locked(self, command: str, expect_prefix: str) -> dict | None:
        """Caller must hold self._lock. Sends one command, retrying
        QUERY_RETRIES times, until a line starting with `expect_prefix +
        ':'` arrives; returns its JSON payload, or None if nothing
        matched within the retry budget or the connection isn't open."""
        if self._serial is None:
            return None
        for _ in range(QUERY_RETRIES):
            try:
                await asyncio.to_thread(self._serial.write, f"{command}\n".encode("ascii"))
            except Exception:  # noqa: BLE001 -- a write failure means the port is gone
                return None
            deadline = time.monotonic() + QUERY_RETRY_INTERVAL_S
            while time.monotonic() < deadline:
                try:
                    line = await asyncio.to_thread(self._serial.readline)
                except Exception:  # noqa: BLE001
                    return None
                if not line:
                    continue
                text = line.decode("ascii", errors="replace").strip()
                if text.startswith(f"{expect_prefix}:"):
                    try:
                        return json.loads(text[len(expect_prefix) + 1 :])
                    except json.JSONDecodeError:
                        log.warning("unparseable BrewPi response: %r", text)
                        return None
        return None
