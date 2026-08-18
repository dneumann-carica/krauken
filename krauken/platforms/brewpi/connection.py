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

Device-configuration commands (verified 2026-08-15 against the same real
rig, plus reading brewpi-remix/brewpi-firmware-rmx's actual firmware
source -- see plans/jiggly-bubbling-popcorn.md for the full session):
`d{}` -> installed devices only (each has a real "i" slot index); `d{r:1}`
-> same, plus a live "v" value per entry; `h{u:-1}` -> available/
uninstalled devices only ("i":-1) -- a bare 'h' with no argument returns a
different, undocumented mixed list, never use it; `h{u:-1,v:1}` -> same,
with live "v" (null if currently unreadable -- confirmed real on this rig,
not a timing fluke). `U<json>` installs/updates a device -- confirmed via
DeviceManager.cpp's parseDeviceDefinition() that "i" must be an explicit,
currently-unused slot number (0-15, MAX_DEVICE_SLOT=16); -1 or omitting
"i" both fail its very first range check identically and SILENTLY (no
response at all), which is exactly what two live attempts using those
shapes produced before this was traced to source. A successful install
does echo a `U:{...}` response line; this driver still re-queries
list_installed_devices() to confirm rather than depending on parsing it.
`R` triggers a real AVR watchdog hardware reset (confirmed via main.cpp's
handleReset(): `wdt_enable(WDTO_60MS)` then spin) -- EEPROM (installed
devices) survives, RAM does not. See platforms/brewpi/device_config.py
for the device model and the actual configuration workflow built on top
of these four methods.

One asyncio.Lock serializes command/response pairs on the shared serial
line -- without it, a control-tick read_chamber() racing a discover()
identify call (or a future concurrent set_target()) could send two
commands before either response arrives, with no way to tell which
response belongs to which request. The four new methods below share this
same lock/query pattern.
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from krauken.contracts.clock import Clock
from krauken.contracts.errors import PlatformUnavailable
from krauken.platforms.base import requires_optional
from krauken.platforms.brewpi.device_config import BrewPiDevice

log = logging.getLogger("krauken.platforms.brewpi")

BAUD_RATE = 57600
# Confirmed live 2026-08-18 (extensively, repeatedly, not a one-off): the
# real post-DTR-reset boot delay on this hardware is ~15-20s, not the
# 2-3s this constant originally assumed -- dominated by the display
# driver's I2C address scan (scan_address() in IIClcd::init_priv(),
# sweeping addresses 8-119 with no display physically present to ACK).
# The old, too-short value caused a real, reproducible daemon bug: every
# identify_and_connect() attempt queried the freshly-opened port before
# the Arduino had actually finished booting, got no response at all,
# closed the connection, and logged "BrewPi not found at startup" --
# confirmed via the daemon's own journal. Paid once per connection
# (persistent thereafter), not per command, so generous headroom here
# costs nothing in steady-state operation.
BOOT_DELAY_S = 22.0
# Real cost of this increase, checked directly rather than assumed:
# identify_and_connect() only pays this delay when actually opening a
# fresh connection (a new port, or self._serial is None) -- an
# already-connected BrewPi costs nothing extra on a later discover()
# rescan, matching this constant's own original "paid once per
# connection, persistent thereafter" reasoning above. For a genuine
# cold-connect (first time, or after a lost connection), a Hardware
# Setup scan that includes BrewPi will now take ~22s longer than before
# to report a result -- confirmed there's no actual enforced budget this
# blows through: discovery.py's DEFAULT_SCAN_BUDGET_S is defined but
# never read/enforced anywhere in discovery.py's own _run_scan() (which
# just awaits every platform concurrently via asyncio.gather with no
# timeout at all) -- so this trades a slower cold-connect scan for a
# connection that actually succeeds, not a slower scan that also still
# fails, which is what the old, too-short value produced in practice.
#
# A single flat wait, no resend -- confirmed live 2026-08-18 via this
# session's own strace captures that re-sending the command on every
# retry (the old QUERY_RETRIES/QUERY_RETRY_INTERVAL_S shape) creates two
# outstanding requests for the same thing whenever the genuine response
# is just running a little behind (e.g. an async D:{...} log line
# interleaving), and their replies can arrive interleaved or out of order
# relative to what the client expects next -- confirmed 44 occurrences of
# the identical command being written twice within under a second of
# itself across one real session. Real write->matching-response latency
# measured directly from that session's own log (continuous-activity
# delays only, excluding idle gaps): median 0.58s, p90 2.5s, p99 5.9s,
# max 9.35s. 15s is a backstop with real margin over every observed case,
# not a number expected to be hit often.
QUERY_TIMEOUT_S = 15.0

