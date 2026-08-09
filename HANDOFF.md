# Handoff briefing — software session

Written 2026-08-08 to hand off from a Claude Code session that was
(mistakenly) running with cwd `/Users/dneumann/Documents/TheKrauken/PCB`
(the hardware/PCB folder) even though all the actual work was software.
This session forked into two: one scoped to this `software/` folder, one
scoped to `PCB/` for hardware work. Paste/point a new session here at the
start so it has this context without replaying the whole prior transcript.

## What this is

The Krauken — a fermentation-controller software stack (Python daemon/API
+ React frontend) for a custom PCB. Design docs live in `plans/`:
`krauken-software-design.md` (architecture) and
`krauken-hardware-role-mapping.md` (the 5-role hardware model: Chamber
Temp/Cooling/Heating bundle + independent Beer Temp + Beer Gravity).

The **implementation plan** being worked from lives outside this repo, at
`/Users/dneumann/.claude/plans/replicated-meandering-dragonfly.md` — read
it for the full current scope. Short version: it covers a round of
frontend bug fixes, a gravity end-criteria semantic change (single
threshold, not a range), edit-running-profile stage gating, Manual/
Simulator hardware-model parity with real BrewPi/Krauken shape, a real
two-probe simulator + wizard identify flow, and a dev-panel expansion
(Tilt/Simulated-controller/Manual-outlets/clock-advance sections). **All
of it is implemented and verified as of this handoff** — see "Status" below.

## Running it locally

`README.md` has the base setup steps. The actual live dev instance this
session was using differs slightly from the README's `/tmp` paths:

```sh
# venv is at software/.venv -- the global pyenv python lacks pytest_asyncio etc.
source .venv/bin/activate

# Daemon and API, same env vars on both:
KRAUKEN_DB_PATH=/Users/dneumann/krauken-dev/krauken.db
KRAUKEN_DAEMON_SOCKET=/Users/dneumann/krauken-dev/daemon.sock
KRAUKEN_SUPERVISOR_SOCKET=/Users/dneumann/krauken-dev/supervisor.sock
KRAUKEN_DEV_PANEL=1              # unlocks /api/v1/dev/* routes AND gives the
                                  # daemon an offsettable clock (see Gotchas)
KRAUKEN_CONTROL_TICK_INTERVAL_S=5  # daemon only -- real default is 30s, 5s
                                    # makes manual testing much less tedious

krauken-daemon     # terminal 1
krauken-api        # terminal 2, same env vars
cd frontend && npm run dev   # terminal 3, vite on :5183, proxies /api -> :8080
```

**Check what's already running before starting anything new** — `ps aux |
grep krauken` — this session left `krauken-daemon`/`krauken-api` running
persistently rather than starting/stopping them per-turn.

## Two ways to view the UI, and why it matters

- `http://localhost:5183` — vite dev server, hot-reloads on every source
  save. Always current.
- `http://localhost:8080` — the API process serving a **separately-copied
  static bundle** at `krauken/api/_static/`, NOT `frontend/dist/` live.
  **This goes stale silently** — after any frontend change meant to be
  visible on :8080, you must rebuild and re-copy:
  ```sh
  cd frontend && npm run build
  cd .. && rm -rf krauken/api/_static/* && cp -r frontend/dist/* krauken/api/_static/
  ```
  Then confirm by diffing the hashed asset filename in `curl -s
  http://localhost:8080/ | grep -o 'assets/index-[^"]*'` against
  `frontend/dist/index.html`. This bit this session twice — check it
  before telling the user something is "deployed."

## Testing

```sh
# Backend -- 151 passing as of this handoff
source .venv/bin/activate && python -m pytest tests/ -q

# Frontend -- 28 passing as of this handoff
cd frontend && npx tsc -b && npx oxlint && npx vitest run
```

Backend processes **do not hot-reload** — any daemon/API source change
needs both processes killed and restarted with the same env vars above.
Schema/migration changes additionally need
`krauken-db --db ~/krauken-dev/krauken.db reset && krauken-db --db
~/krauken-dev/krauken.db seed-demo`, then hardware re-scan + re-map.

## Live dev-environment state as of this handoff

- `krauken-daemon`/`krauken-api` running (`KRAUKEN_DEV_PANEL=1`,
  `KRAUKEN_CONTROL_TICK_INTERVAL_S=5`), vite dev server running on :5183.
- DB was reset clean + reseeded with the demo batch at the user's request
  right before this handoff (fresh schema, one completed demo
  fermentation, no test fermentations).
- Hardware is currently mapped to **Manual** (chamber_temp/cooling/heating
  → Manual chamber controller; beer_temp/beer_gravity → Manual Tilt) — the
  user re-mapped it since the reset, presumably continuing manual-hardware
  testing.
- No active fermentation right now.
- The :8080 static bundle matches the current source (hashes verified
  equal at last check) — but re-verify per the gotcha above if any
  frontend file has changed since.

## Established working patterns (the user has reinforced these repeatedly)

- **Root-cause everything by reading actual code — never guess.** Every
  bug this session was diagnosed by reading real source/logs/live API
  responses and cited with file:line, before proposing any fix.
