# BrewPi + Tilt implementation: decisions to ratify

## v0.1.6: Krauken takes over BrewPi's Device Configuration entirely

The shipped `confirm_heater`/`identify_probes` setup flow silently assumed
BrewPi's classic web UI had already mapped which OneWire probe is chamber/
beer and which pin drives cooling/heating. Checked against the actual
reference rig (`brewpi.local`) and that assumption is false in practice:
a cooling actuator is installed, but no heat device is installed at all,
anywhere -- `confirm_heater` could only ever report "not confirmed" there,
correctly, but for the wrong reason (no device to drive, not "no heater
wired"). Decision: Krauken discovers and installs the mapping itself, via
a guided wizard built on four new BrewPi serial commands.

**Protocol facts, all confirmed against the real Arduino (firmware
0.2.13) and `brewpi-remix/brewpi-firmware-rmx`'s actual source, not
guessed:**
- `d{}`/`d{r:1}` (installed devices, optionally with live values),
  `h{u:-1}`/`h{u:-1,v:1}` (available devices) -- a bare `'h'` with no
  argument returns a different, undocumented mixed list; never used.
- `U<json>` installs/updates a device. `DeviceManager.cpp`'s
  `parseDeviceDefinition()` rejects an install with `"i":-1` or omitted
  entirely, silently, at its very first range check
  (`inRangeInt8(dev.id, 0, MAX_DEVICE_SLOT)`) -- confirmed by two live
  attempts that both produced no response and no installed device.
  Reading the source explained why: the firmware doesn't auto-assign a
  slot at all; the caller must supply an explicit, currently-unused slot
  number (0-15, `MAX_DEVICE_SLOT=16`). A third live attempt with
  `"i":0` confirmed the fix -- installed cleanly, `"f":0` uninstalled
  cleanly, rig back to its exact starting baseline.
- `R` triggers a real AVR watchdog reset (`main.cpp`'s `handleReset()`).
  EEPROM survives; RAM does not.
- Anti-short-cycle protection (`TempControl.h`/`.cpp`) is the crux of the
  whole design. `TempControl::init()` zeroes `lastHeatTime`/`lastCoolTime`
  on every boot specifically to force a wait after any reset (source
  comment: *"Do not allow heating/cooling directly after reset...
  could damage the compressor"*) -- rebooting does NOT clear the wait,
  it restarts it. But `lastHeatTime`/`lastCoolTime` track the *logical
  function's* engagement history, not which physical pin backs it
  (confirmed via `DeviceManager.cpp`'s `parseDeviceDefinition`, which
  swaps the actuator pointer without resetting either): once any relay
  has genuinely engaged, reassigning that function to a different
  candidate pin engages the new one within one control-loop tick. Pay
  the ~10-minute wait once per session, sweep the rest near-instantly.
- A positively-identified relay is deliberately left energized while the
  sweep continues for the other function, rather than reverted --
  confirmed the protection timers guard against *rapid cycling*, not
  continuous running, so this doesn't violate what they're actually for.
  The final pushed configuration is followed by a real reset, which
  deterministically forces every pin to a safe level: `ActuatorPin.h`'s
  `DigitalPinActuator` constructor calls `setActive(false)` *before*
  `pinMode(pin, OUTPUT)`, so any pin reconstructed from EEPROM at boot is
  forced correct regardless of what state it was left in.
- A real firmware quirk found empirically, not by inspection: `'d'`/`'h'`
  responses sometimes interleave an unrelated log message mid-array,
  splitting one JSON array across physical lines
  (`d:[D:{"logType":"E",...}` on one line, the rest of the array on the
  next). `connection.py`'s existing single-line `_query_locked()` would
  silently fail to parse this; a dedicated `_query_device_list_locked()`
  strips embedded log fragments and accumulates lines until the result
  parses as a JSON array.

**Also found live, off to the side of this feature**: a second physical
OneWire probe on the reference rig was intermittently "detected but
unreadable" (`v: null`, paired with a firmware warning: `"Temperature
sensor disconnected pin %d, address %s"`, confirmed via
`LogMessages.h`). Isolated via a physical swap test (moved the *working*
probe into the suspect slot; it immediately showed the classic DS18B20
85°C/`185°F` power-on-reset value) -- the fault is the slot's wiring, not
either sensor. The wizard's `identify_onewire_probes` action tracks
`readable` per address explicitly for exactly this reason: a
detected-but-unreadable probe is a real, ongoing hardware state, not a
transient hiccup to retry into "reading…" forever.

**Architecture note, corrected mid-implementation**: the original draft
of this plan put all five new job runners directly in
`daemon/tests_runtime.py`, hardcoding the string `"brewpi"` -- exactly
the daemon-composition-root violation the `PlatformRegistry` refactor
(see below) existed to prevent. Corrected before writing production code:
`platforms/registry.py`'s `PlatformBinding` gained a `test_runners` table
(via a new `PlatformTestRunner` in `contracts/interfaces.py`, to avoid a
circular import back into `tests_runtime.py`), so `daemon/tests_runtime.py`
never needs to know BrewPi exists -- it just falls back to
`PLATFORM_BINDINGS[platform].test_runners` for any action it doesn't
recognize generically. `confirm_heater`/`identify_probes`/`fire_outlet`
needed no such move: re-reading them confirmed they already dispatch
purely through the abstract `ChamberDriver`/`ChamberMode` Protocol, with
zero references to any concrete platform.

## v0.1.5: real control-loop crash, caught live testing on brewpi.local

You'd remapped a live fermentation's `beer_temp`/`beer_gravity` from the
real Tilt to `simulator:tilt` (to see it run on the accelerated clock),
and asked me to restart the daemon to pick that up. Within about 2
minutes, the daemon was pinned at ~44% CPU (this Pi Zero W's whole
single core) and spamming >100 log lines/second -- caught and stopped
immediately, then root-caused from the real tracebacks, not guessed.

**What actually happened**, end to end:
1. `daemon/app.py`'s `_select_clock()` is evaluated once, at daemon
   startup -- with the mapping now pure-simulator, it correctly picked
   `SimulatorClock` (real behavior, working as documented).
2. `SimulatorClock.sleep()` never actually waits -- it advances its own
   counters by the full requested amount and returns immediately, so
   `_control_loop()`'s `while True` fires control ticks as fast as the
   event loop and real IPC round-trips allow, each one advancing
   simulated elapsed time (`t_h`) by another `control_tick_interval_s`
   (30s) regardless. This is the whole point of the accelerated clock
   (complete a multi-week fermentation in real seconds) -- not itself a
   bug.
3. `platforms/simulator/plant.py`'s `gravity_at()` -- a plain logistic
   decay curve, `terminal + (og-terminal) / (1 + exp((t_h-midpoint_h)/
   steepness_h))` -- needs a genuinely large *positive* exponent once
   `t_h` gets far enough past `midpoint_h`. `math.exp()` of a large
   positive number overflows (`OverflowError: math range error`).
   Every *other* `math.exp()` call in this same module
   (`advance_chamber_temp`'s exact-solution decay, `exotherm_f_per_h`'s
   Gaussian) has an exponent that's unconditionally <= 0 by
   construction, so extreme inputs only ever safely underflow toward 0
   -- deliberately, per `advance_physics`'s own docstring, which
   documents a prior, similar real incident (a ~1e200°F projected
   temperature that crashed the chart's projection loop). `gravity_at()`
   alone was never given the same treatment.
4. `_control_loop()`'s own resilience ("one bad tick must never
   permanently kill control", by design, so a transient failure
   self-heals next tick) caught the exception, logged it, and moved on
   -- but `clock.sleep()` still runs unconditionally at the end of every
   loop iteration regardless of that tick's success/failure, so `t_h`
   kept growing every single iteration. Since the *same* overflow
   recurs at any `t_h` past the threshold, this specific failure could
   never self-heal the way the resilience mechanism assumes transient
   failures will -- it just retried the identical doomed read as fast
   as the CPU allowed, forever.

**Fixed**: `gravity_at()` rewritten with the standard numerically-stable
sigmoid form (only ever exponentiate whichever side of the midpoint is
non-positive) -- verified via a new `tests/unit/test_simulator_plant.py`
(plant.py had zero direct unit tests before this) to produce identical
output to the original formula for every normal input, and to correctly
saturate at the curve's own asymptotes for extreme `t_h` instead of
raising.

**Deliberately not done, flagged instead of silently expanded into**:
hardening `_control_loop()` itself so a *repeatable* (not transient)
failure can't spin forever regardless of root cause -- e.g. some ceiling
on ticks-per-real-second, or treating N consecutive identical failures
differently from one transient one. The immediate, concrete bug is
fixed and verified; a broader control-loop resilience redesign is a
real, separate design decision worth your input, not something to
fold in unprompted.



Built per your "build both platforms, don't block on questions, document
decisions" instruction, then corrected per your walkthrough feedback on
items #6-#8 below. Everything here is implemented, tested (278 passing
tests, `.venv/bin/pytest`), and verified against your real hardware
(BrewPi Arduino at 192.168.86.208, Tilt Orange) — not just against mocks.
Nothing is blocked on the items below; they're things you may want to
revisit, not open questions holding up the build.

## Architecture correction: the daemon no longer knows any concrete platform exists

Flagged by you, in three escalating rounds, after reviewing the actual
`Daemon`/`DaemonContext` code: it knew too much about Manual/Simulator/
BrewPi/Tilt specifically — first their raw IPC client class, then their
socket paths, finally the concrete classes (`BrewPiConnection`,
`TiltScanner`, IPC connections) and Tilt's `hci_device` config
themselves. Each round went one layer deeper; the final, complete fix:

- **`krauken/platforms/registry.py`** is now the sole owner of
  constructing every platform's concrete state object. `PlatformBinding`
  gained `build_state: Callable[[Clock], Any]` (replacing `state_attr`, a
  ctx-attribute-name string) — a factory the new **`PlatformRegistry`**
  class calls itself, once per enabled platform, at daemon startup.
  `PlatformRegistry` is iterable (yields `PlatformDriver`s for
  `discover()`, so `daemon/discovery.py` needed zero changes),
  `state_for(platform_id)` is how `daemon/drivers.py`'s role-dispatch
  asks for the underlying object by name now, and `start_all()`/
  `stop_all()` generically start/stop whatever was actually constructed
  — with a shared try/except (not special-cased to Tilt the way the old
  code was), a real improvement: every platform's start/stop now gets
  the same "a dependency that's down must never block or crash the
  rest" treatment, not just Tilt's.
- **`krauken/platforms/ipc_driver.py`**'s new `IpcPlatformConnection`
  (base) + `ManualIpcConnection`/`SimulatorIpcConnection` (concrete)
  resolve their OWN socket path from `KRAUKEN_MANUAL_SOCKET`/
  `KRAUKEN_SIMULATOR_SOCKET` (imported defaults from `krauken/config.py`,
  not duplicated) — nothing external ever passes one in during normal
  (zero-arg) construction. An optional override exists only for tests
  that need a specific throwaway socket.
- **`krauken/platforms/brewpi/connection.py`** gained `start()`/`stop()`
  — the "background `identify_and_connect()`, it can take several real
  seconds" logic that used to live in `daemon/app.py` (as
  `Daemon._connect_brewpi()`, with its own tracked task) moved into
  `BrewPiConnection` itself, so `PlatformRegistry` can treat it exactly
  like every other platform: call `.start()`, nothing more.
  `identify_and_connect()`/`close()` themselves are untouched.
