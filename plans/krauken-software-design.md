# The Krauken — Software Design Document

**Scope:** the backend/control layer — control daemon, hardware supervisor, storage, and the web/API tier's contract with them. Frontend visual/interaction design is handled separately; this document defines what that frontend talks to.

**Out of scope for this iteration** (see Section 10): multi-chamber support, smart-plug and generic-sensor hardware platforms, authentication, hydrometers other than Tilt.

---

## 1. Goals and non-goals

The Krauken drives the temperature of a fermentation chamber against a user-defined profile, logs fermentation data (temps, gravity), and exposes current and historical state to a web UI. It must run today on a Raspberry Pi against BrewPi (Arduino) hardware the user already owns, and against The Krauken hardware once built, and it must be safely giveable to other BrewPi owners as better software for hardware they already have.

Non-goals for this iteration: managing more than one fermentation chamber per install; supporting hardware beyond BrewPi and The Krauken; any authentication (trusted-LAN, no login); smart plugs or a-la-carte sensor mixing (the role-mapping model is designed to add these later without rework, but they are not built now).

---

## 2. Process architecture

Four processes, chosen specifically so that a bug in the most complex, most frequently-changing code (the control daemon) can never leave an actuator in an unsafe state for longer than a heartbeat timeout.

```
React frontend  →  Web/API tier  →  Control daemon  →  Hardware Supervisor  →  relays/OneWire (Krauken only)
                         ↓                  ↓                                  BrewPi talks to Control daemon directly over serial
                      SQLite  ←──────────────┘
```

- **Control daemon** (Python) — owns the beer-temp cascade, fermentation profile evaluation, gravity-phase logic, overrides, fault policy, and all writes to SQLite. Talks to hardware through the `ChamberDriver` / `BeerTempSource` / `GravitySource` contracts (Section 4). This is where nearly all business logic and nearly all bug risk lives, and it is designed to be crash-and-restart-safe (Section 7).
- **Hardware Supervisor** (Python, Krauken only) — a separate, deliberately minimal process that owns raw GPIO and OneWire access plus the compressor-protection timers (min-on, min-off, anti-short-cycle) and the cool/heat interlock. It is the software equivalent of what BrewPi gets for free from being a physically separate microcontroller: if the control daemon crashes, the supervisor keeps enforcing protection and holding its last commanded target, because it has no complex logic to crash. BrewPi does not need this process — the Arduino already provides the same isolation over the serial link.
- **Web/API tier** (Python, same stack as the daemon) — the only process the frontend talks to. Reads history directly from SQLite; forwards commands (activate profile, set/clear override, etc.) to the control daemon; has no business logic of its own.
- **SQLite (WAL mode)** — single-writer (the control daemon), multi-reader (the web tier), holds history, fermentation/profile config, and hardware role mapping.

### Why the read/write split

The web tier never asks the daemon for historical data. History is read straight from SQLite. This keeps chart/history queries from ever competing with, blocking, or crashing the real-time control loop, and it means past fermentations stay viewable even while the daemon is mid-restart. Only live state and commands go through the daemon.

### Local IPC

Both the web-tier↔daemon and daemon↔supervisor links use a **Unix domain socket carrying JSON request/response**. Local-only by filesystem permission, no TCP port to accidentally expose, trivially testable with `curl --unix-socket` or a raw socket client in test code.

---

## 3. Data model (SQLite, WAL mode)

Indicative schema — exact column types are implementation detail, but the shape below is load-bearing for the rest of the doc.

```
fermentations
  id, name, profile_id, started_at, ended_at (nullable), simulated (bool), notes

profiles
  id, name, definition (JSON: ordered phases — see Section 8)

samples
  id, fermentation_id, ts, beer_temp, chamber_temp, gravity (nullable),
  chamber_mode ('cool'|'heat'|'idle'), effective_target, beer_temp_ok (bool)

events
  id, fermentation_id, ts, type, payload (JSON)
  -- types: phase_started, phase_advanced (+ reason: time|gravity|manual),
  --        override_set, override_cleared, fault_raised, fault_cleared,
  --        beer_temp_lost, beer_temp_recovered

hardware_config
  id, role ('chamber_temp'|'chamber_cooling'|'chamber_heating'|'beer_temp'|'beer_gravity'),
  platform ('brewpi'|'krauken'|'tilt'|'manual'|'simulator'), platform_config (JSON)
```

`samples` are written on change, not on a fixed cadence — see Section 9 for the exact write policy. Every row carries its own timestamp; nothing downstream (charts, duty-cycle math) may assume even spacing.

