# Hardware role mapping — setup logic and constraints

This describes how the setup UI should let a user declare their hardware and how that maps onto the five control roles the daemon needs filled. It's deliberately silent on visual design — that's for the UI session — and focused on the rules that visual design has to enforce.

## 1. The five roles

| Role | Requirement level |
|---|---|
| Chamber Temp | Required — no valid rig without it |
| Chamber Cooling | Required — no valid rig without it |
| Chamber Heating | Optional — cooling-only rigs are valid |
| Beer Temp | Required — no valid rig without it |
| Beer Gravity | Optional — absence only disables gravity-gated phases |

Beer Temp is required for the same reason Chamber Temp is: without it the daemon has no controlled variable to manage fermentation against. There is no degraded fallback mode for a missing Beer Temp — a config that doesn't fill it is not a valid config, full stop.

## 2. Platform catalog (extensible)

Each platform declares which role(s) it can fill. Bundling — one platform auto-filling multiple roles at once — exists in exactly one place: the chamber roles on BrewPi and The Krauken. That's addressed in Section 3. Every other role, including Beer Temp and Beer Gravity, is filled independently, one platform per role, even when the same physical device is capable of filling more than one.

| Platform | Roles it can fill | Bundled? |
|---|---|---|
| BrewPi | Chamber Temp + Chamber Cooling + Chamber Heating, always together. Separately and independently, it can also be the pick for Beer Temp if a second probe is physically wired. | Chamber roles: bundled. Beer Temp: independent pick, not auto-set |
| The Krauken | Chamber Temp + Chamber Cooling + Chamber Heating, always together. Separately and independently, it can also be the pick for Beer Temp if a beer probe is wired. | Chamber roles: bundled. Beer Temp: independent pick, not auto-set |
| Tilt | Independently eligible for Beer Temp. Independently eligible for Beer Gravity. A user can pick Tilt for both, either one, or neither — picking it for one does not set the other. | Not bundled |
| Generic chamber temp sensor (future) | Chamber Temp only | Independent (single-role) |
| Generic beer temp sensor (future) | Beer Temp only | Independent (single-role) |
| Smart plug (future) | Chamber Cooling **or** Chamber Heating — the user declares which purpose when adding the plug, since the plug itself has no idea what it's wired to | Independent, purpose-locked once declared |
| Future hydrometer, temp-capable (e.g. iSpindel-like) | Independently eligible for Beer Temp. Independently eligible for Beer Gravity. Same mix-and-match as Tilt. | Not bundled |
| Future hydrometer, gravity-only | Beer Gravity only | Independent (single-role) |

New platform types should be addable to this table without changing the rules below.

## 3. The bundle rule

Bundling exists for exactly one reason: **so that a single chamber controller owns both the sensing and the compressor-protection switching for cooling and heating, and that protection logic is never split from what it's protecting.** It is not about convenience, and it is not about "one physical device, so why not group its readings" — that reasoning does not apply anywhere else in this catalog, which is why Beer Temp and Beer Gravity are never bundled even when one device (Tilt, a future hydrometer) can supply both.

- BrewPi and The Krauken each declare a chamber role set `R = {Chamber Temp, Chamber Cooling, Chamber Heating}`. Assigning either platform to any role in `R` auto-assigns it to the other roles in `R` that aren't already pinned to something else.
- This bundle is **all-or-nothing** — there is no valid partial state. You cannot keep BrewPi as Chamber Temp while routing Chamber Cooling through a smart plug. Splitting it means two independent things decide when to cool the same fridge: the device's own onboard control loop, and the daemon acting on a separate switch. That's a control hazard, not a style preference, so the system must refuse to save a split state rather than just warn about it.
- Beer Temp is never part of this bundle, on either platform. Choosing BrewPi or The Krauken for a chamber role has no effect on the Beer Temp selection.
- Beer Temp and Beer Gravity are never bundled with each other, on any platform. Picking Tilt (or a future temp-capable hydrometer) for one does not select it for the other.
- All non-chamber roles — Beer Temp, Beer Gravity, and any independent single-role platform — are simply assigned one at a time from whichever eligible platforms the user has. There's no priority order to compute: the user is choosing, not the system, so there's no sense in which one eligible source is "better" than another by default.