- **`krauken/platforms/tilt/scanner.py`**'s `hci_device` is now
  self-resolving (`KRAUKEN_TILT_HCI_DEVICE` + a `DEFAULT_HCI_DEVICE`
  constant that lives in this module now, not `krauken/config.py`, since
  nothing else needs it once the daemon stops threading it through) —
  same explicit-override-for-tests shape as the IPC connections.
- **`krauken/daemon/app.py`** shrank accordingly: `DaemonContext` holds
  only `registry` — no `brewpi_connection`/`tilt_scanner`/
  `manual_connection`/`simulator_connection` attributes, no imports of
  any concrete platform class. `build_daemon()`'s public signature lost
  `simulator_socket`, `manual_socket`, and `tilt_hci_device` entirely.
- Real fallout, not just renames: `krauken/db/seed.py`'s demo-batch
  generator and `tests/scenarios/test_full_fermentation.py`'s four
  scenario tests both called the now-gone `simulator_socket=`/
  `manual_socket=` kwargs — fixed via a scoped env-var override
  (`monkeypatch.setenv` in tests; a save/restore context manager in
  `seed.py`, which isn't test code and has no `monkeypatch` available).
  `tests/api/conftest.py`'s shared `daemon` fixture got the same
  treatment — the only fixture that needed it; everything depending on
  `daemon`/`client` elsewhere needed no changes at all.
- Verified end to end, not just via the 294 passing tests: manually ran
  a scenario daemon with `KRAUKEN_PLATFORMS=brewpi,tilt` and confirmed
  `registry.state_for("manual")`/`state_for("simulator")` come back
  `None` cleanly, `brewpi`/`tilt` still construct and start correctly
  (Tilt's real "no AF_BLUETOOTH on macOS" unavailability caught and
  logged by the new generic `start_all()`, not a crash), and
  start/stop completes with no errors.

## What shipped

- `krauken/platforms/base.py` — new `@requires_optional` decorator for
  graceful missing-dependency handling (was referenced in a stale
  `pyproject.toml` comment but never existed).
- `krauken/platforms/brewpi/{connection,live,platform}.py` — BrewPi
  `ChamberDriver` (+ eligible `BeerTempSource` if a second probe is wired)
  over the real serial protocol, verified byte-for-byte against your
  Arduino.
- `krauken/platforms/tilt/{scanner,live,platform}.py` — Tilt
  `GravitySource`/`BeerTempSource` via raw-HCI BLE scanning, verified
  against your real Tilt Orange (decoded to the exact UUID/temp/gravity
  BrewPi's own web UI showed).
- `krauken/platforms/registry.py`, `krauken/daemon/app.py` — wired both
  into the daemon's existing platform-binding/lifecycle machinery.
- `pyproject.toml`, `deploy/krauken-daemon.service`,
  `deploy/krauken.conf.example` — dependency swap and deployment
  permissions (see below).
- New unit tests across 7 test files, several using real captured hardware
  bytes as golden fixtures rather than synthesized data.

## Decisions made without asking (ratify or override any of these)

### 1. Tilt scans for all 8 colors unconditionally, not a configured one
You corrected this mid-build (initial draft gated scanning behind
`KRAUKEN_TILT_COLORS`) — implemented as: `TiltScanner` always watches for
all 8 known Tilt UUIDs, `discover()` surfaces whichever are actually
detected as separate candidates, same "scan and see what's really there"
philosophy as BrewPi's port auto-scan. `KRAUKEN_TILT_COLORS` config was
removed entirely as a result. **Nothing to ratify here** — this was your
explicit direction, noted for completeness.

### 2. `aioblescan` (raw HCI) instead of `bleak`, in `pyproject.toml`'s `pi` extra
Not a preference — a real finding from live testing. `bleak`'s BlueZ
D-Bus Discovery backend never surfaced the Tilt's advertisement on your
Pi across multiple attempts, despite the Tilt broadcasting and being read
successfully by both your phone and BrewPi Remix's own Tilt manager at
the same time. `aioblescan`'s raw HCI socket — what BrewPi Remix itself
uses — caught it instantly and repeatedly. **Consequence to ratify**: the
daemon process needs `CAP_NET_RAW` (added to
`deploy/krauken-daemon.service` via `AmbientCapabilities=CAP_NET_RAW` —
not full root, and compatible with the existing `NoNewPrivileges=true`).

### 2b. `AF_BLUETOOTH` doesn't exist on macOS — aioblescan is Linux-only despite importing anywhere
Discovered because `Daemon.start()`'s Tilt-scanner startup initially only
caught `PlatformUnavailable`, and this raised a raw `AttributeError`
instead, which briefly looked exactly like the whole daemon/test-suite
hanging (it hadn't — a fixture had crashed, and pytest's cleanup of an
already-broken async fixture was what looked stuck). Fixed by having
`TiltScanner.start()` catch broadly and wrap anything from actually
opening the socket as `PlatformUnavailable`, so a dev Mac (or any non-Linux
box) degrades cleanly instead of crashing daemon startup. Nothing to
ratify — this is a straightforward robustness fix, not a design choice,
but flagged since it came from a real bug during this build.

### 3. BrewPi auto-scans `/dev/ttyACM*`/`/dev/ttyUSB*`, no configured port
Per your earlier answer. `BrewPiPlatform.discover()` tries the
currently-connected port first (unconditionally, even if a fresh
`glob()` doesn't happen to re-list it — an earlier draft only reordered
when it *did* appear in the fresh scan, which would have dropped a
working connection on a transient enumeration gap; fixed during testing),
then every candidate port, identifying via the real `n` (version) command.

### 4. BrewPi's `device_id` is fixed (`"brewpi:controller"`), not derived from the serial port
A port can enumerate under a different path after a reboot; since a
single-chamber install has at most one BrewPi, there's nothing a
port-derived id would need to disambiguate. The actual port is still
recorded in `identity` for display/debugging.

### 5. `set_target()` always uses BrewPi's fridge-constant mode, never beer-constant
`j{mode:"f", fridgeSet:<temp>}` / `j{mode:"o"}` (off) — verified working
against your Arduino, including read-back confirmation that `FridgeSet`
genuinely changed and was restored to your original 76.0°F afterward.
Deliberately never hands the Arduino a beer setpoint and lets its
on-board beer-constant logic compute a second, independent fridge target
— that would be two controllers disagreeing about the same knob, exactly
what the design doc's "compressor protection lives exactly once" rule
already forbids one layer up.

### 6. ~~BrewPi's `probe_temps()` always returns `{}`~~ — **WRONG, corrected**
Original reasoning was that the Arduino's field names ("FridgeTemp"/
"BeerTemp") already unambiguously label each probe. You corrected this:
those names tell you what role a probe plays, but not which *physical*
probe that is when you're the one wiring up a new rig — the exact same
"which wire is actually which" problem a OneWire bus has. Fixed:
`probe_temps()` now returns real per-probe readings keyed by
`brewpi-fridge`/`brewpi-beer` (beer slot only present once actually
wired), and BrewPi devices now offer `identify_probes` in
`available_tests` — same wiggle-test flow every other platform already
had, no new test-runtime mechanism needed since the existing delta-
detection logic already handled 1-probe and 2-probe cases generically.

### 7. Simulated Tilt + real Tilt coexisting — **verified, already true, nothing to build**
This already existed before this session's work and is unaffected by it:
`platforms/simulator/platform.py`'s `SimulatorPlatform.discover()` has
always emitted a `simulator:tilt` device that computes beer temp/gravity
from the same shared `SimPlantEngine` driving the simulated chamber.
Since `simulator` and the new `tilt` platform are both enabled by default
and `discover()` aggregates every platform independently with zero
cross-platform interaction, both a simulated Tilt and any real Tilt(s)
in range show up side by side as separate Hardware Setup candidates.
Added `test_simulated_tilt_and_real_tilt_platform_coexist` to lock this
in as a real regression check rather than leave it as an assumption.

### 8. Multi-Tilt live dispatch — **fixed, not just flagged**
Originally documented as a known limitation (single detected color, first
sorted alphabetically, no way to honor which one was actually mapped).
You asked for the real fix: `hardware_config`'s `device_id` column was
already there, just not threaded through. `daemon/drivers.py`'s three
dispatch functions now accept and forward `device_id`; every driver
constructor across all four platforms accepts it as a second argument
(everything except Tilt ignores it — Manual/Simulator/BrewPi only ever
have one conceptual device per role anyway); `TiltBeerTempSource`/
`TiltGravitySource` now parse the color out of a real `device_id` like
`"tilt:purple"` and read exactly that one, falling back to
first-detected only when no device_id is given at all (e.g. a bare
`TiltBeerTempSource(scanner)` construction with no mapping context).
Verified end-to-end with two simultaneous fake Tilts through the real
`daemon/drivers.py` dispatch chain, not just at the class level.

### 9. `brewpi.service` (the legacy BrewPi Remix stack) is stopped, not restarted
Per your answer that full replacement is the end goal. Left stopped
rather than restarted after testing, since we're still actively iterating
against the same serial port.

### 10. Private asyncio API used for BLE (`event_loop._create_connection_transport`)
Not the documented `create_connection()` — matches BrewPi Remix's own
working `aioblescan` usage on this exact Python version verbatim (their
own code comment: "this used to work but now requires a STREAM socket").
Accepted fragility: could break on a future Python/asyncio version, but
this is no more fragile than the upstream library's own example code, and
was chosen over guessing at a "more correct" modern API that hadn't
actually been proven against your hardware.

## Bugs found and fixed during this build (all covered by tests now)
- BrewPi reconnect logic could drop a working connection on a transient
  port-enumeration gap (see #3).
- `TiltScanner.start()` let non-`PlatformUnavailable` exceptions escape
  and crash daemon startup instead of degrading gracefully (see #2b).
- `TiltScanner`'s packet handler (`.process`) was assigned *after*
  `send_scan_request()` instead of before, matching the reference
  implementation's ordering — advertisements arriving in that window
  would have been silently dropped. Caught during real-hardware testing
  when a 6-second scan window detected nothing; fixed, then confirmed
  detecting the real Tilt within 5 seconds afterward.
