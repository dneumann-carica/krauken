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
firmware, copied rather than guessed. `j`'s reply is not one line but a
variable-length burst (0+ D:/T: lines depending on which keys were sent,
then always S: then always C: last) -- see set_fridge_target()'s
_drain_until_locked() call for why that whole burst gets read and
discarded before the write returns. Unlike the IPC-backed drivers
(platforms/ipc_driver.py's module docstring: resend every tick
regardless of change, cheap and self-healing over an in-process/socket
call), a `j` write here is a real serial round-trip plus a burst-drain,
so set_fridge_target() skips it when unchanged from the Arduino's own
last-reported FridgeSet -- see that method's own docstring and
BrewPiConnection._last_reported_fridge_set_f for why comparing against a
real read (not a locally-cached "what we last sent") is what keeps this
safe across a reset_brewpi (a wizard safety-net action deliberately
allowed to run even during an active fermentation, per
contracts/interfaces.py's requires_no_active_fermentation handling).

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
import logging.handlers
import re
import time
from dataclasses import dataclass
from typing import Any

from krauken.contracts.clock import Clock
from krauken.contracts.errors import PlatformUnavailable
from krauken.platforms.base import requires_optional
from krauken.platforms.brewpi.device_config import BrewPiDevice

log = logging.getLogger("krauken.platforms.brewpi")

# Raw serial read/write trace -- see plans/i-do-want-the-mellow-sloth.md.
# The control loop has been observed stalling for real minutes at a time on
# the actual rig, with only a single confirmed-malformed response to go on;
# rather than guess further at the mechanism, every byte in or out gets
# logged here so the next occurrence can be read directly. Separate logger
# (not `log` above) so this never mixes into or floods the normal daemon
# log -- it has its own dedicated, rotating file instead.
_SERIAL_TRACE_LOG_PATH = "/var/lib/krauken/brewpi-serial.log"
_serial_trace_log = logging.getLogger("krauken.platforms.brewpi.serial_trace")
_serial_trace_configured = False


class _UTCFormatter(logging.Formatter):
    """Timestamps in UTC, matching timefmt.iso()'s convention elsewhere in
    this app -- logging.Formatter defaults to local time, which would make
    cross-referencing this trace against samples/events timestamps a
    real source of confusion, not just an inconsistency."""

    converter = time.gmtime  # type: ignore[assignment]


def _ensure_serial_trace_configured() -> None:
    """Lazy, best-effort, tried exactly once -- not at import time, since
    this module is imported by the test suite on machines that have no
    /var/lib/krauken at all. A failure here (missing directory, no
    permission) must never crash real serial I/O over a diagnostic
    concern; it just means the trace falls through to whatever the
    default logging config already does with it (journald), same as
    before this existed, rather than silently going nowhere."""
    global _serial_trace_configured
    if _serial_trace_configured:
        return
    _serial_trace_configured = True
    try:
        handler = logging.handlers.RotatingFileHandler(
            _SERIAL_TRACE_LOG_PATH, maxBytes=10_000_000, backupCount=5,
        )
        handler.setFormatter(_UTCFormatter("%(asctime)s.%(msecs)03d %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
        _serial_trace_log.addHandler(handler)
        # Only suppress the default (journald) path once the dedicated
        # file is confirmed working -- if the handler above raised, this
        # line is never reached, so the trace still goes somewhere.
        _serial_trace_log.propagate = False
    except OSError:
        pass
    _serial_trace_log.setLevel(logging.INFO)

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

# Confirmed live 2026-08-19, via strace of the daemon's own serial fd,
# during the device-config wizard's relay-identification sweep: two
# install_device() writes issued back-to-back (abandoning the previous
# candidate, then installing the next one -- no read of any kind in
# between, since install_device() is a deliberate fire-and-forget write,
# see its own docstring) arrived only ~12ms apart, and the SECOND command
# came back corrupted -- echoed as a fully-zeroed/uninstalled device
# ({"f":0,"h":0,"p":0}) instead of what was actually sent. Installing a
# device almost certainly triggers a synchronous EEPROM write on the AVR
# (EEPROM writes are ~3.3ms/byte and block the main loop), which can
# outlast the Arduino's small serial RX buffer if a second full command's
# bytes arrive before the first one has been fully processed -- exactly
# what happened here. The old wait-based wizard design never hit this:
# it always had several real seconds of polling between installs by
# accident. The redesigned, deliberately-fast off-based sweep (see
# device_config.py's identify_relay_pin) removed that accidental
# protection, so install_device() now provides it directly instead of
# relying on every caller to happen to have one.
INSTALL_SETTLE_S = 0.3
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
# continuing the array. Log payloads observed so far are flat (no nested
# braces), so a non-greedy single-level match is sufficient.
#
# DELIBERATELY UNUSED right now (see _query_device_list_locked's own
# docstring, and plans/i-do-want-the-mellow-sloth.md): this used to be
# stripped out before parsing, but a live investigation into a real
# multi-minute control-loop stall isn't confident this is even the right
# diagnosis yet -- rather than keep quietly correcting/hiding this exact
# wire behavior, it's left in place unstripped for now so the new serial
# trace log can show it happening directly. Left defined, not deleted,
# since it's very likely needed again (here and/or in _query_locked) once
# the real mechanism is confirmed.
_LOG_FRAGMENT_RE = re.compile(r"D:\{[^{}]*\}")

# Sentinel for BrewPiConnection._last_reported_fridge_set_f's initial value --
# see that field's own comment. Deliberately not None: a real FridgeSet can
# legitimately BE None (mode "o"/off reports FridgeSet:null), so a sentinel
# that can never equal a real float or None is needed to distinguish "we
# have genuinely never read one yet" from "we read one and it was null."
_UNKNOWN_FRIDGE_SET: Any = object()

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
        # What the Arduino ITSELF most recently reported as its own
        # FridgeSet, from a real T: response's "FridgeSet" field -- NOT a
        # locally-tracked assumption about what set_fridge_target() last
        # sent (that's commanded_target_f above, which keeps its own
        # "last requested, for display, regardless of outcome" meaning
        # unchanged). set_fridge_target() dedupes against THIS field, not
        # commanded_target_f, specifically so the dedupe stays correct
        # across any reset/reconnect with no separate invalidation logic:
        # whatever the Arduino's real FridgeSet becomes after a reset (a
        # real AVR watchdog reset via reset_and_reconnect(), or any fresh
        # port open, which DTR-resets it), the very next read_temps() call
        # reports it here, and that's what the next set_fridge_target()
        # call compares against -- see connection.py's own module docstring
        # and set_fridge_target()'s docstring for the full reasoning.
        # Starts at _UNKNOWN_FRIDGE_SET (not None) so the very first
        # set_fridge_target() call -- even set_fridge_target(None) -- always
        # writes rather than coincidentally matching an unset None.
        self._last_reported_fridge_set_f: Any = _UNKNOWN_FRIDGE_SET
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
        adapter, no dependency -- not a single missed poll). Also records
        the response's "FridgeSet" into _last_reported_fridge_set_f (see
        that field's own comment) -- set_fridge_target()'s dedupe is
        grounded in this, so every successful read keeps it fresh whether
        or not the caller cares about fridge_set_f on the returned
        BrewPiReading itself."""
        async with self._lock:
            data = await self._query_locked("t", "T")
            if data is not None:
                self._last_reported_fridge_set_f = data.get("FridgeSet")
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
        forbids one layer up.

        Drains the resulting response burst (see _drain_until_locked())
        before returning -- this used to write and return immediately,
        leaving the Arduino's reply (D:/T: echoes, then always S: then
        C:) sitting unread until some later, unrelated read happened to
        drain past it and could mistake a stale line for its own fresh
        answer. Still fire-and-forget in the sense that nothing here
        parses or uses that reply -- draining it is purely so it can't
        contaminate a later read, not because this method needs it.

        Skips the write entirely (no serial round-trip, no burst to
        drain) when temp_f already matches _last_reported_fridge_set_f --
        confirmed live that the daemon's control loop calls this every
        ~30s tick even when the target hasn't moved (a held/constant
        fermentation stage), which used to mean a real j write plus a
        full burst-drain every tick for nothing. Deliberately dedupes
        against _last_reported_fridge_set_f (a real read), never against
        commanded_target_f (what we last asked for) -- see that field's
        own comment for why: this makes the dedupe self-healing across
        any reset without separate invalidation bookkeeping, since a
        reset changes what the Arduino reports on the next read_temps()
        before it changes what we'd next command. This is why the dedupe
        is only effective for a caller that reads before it writes every
        cycle, true of control_loop.py's real per-tick pattern (the
        actual target of this) -- a caller that writes repeatedly with no
        interleaved read (e.g. device_config.py's identify_relay_pin
        sweep) simply keeps writing every time, unchanged, which is safe,
        just not optimized.

        Rounds temp_f to 1 decimal place before doing anything else with
        it -- confirmed live via the serial trace log that the PI
        cascade's raw float output (e.g. 52.672244164987845, carrying the
        full accumulated-integral precision) was going out over the wire
        verbatim, bloating every `j` write for digits the Arduino's own
        temperature resolution can't meaningfully use. Rounding here,
        before the dedupe check below, is a double win rather than just a
        smaller payload: the PI integral nudges its raw output by a tiny
        fraction every tick even while genuinely holding steady (see a
        real recorded sequence: 52.672, 52.681, 52.686, ... -- all
        different bit-for-bit, all the literal same 52.7 once rounded),
        so rounding first also means the dedupe check actually matches
        far more often, cutting real j writes, not just their length."""
        if temp_f is not None:
            temp_f = round(temp_f, 1)
        body = 'j{mode:"o"}' if temp_f is None else f'j{{mode:"f", fridgeSet:{temp_f}}}'
        self.commanded_target_f = temp_f
        async with self._lock:
            if self._serial is None:
                return
            if temp_f == self._last_reported_fridge_set_f:
                return
            try:
                await self._write_traced(f"{body}\n".encode("ascii"))
                await self._drain_until_locked("C")
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
        there's no response worth awaiting here. Caller re-queries
        list_installed_devices() to confirm the install actually took.

        Settles for INSTALL_SETTLE_S after writing -- see that constant's
        own comment for the confirmed-live corruption this prevents
        (two installs sent back-to-back, with nothing else awaited in
        between, can arrive faster than the Arduino's own EEPROM write for
        the first one finishes, corrupting the second). Uses a real
        asyncio.sleep(), NOT self.clock.sleep() -- deliberately, same
        precedent as identify_and_connect()'s own boot delay: this method
        can run before any platform role is mapped (that's the entire
        point of the device-config wizard), which is exactly when the
        daemon's clock selection can still land on the accelerated
        SimulatorClock (see _select_clock() in daemon/app.py) -- a
        clock-relative sleep here would silently evaporate in precisely
        the scenario this delay exists to protect."""
        body = "U" + json.dumps(device.to_wire_json())
        async with self._lock:
            if self._serial is None:
                return
            try:
                await self._write_traced(f"{body}\n".encode("ascii"))
            except Exception:  # noqa: BLE001 -- best-effort; caller's re-query surfaces the real outcome
                log.warning("failed to write install_device(%r) to BrewPi", device)
                return
            # Held INSIDE the lock, not after it -- otherwise a concurrent
            # caller (e.g. a control-tick read_temps()) could send its own
            # command into exactly the same window this delay exists to
            # protect, reintroducing the same hazard from a different path.
            await asyncio.sleep(INSTALL_SETTLE_S)

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
                    await self._write_traced(b"R\n")
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

    async def _write_traced(self, data: bytes) -> None:
        """Every raw serial write goes through here, so the trace log has
        no gaps regardless of which higher-level method triggered it.
        Raises exactly like a bare self._serial.write() would -- callers
        keep their own existing try/except; this only adds the trace line
        before attempting the real write."""
        _ensure_serial_trace_configured()
        _serial_trace_log.info("WRITE %r", data)
        await asyncio.to_thread(self._serial.write, data)

    async def _readline_traced(self) -> bytes:
        """Every raw serial read goes through here. Logs even an empty/
        timed-out read (pyserial's readline() returns b"" rather than
        blocking forever, given the timeout=1.0 the port was opened
        with) -- during a real stall, a run of empty reads IS the
        signal: it distinguishes "nothing arrived for minutes" from
        "we never even got a turn to try" (lock contention) from
        "garbage kept arriving" (a parsing/framing problem), which a
        trace that only logs successful reads couldn't tell apart."""
        _ensure_serial_trace_configured()
        line = await asyncio.to_thread(self._serial.readline)
        _serial_trace_log.info("READ %r", line if line else b"<timeout, no data>")
        return line

    async def _drain_until_locked(self, terminal_prefix: str) -> None:
        """Caller must hold self._lock, immediately after writing a
        command whose reply is a variable-length BURST of lines rather
        than one parseable payload -- specifically `j`. Confirmed live
        2026-08-23 (see plans/i-do-want-the-mellow-sloth.md) via the new
        serial trace log: set_fridge_target() used to write 'j{...}' and
        return without reading anything back, but the real firmware's
        PiLink::receiveJson() always answers a 'j' with a burst -- zero
        or more D:/T: lines (one D: per settings key received, plus a
        full extra T: line for each of 'mode'/'fridgeSet' that actually
        triggered setMode()'s/setFridgeSetting()'s own annotation print,
        e.g. `T:{...,"FridgeAnn":"Fridge set to 61.0 by web",...}`),
        THEN unconditionally S: (sendControlSettings()) and C:
        (sendControlConstants()) -- C: always last, even on an empty or
        malformed body, since neither trailing call is gated on the
        parse actually succeeding. Left unread, that whole burst just
        sat in the OS-level serial buffer for whatever LATER, unrelated
        _query_locked() call happened to run next -- which matches on
        expect_prefix alone with no freshness check, so it could (and,
        confirmed via the trace log, did) return a stale T: a 'j' had
        queued up well before the 't' that "answered" it was ever sent.
        Draining here, right after the write that caused the burst,
        means nothing is left behind for a later read to misattribute.
        Bounded by the same QUERY_TIMEOUT_S deadline as the query
        methods below, as a backstop in case a byte gets dropped and
        `terminal_prefix` never arrives -- content of every drained line
        is discarded; nothing here is worth parsing since no caller of
        set_fridge_target() currently wants the echoed settings back."""
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        while time.monotonic() < deadline:
            line = await self._readline_traced()
            if not line:
                continue
            text = line.decode("ascii", errors="replace").strip()
            if text.startswith(f"{terminal_prefix}:"):
                return

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
            await self._write_traced(f"{command}\n".encode("ascii"))
        except Exception:  # noqa: BLE001 -- a write failure means the port is gone
            return None
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                line = await self._readline_traced()
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
        with the matching prefix until the accumulated text parses as a
        JSON array, or the timeout is exhausted.

        Deliberately NOT stripping embedded log fragments right now (see
        _LOG_FRAGMENT_RE's own comment, and plans/i-do-want-the-mellow-
        sloth.md) -- a real, confirmed-live failure mode this used to
        paper over (an interleaved `D:{...}` fragment splitting one
        logical response across physical lines) is temporarily left
        unmitigated on purpose, so the new serial trace log
        (_write_traced/_readline_traced) can show it happening
        byte-for-byte instead of it being silently cleaned up here. This
        genuinely reopens that old failure mode in the Hardware Setup
        wizard's device-list reads until the actual stalling mechanism is
        confirmed and a real fix (here and/or in _query_locked) lands."""
        if self._serial is None:
            return None
        try:
            await self._write_traced(f"{command}\n".encode("ascii"))
        except Exception:  # noqa: BLE001 -- a write failure means the port is gone
            return None
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        accumulated = ""
        collecting = False
        while time.monotonic() < deadline:
            try:
                line = await self._readline_traced()
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
            accumulated += text
            try:
                parsed = json.loads(accumulated)
            except json.JSONDecodeError:
                continue  # incomplete so far (or a stray log fragment) -- keep reading
            if isinstance(parsed, list):
                return parsed
            log.warning("unexpected non-list BrewPi device-list response: %r", accumulated)
            return None
        return None