# Confirmed real on the actual rig, not a parsing bug in this driver: the
# firmware sometimes interleaves an unrelated log message (a bare
# `D:{...}` fragment) into the MIDDLE of a 'd'/'h' device-list response,
# splitting one logical JSON array across physical lines, e.g.
# `d:[D:{"logType":"E","logID":10,"V":[10]}` followed by a second line
# continuing the array. Stripped out before parsing by
# _query_device_list_locked(); log payloads observed so far are flat
# (no nested braces), so a non-greedy single-level match is sufficient.
_LOG_FRAGMENT_RE = re.compile(r"D:\{[^{}]*\}")

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

    async def list_installed_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        """`d{r:1}` (or `d{}` if with_values=False) -- installed devices
        only (each has a real "i" slot index)."""
        command = "d{r:1}" if with_values else "d{}"
        async with self._lock:
            data = await self._query_device_list_locked(command, "d")
        return [BrewPiDevice.from_json(raw) for raw in (data or [])]

    async def list_available_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        """ALWAYS the explicit `h{u:-1,v:1}` / `h{u:-1}` form -- never a
        bare 'h' (confirmed to return a different, undocumented mixed
        list -- see module docstring)."""
        command = "h{u:-1,v:1}" if with_values else "h{u:-1}"
        async with self._lock:
            data = await self._query_device_list_locked(command, "h")
        return [BrewPiDevice.from_json(raw) for raw in (data or [])]

    async def list_all_devices(self, *, with_values: bool = True) -> list[BrewPiDevice]:
        """installed + available, concatenated. The ONLY method any caller
        above this layer should use to build a candidate list -- using
        either list alone is exactly the bug found this session (an
        already-installed device becomes invisible to anything that only
        asks 'h' or only asks 'd')."""
        installed = await self.list_installed_devices(with_values=with_values)
        available = await self.list_available_devices(with_values=with_values)
        return installed + available

    async def install_device(self, device: BrewPiDevice) -> None:
        """`U<json>` -- fire-and-forget write, same convention as
        set_fridge_target: brewpi.py's own handler proxies this straight
        through with zero validation, and a rejected write produces no
        response at all (confirmed via source -- see module docstring), so
        there's nothing useful to await here. Caller re-queries
        list_installed_devices() to confirm the install actually took."""
        body = "U" + json.dumps(device.to_wire_json())
        async with self._lock:
            if self._serial is None:
                return
            try:
                await asyncio.to_thread(self._serial.write, f"{body}\n".encode("ascii"))
            except Exception:  # noqa: BLE001 -- best-effort; caller's re-query surfaces the real outcome
                log.warning("failed to write install_device(%r) to BrewPi", device)

    async def reset_and_reconnect(self) -> bool:
        """`R` -- a real AVR watchdog hardware reset (confirmed via
        main.cpp's handleReset()), not a soft idle. EEPROM (installed
        devices) survives; RAM does not. Writes 'R\\n', waits
        BOOT_DELAY_S the same as a fresh connection would, then re-runs
        identify_and_connect() against the same port -- a watchdog reset
        doesn't re-enumerate the USB device the way a real unplug/replug
        would, so the existing port path is still correct. Returns
        whatever identify_and_connect() returns.

        Uses self.clock.sleep(), not a raw asyncio.sleep -- unlike
        identify_and_connect()'s own boot delay (deliberately real-time
        always, since a fresh connection can happen before any platform
        selection is settled), this method only ever runs against an
        already-selected BrewPi connection, whose clock is already
        whatever the daemon chose for the whole session (ProductionClock
        for any real hardware mapping, SimulatorClock only in tests) --
        so this is free to respect it, keeping unit tests fast."""
        async with self._lock:
            if self._serial is not None:
                try:
                    await asyncio.to_thread(self._serial.write, b"R\n")
                except Exception:  # noqa: BLE001 -- best-effort; identify_and_connect() below re-verifies
                    log.warning("failed to write reset command to BrewPi")
        await self.clock.sleep(BOOT_DELAY_S)
        return await self.identify_and_connect()

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

    async def _query_locked(self, command: str, expect_prefix: str) -> Any | None:
        """Caller must hold self._lock. Sends the command exactly ONCE,
        then reads until a line starting with `expect_prefix + ':'`
        arrives or QUERY_TIMEOUT_S elapses -- never resends mid-wait (see
        QUERY_TIMEOUT_S's own comment for why: a resend while the genuine
        reply is just running behind creates two outstanding requests for
        the same thing, and their replies can arrive interleaved or out of
        order). Returns the parsed JSON payload, or None if nothing
        matched within the window or the connection isn't open. Fine for
        't'/'n'/'s'/'c' -- confirmed those always arrive as one complete,
        self-contained line. NOT used for 'd'/'h' (see
        _query_device_list_locked): those can arrive split across
        multiple physical lines with a log message interleaved mid-array."""
        if self._serial is None:
            return None
        try:
            await asyncio.to_thread(self._serial.write, f"{command}\n".encode("ascii"))
        except Exception:  # noqa: BLE001 -- a write failure means the port is gone
            return None
        deadline = time.monotonic() + QUERY_TIMEOUT_S
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

    async def _query_device_list_locked(self, command: str, expect_prefix: str) -> list[Any] | None:
        """Caller must hold self._lock. Like _query_locked (single write,
        no resend, one QUERY_TIMEOUT_S deadline), but for 'd'/'h'
        responses specifically: accumulates lines starting from the one
        with the matching prefix (stripping any embedded log fragments --
        see _LOG_FRAGMENT_RE) until the accumulated text parses as a JSON
        array, or the timeout is exhausted."""
        if self._serial is None:
            return None
        try:
            await asyncio.to_thread(self._serial.write, f"{command}\n".encode("ascii"))
        except Exception:  # noqa: BLE001 -- a write failure means the port is gone
            return None
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        accumulated = ""
        collecting = False
        while time.monotonic() < deadline:
            try:
                line = await asyncio.to_thread(self._serial.readline)
            except Exception:  # noqa: BLE001
                return None
            if not line:
                continue
            text = line.decode("ascii", errors="replace").strip()
            if not collecting:
                if not text.startswith(f"{expect_prefix}:"):
                    continue
                text = text[len(expect_prefix) + 1 :]
                collecting = True
            accumulated += _LOG_FRAGMENT_RE.sub("", text)
            try:
                parsed = json.loads(accumulated)
            except json.JSONDecodeError:
                continue  # incomplete so far (or a stray log fragment) -- keep reading
            if isinstance(parsed, list):
                return parsed
            log.warning("unexpected non-list BrewPi device-list response: %r", accumulated)
            return None
        return None