---

## 4. Core interfaces

These are the contracts the control daemon programs against. Driver implementations are swapped underneath; the daemon's cascade logic is written once against these three interfaces regardless of which hardware is active.

```python
class ChamberDriver(Protocol):
    def read_chamber(self) -> ChamberReading:  # {temp, mode: cool|heat|idle, health}
        ...
    def set_target(self, temp: float | None) -> None:  # None = idle
        ...

class BeerTempSource(Protocol):
    def read(self) -> BeerReading:  # {temp, health, last_good_ts}
        ...

class GravitySource(Protocol):
    def read(self) -> GravityReading:  # {gravity, health, last_good_ts}
        ...
```

`ChamberDriver` is the only bidirectional one — it senses **and** actuates, because on both real platforms the same device does both. `BeerTempSource` and `GravitySource` are pure reads: there is no way to set beer temp, only to chase it by moving the chamber.

Each reading's `health` is what everything else in this doc keys off — the beer-temp fail-safe (Section 7), fault events, and the evaluator's fault-injection assertions all just watch these fields rather than each having their own detection logic.

### Two `ChamberDriver` families

- **Integrated (BrewPi)** — the Arduino owns its own thermostat and compressor protection; the driver is a thin serial wrapper (`set_target` → fridge-constant setpoint; `read_chamber` → poll temp + protection state back over serial).
- **Composed (The Krauken)** — no onboard intelligence in the relays/probes themselves. The daemon's `ChamberDriver` implementation for Krauken is a thin RPC client to the **Hardware Supervisor** process (Section 2), which is where the thermostat, compressor protection, and cool/heat interlock actually live.

Compressor protection is provided exactly once per platform, at whichever tier owns the switching decision, and the tiers are never stacked. This is why BrewPi is never driven as a bare switch underneath a daemon-side controller (Section 4 of the earlier architecture discussion) — that would duplicate protection and let the two disagree about state.

### Beer temp: no fallback chain

