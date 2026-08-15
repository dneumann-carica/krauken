# BrewPi + Tilt implementation: decisions to ratify

Built per your "build both platforms, don't block on questions, document
decisions" instruction, then corrected per your walkthrough feedback on
items #6-#8 below. Everything here is implemented, tested (278 passing
tests, `.venv/bin/pytest`), and verified against your real hardware
(BrewPi Arduino at 192.168.86.208, Tilt Orange) — not just against mocks.
Nothing is blocked on the items below; they're things you may want to
revisit, not open questions holding up the build.

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