- **Plan mode for feedback rounds**: the user dumps a batch of feedback
  items with an explicit "don't change anything yet"; investigate each
  read-only, report findings honestly, and only consolidate into a plan
  (or start fixing) once they explicitly say to.
- **Never delete `fermentations`/`fermentation_stages` rows via direct SQL
  against a live daemon without terminating via the real API endpoint
  first** — a direct delete under an active fermentation leaves the
  daemon with a dangling in-memory reference. `devices`/`samples` rows are
  lower-risk caches, but prefer defensive query fixes over manual cleanup
  where possible (see the reused-id bug below).
- **After a DB reset, hardware must be re-scanned and re-mapped** — the
  reset wipes the `devices` table and the role mapping.
- **Definition of Done rubric** (user's global CLAUDE.md): for any
  non-trivial deliverable, enumerate 5-10 real quality characteristics,
  self-rate 1-10 against each, revise anything under 8, and show the
  rubric alongside the finished work.
- **Verify live, not just in code.** This session's standard closing move
  for any fix was: curl the real running API to confirm the data is
  right, then a Playwright screenshot against the real running frontend
  (dev server or built bundle) to confirm it renders right — not just
  "tests pass."

## Known gotchas / accepted simplifications (documented in code, not bugs)

- **`fermentations.id` is a plain `INTEGER PRIMARY KEY`, not
  `AUTOINCREMENT`** — SQLite reuses an id once its row is gone. The app's
  own DB connection has `PRAGMA foreign_keys=ON` (`db/connection.py`), so
  deletes through it cascade properly, but an ad hoc direct-`sqlite3`-CLI
  cleanup against the db file does NOT have that pragma set, so it can
  leave orphaned `samples`/`fermentation_stages` rows that a later,
  id-reusing fermentation silently inherits. This actually happened and
  caused real chart bugs (bogus "NOW" position, wrong projection anchor)
  — fixed defensively in `db/queries.py` (`ts >= started_at` guards on
  `latest_sample`/`fermentation_series`), but the root cause (id reuse
  itself) is still there. Be careful with any future direct-SQL cleanup.
- **`_compute_projection()` in `db/queries.py` anchors "now" on the last
  real sample's own timestamp, not wall-clock time** — deliberate, because
  it runs in the API process (via `deps.run_ro`), which has no handle on
  the daemon's `Clock`. Don't "fix" this back to `datetime.now()`.
- **`OffsettableSystemClock`** (`contracts/clock.py`) backs the dev
  panel's clock-advance feature (`+15min`/`+6h`/`+1d` buttons). It only
  applies when the **daemon** process itself is started with
  `KRAUKEN_DEV_PANEL=1` (not just the API). Its offset is pure in-memory
  state, reset to zero on daemon restart, and grows monotonically with
  every advance-click — nothing currently resets it except a restart.
  Anything that reads real wall-clock time directly instead of going
  through this clock will disagree with it, which caused at least two
  real bugs this session (a wizard countdown, and the projection anchor
  above) — audit any new backend timing code for this.
  Also: on a *very* large single jump, the simulator's Euler-integration
  physics can show a slightly distorted temperature step right after —
  known and accepted (user's explicit call), not something to "fix" by
  chunking the jump into substeps.
- **`beer_temp_source(ctx, platform)`/`gravity_source(ctx, platform)` in
  `daemon/drivers.py` resolve by platform string, not device_id** — so
  Manual's probe-2 (on `manual:chamber`) and `manual:tilt` both resolve to
  the same `ManualBeerTempSource`, reading `panel.tilt.temp_f` either way.
  Deliberate simplification, documented in a docstring, mirrors an
  identical pre-existing ambiguity in Simulator. `probe2_temp_f` is real
  state used only by the identify_probes wiggle-test and dev-panel
  display — not actually wired into the control loop's beer_temp
  resolution. Flagged as a real scope boundary, not an oversight.

## Resolved loose end (was open at handoff time, closed 2026-08-08)

The user was asked whether they remembered a design decision about
**auto-detecting the real Krauken hardware** (as opposed to BrewPi, whose
mechanism — serial port enumeration + version/ID handshake — is written
down in `plans/krauken-hardware-role-mapping.md` §5). No prior decision
was found recorded anywhere (design docs, codebase, memory files) — it
turned out there wasn't one yet, not that one had been lost.

**Confirmed with the user directly**: Krauken discovery = the Hardware
Supervisor's Unix socket existing and responding to a handshake, mirroring
BrewPi's serial+handshake pattern. Now written down in
`plans/krauken-hardware-role-mapping.md` §5. `platforms/krauken/` is
still an empty stub — this is a design decision to build against, not
an implementation yet.

## Not yet built

`platforms/krauken/`, `platforms/brewpi/`, `platforms/onewire/`,
`platforms/tilt/` are all empty stubs — only `manual/` and `simulator/`
are implemented. That's expected for the current milestone (per
`krauken-software-design.md`'s non-goals), not a gap to silently fill in.