Beer Temp is a single, user-assigned source (BrewPi, Tilt, or Krauken's wired beer probe — whichever the setup flow assigned; see Section 6). There is no priority ordering and nothing to fail over to. Loss of the assigned source is handled entirely by the fail-safe policy in Section 7, not by switching sources.

---

## 5. Hardware platforms in scope

Only four drivers are built in this iteration:

- **BrewPi** — `ChamberDriver` over serial (pyserial), fridge-constant mode, existing Arduino firmware unmodified. Also eligible as an independent `BeerTempSource` if a second probe is wired (not the case on the reference rig, which has chamber-only).
- **The Krauken** — `ChamberDriver` via the Hardware Supervisor (GPIO relays + OneWire, thermostat + compressor protection implemented once in the supervisor). Also eligible as an independent `BeerTempSource` if a beer probe is wired.
- **Tilt** — `GravitySource` and, independently, an eligible `BeerTempSource`. Read directly over BLE in the control daemon (via `bleak`/BlueZ) — no separate bridge process. "Tilt dropped out" is detected as no beacon seen within a timeout, which feeds the same `health`/`last_good_ts` shape as everything else.
- **Manual** (test) — a `ChamberDriver` + `BeerTempSource` + `GravitySource` backed by operator-settable values, exposed as a dev panel in the same web app. Sensor readings are whatever the operator sets; `set_target` calls are recorded but otherwise inert.
- **Simulator** (test) — see Section 11.

Not built now, but the interfaces above are shaped so they slot in later without changing the daemon: generic OneWire chamber/beer temp sensors, smart plugs as independent `Switch` implementations under the composed `ChamberDriver` path, and additional hydrometers as independent `BeerTempSource`/`GravitySource` implementations.

---

## 6. Hardware role mapping

Full rules are in the companion document, `krauken-hardware-role-mapping.md` — summarized here for completeness:

- Five roles: Chamber Temp, Chamber Cooling (required), Chamber Heating (optional), Beer Temp (required), Beer Gravity (optional).
- BrewPi and The Krauken each bundle Chamber Temp + Chamber Cooling + Chamber Heating as an all-or-nothing unit — the only bundling in the system, justified solely by compressor protection needing to stay with its own switching.
- Beer Temp and Beer Gravity are always independent, single-platform picks, never auto-set by a chamber selection, never bundled with each other even when one device (Tilt) is capable of both.
- A config with Chamber Temp, Chamber Cooling, or Beer Temp unfilled cannot be saved.
- `hardware_config` (Section 3) is the persisted result of this mapping; it's what the daemon reads on startup to construct its driver instances.

---

## 7. Control loop

### The cascade

Beer temp (from `BeerTempSource`) is the controlled variable; chamber temp (via `ChamberDriver`) is the manipulated variable. The daemon computes an effective beer target from the active profile phase (Section 8) and any active override (Section 8.3), then drives the chamber toward whatever setpoint keeps beer temp converging on that target. The cascade is uniform across both hardware platforms — it sits entirely above `ChamberDriver` and never knows which platform is underneath.

### Beer-temp loss: fail-safe policy

Because there is no fallback source, loss of Beer Temp is handled as an explicit control-mode change rather than left as an unhandled fault:

1. The daemon tracks time-since-last-good `BeerTempSource` reading continuously.
2. A single missed read is not a failure — only when that duration exceeds a threshold (default 15 minutes, configurable, not hardcoded) does the daemon declare **beer-temp-lost**.
3. On beer-temp-lost: the daemon sets the chamber target directly to the current beer *target* temperature (not the last-held chamber setpoint), and logs `beer_temp_lost`. Rationale: driving toward the intended beer target is safer than continuing to hold whatever chamber setpoint was in effect, which may be intentionally well beyond the beer target (e.g., an aggressive cold-crash setpoint) and would actively harm the beer if held blind.
4. Time-based phase transitions continue to progress normally during beer-temp-lost — there's no reason a sensor outage should also freeze a duration-based hold. Gravity-gated phases are unaffected by this fault (they run off `GravitySource`, a separate reading).
5. On recovery (a good reading returns), the daemon resumes normal cascade control immediately and logs `beer_temp_recovered`. No manual re-arm required.

### Chamber controller (composed path only)

Lives in the Hardware Supervisor for Krauken. Responsibilities: hysteresis/deadband thermostat around the commanded chamber target, minimum on-time / minimum off-time / anti-short-cycle protection for the compressor, and a cool/heat interlock that never energizes both simultaneously and keeps a deadband wide enough to prevent oscillation between them. Exposes `ChamberDriver` upward to the control daemon over the local socket; drives raw relay/OneWire primitives downward.

### Supervisor liveness

If the control daemon stops sending heartbeats/commands, the supervisor holds its last commanded target — that state is already safe — and does not attempt any beer-temp-aware logic of its own (it has no concept of beer temp; that reasoning belongs entirely to the daemon). A prolonged silence from the daemon is its own alertable condition, distinct from beer-temp-lost.

---

## 8. Fermentation profiles

A profile is an ordered list of phases. Each phase has a target beer temp and an exit condition.

### 8.1 Exit conditions

- **Time-based**: hold for N hours/days.
- **Gravity-based**: gravity within `[Y, Z]` and stable for `X` hours, where "stable" is defined against a smoothed reading (rolling median or EMA — Tilt readings are noisy) as drift below a threshold `ε` over a trailing window of `X` hours. Gravity-based conditions require:
  - an optional **min-time floor** (don't advance before N hours even if it looks stable — guards against false plateaus during lag phase), and
  - a **mandatory max-time cap** (advance no later than day N regardless — guards against stuck fermentations and sensor loss; the setup/authoring flow should refuse to save a gravity-gated phase without one).
- **Manual advance**: an operator-triggered "advance now" from the UI, valid on any phase, logged the same as an automatic transition (`phase_advanced` with `reason: manual`).

### 8.2 Gravity-source loss during a gravity-gated phase

If a phase is gravity-gated and `GravitySource` health degrades, the daemon does not advance on stale data — it holds the current setpoint, raises a fault, and falls through to the phase's max-time cap if the outage persists. This is the reason the cap is mandatory rather than merely recommended: it's the degradation path, not just a backstop against a stuck ferment.

### 8.3 Overrides

An override has a **target**: `beer` (replace the effective beer setpoint; the cascade keeps running above it) or `chamber` (bypass the cascade, command the chamber directly). `effective_target = override if present else profile(now)`. Override set/clear are first-class logged events, which is what lets the evaluator (Section 11) assert that an override actually behaved as intended and reverted cleanly.

---

## 9. Sampling and storage

The control loop evaluates at a fast, fixed interval (default 1 minute, configurable) regardless of storage policy — control needs fresh readings whether or not anything gets written. A `samples` row is persisted when any of the following is true:

- a tracked value (beer temp, chamber temp, gravity) moves beyond a small threshold since the last stored row,
- chamber mode changes (cool/heat/idle transition),
- a discrete event occurs in the same tick (override set/cleared, phase transition, fault raised/cleared), or
- a heartbeat interval elapses with no other reason to write (default 15–30 minutes) — so a gap in stored data always means the daemon actually stopped, never "nothing changed."

Because rows are variable-interval, all downstream consumers (duty-cycle calculations, chart rendering) must use each row's own timestamp rather than assuming even spacing — this is a note for the frontend session as well as for the daemon.

---

## 10. Crash and restart behavior

- The control daemon runs as a `systemd` unit with `Restart=on-failure`, a `RestartSec` backoff, and `StartLimitIntervalSec`/`StartLimitBurst` to cap restart attempts — beyond that limit systemd marks the unit `failed` and stops retrying, which is the condition that should raise an operator-visible alert (a daemon that's down is a very different situation from one that's running).
- On startup, the daemon reconstructs live state entirely from SQLite: which fermentation is active, which phase, phase-start timestamp, any active override. There is no in-memory-only state that fermentation control depends on across a restart.
- The Hardware Supervisor (Krauken only) runs as its own `systemd` unit, independently restartable, and is what keeps holding a safe target during any control-daemon downtime (Section 7) — this is the isolation boundary that gives Krauken the same crash-blast-radius property BrewPi gets for free from being a separate microcontroller.

---

## 11. Testability: Manual and Simulator drivers, scenarios, evaluator

**Injectable clock.** The control loop reads "now" from a `Clock` abstraction, never wall-clock time directly — real clock in production, an advanceable clock in tests. Without this, compressing a multi-week fermentation into a fast test run is impossible, so it's treated as a hard requirement rather than a nice-to-have.

**Manual driver** — operator-settable beer temp, chamber temp, and optional gravity, exposed as a dev panel in the same web app (not a separate UI), so the UI code under test is the same code real users see.

**Simulator driver** — a thermal + gravity plant model (`SimPlant`) that evolves beer/chamber temp and gravity over compressed time in response to the chamber targets the daemon commands. It exposes three thin adapter faces — `ChamberDriver`, `BeerTempSource`, `GravitySource` — all reading the same underlying coupled model, mirroring how the real Tilt and BrewPi each serve more than one interface from one physical reader. Includes:
  - a first-order thermal model (beer tracks chamber through a heat-transfer coefficient, plus ambient leak) with a fermentation-exotherm term so active fermentation makes the beer run warmer than the chamber, stressing the controller's ability to cool against real load,
  - a gravity-attenuation curve (lag → rapid attenuation → terminal plateau) tied to the same exotherm term, with injectable pathologies: false-plateau-then-resumes, stuck-fermentation-never-in-range, and Tilt-drops-out-mid-phase.

**Scenarios** are declarative: profile + plant parameters + injected events (an override at time T, a sensor fault at time T) + a compression factor + assertions. A simulator run produces a real, persisted fermentation record flagged `simulated`, which serves three purposes from one artifact: the evaluator checks it, it renders in the UI like any other fermentation (flagged so it's distinguishable), and it's eyeball-checkable on a chart.

**Evaluator** is a function over a recorded sample/event series plus a scenario's assertions — e.g., beer held within a tolerance during steady phases after a settling window; an override's effective target and beer response tracked correctly and reverted cleanly; a gravity-gated phase advanced within its expected window and not before its min-floor or after its max-cap; commanded chamber targets never left safety bounds; the beer-temp-lost fail-safe engaged within the 15-minute threshold and drove toward the beer target rather than holding the prior setpoint. The valuable output is not just pass/fail but the first timestamp where behavior deviated, so a regression points directly at when and what.

---

## 12. Explicitly deferred (not this iteration)

- **Multi-chamber support.** Everything above assumes one daemon, one chamber. Revisit by introducing a chamber/fermentation identifier threaded through the interfaces and schema if this becomes needed — deliberately not built now to avoid speculative complexity.
- **Smart plugs and generic OneWire sensors** as independent `Switch`/`TempSensor` implementations under the composed `ChamberDriver` path. The interfaces are already shaped to accept these later without touching the daemon's cascade logic.
- **Additional hydrometers** beyond Tilt, as independent `BeerTempSource`/`GravitySource` implementations.
- **Authentication.** Trusted-LAN, no login, for this iteration.
- **Exact BrewPi serial protocol bytes** (fridge-constant mode, temp queries) — verify against the actual firmware during driver implementation; this is a driver-level detail, not an architectural one.
DOCEOF
wc -l /mnt/user-data/outputs/krauken-software-design.md