### Resolving a broken chamber bundle

If a chamber role that was auto-filled by BrewPi/Krauken gets reassigned to something else while sibling chamber roles are still pinned to it, the UI enters an error state and blocks save until the user does one of:

1. **Revert** — put the changed role back on the same platform.
2. **Fully remove it** — clear every role in `R` off of that platform in one action, then assign each role individually to other platforms.

There is no third option. Make option 2 a single control, not a per-field edit — that's the actual intended path when someone is migrating off Krauken/BrewPi onto independent components.

## 4. How this feeds the daemon

Once the mapping is internally consistent, it resolves mechanically into the daemon's existing interfaces:

- Chamber Temp + Chamber Cooling + Chamber Heating together resolve to one `ChamberDriver` — either an integrated driver (BrewPi) or a composed one (`SoftwareChamberController` wired to whichever `TempSensor` fills Chamber Temp and whichever `Switch`es fill Cooling/Heating).
- If Chamber Heating is unfilled, the resulting `ChamberDriver` simply never issues a heat command — a legitimate cooling-only rig.
- Beer Temp resolves to exactly one `BeerTempSource` implementation — whichever platform the user picked. There is no fallback chain and nothing to fail over to. If that source goes unhealthy at runtime, the daemon has no runner-up to switch to; it's a live fault that needs its own defined behavior (hold last known chamber target and alert is the leading candidate, but the precise handling belongs in the interface-signature pass, not here).
- Beer Gravity, if filled, resolves to a `GravitySource`; if not, `GravitySource` is absent and gravity-gated profile phases fall back to their time caps.

## 5. Discovery vs. validation

Two different setup-flow interactions; every platform driver should be describable as needing one or both:

- **Auto-discoverable** — platforms with an addressable identity you can enumerate without physical interaction: BLE beacons (Tilt, future hydrometers) via scan, BrewPi via serial port enumeration plus a version/ID handshake, future smart plugs via local-network discovery, Krauken via its Hardware Supervisor's Unix socket existing and responding to a handshake (mirrors the BrewPi pattern — see Section 2 of `krauken-software-design.md` for the supervisor process itself; unimplemented as of this writing, `platforms/krauken/` is still an empty stub).
- **Requires manual validation** — anything whose physical identity can't be inferred from its protocol identity. Which OneWire address is the chamber probe versus the beer probe needs a "wiggle test" (warm one probe, confirm which reading moves). Which relay or plug maps to which physical function needs a "fire test" (momentarily energize it, user confirms what just turned on).

For a consistent setup-flow interaction pattern regardless of platform type, every driver should expose:

- `discover() -> [candidates]` — best-effort, may return nothing.
- `identify()/test()` — an explicit, user-initiated, safe action (momentary relay fire, or "which reading just changed") that the UI calls during setup, not during normal operation.

## 6. Error/validation states the UI needs to render

1. **Split chamber bundle** — a chamber role reassigned away from BrewPi/Krauken while sibling chamber roles are still pinned to it. Blocks save; offer revert or full removal (Section 3).
2. **Missing required role** — Chamber Temp, Chamber Cooling, or Beer Temp entirely unfilled. Hard block; the rig cannot run a fermentation.
3. **Unqualified assignment** — a platform assigned to a role it doesn't support (e.g., a gravity-only hydrometer put in the Beer Temp slot).
4. **Optional and unfilled, valid** — Chamber Heating unfilled (cooling-only rig, informational) or Beer Gravity unfilled (informational, gravity-gated phases fall back to time caps). Neither blocks save.
5. **Discovery/validation failure** — a role is assigned to a platform that isn't currently reachable (Tilt out of range, plug offline). Distinct from "unassigned" — this is "assigned but unhealthy" and should read as a live fault, not a setup-time error. For Beer Temp specifically, since there's no fallback, this fault is more consequential than the equivalent for other roles.
