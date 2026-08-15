# Deployment, packaging, and Krauken HAT provisioning: decisions to ratify

Companion to `plans/brewpi-tilt-implementation-decisions.md`. Covers the
deployment-shape conversation (packaging, CI/CD, legacy-BrewPi handling),
`krauken-hwid` (the Krauken HAT EEPROM provisioning tool), and now the
actual `.deb`/Actions/Pages pipeline itself.

## Both release blockers now resolved

1. **Repo visibility.** Per your call, made public:
   `gh repo edit --visibility public --accept-visibility-change-consequences`,
   confirmed via `gh repo view --json visibility` → `PUBLIC`. GitHub Pages
   enabled with Actions as the build source (`gh api -X POST
   repos/dneumann-carica/krauken/pages -f "build_type=workflow"`), live at
   `https://dneumann-carica.github.io/krauken/`. The publish script and
   workflow now bake this real URL into the generated `index.html` and
   sources.list snippet instead of a placeholder (computed in the workflow
   from `github.repository_owner`/`github.event.repository.name`, so a
   repo rename/transfer can't silently make it wrong).
2. **GPG signing key.** Per your call ("generate one now"), generated a
   fresh passphrase-less RSA 4096 keypair (passphrase-less is standard
   practice for CI-signing keys — the repo secret's confidentiality is the
   actual security boundary, not an interactive unlock) in an isolated,
   short-lived `GNUPGHOME`, exported the private key straight into
   `gh secret set KRAUKEN_GPG_PRIVATE_KEY` and the key ID into
   `KRAUKEN_GPG_KEY_ID` (both confirmed present via `gh secret list`), then
   destroyed the temporary keyring — no private-key material persists
   anywhere outside that one GitHub repo secret. Public key fingerprint,
   for your own records/verification:
   `4929 5A61 2171 3934 2318 750C 9ABE 1A1D C0A3 08B2` (key ID
   `9ABE1A1DC0A308B2`). The `.deb` consumer never needs this file
   separately — the workflow re-exports the public key from the secret at
   every build (`.github/workflows/build-deb.yml`'s "Export the public
   key" step) and publishes it alongside the repo as
   `krauken-archive-keyring.asc`.

## Verified end-to-end with a real `v0.1.0` release

Pushed the tag and watched `build-deb.yml` run for real (`gh run watch`).
Took three iterations to go green — all three were genuine bugs, not
CI flakiness, each fixed and the tag re-pushed pointing at the fix:

1. **`Unmet build dependencies: nodejs (>= 18) npm`.** `actions/setup-node`
   only puts Node on `PATH` via its tool cache; `dpkg-checkbuilddeps`
   consults dpkg's own installed-package database, which never heard
   about it. Fixed: also `apt-get install nodejs npm` alongside the other
   packaging tools (setup-node's version still wins on `PATH` for the
   actual frontend build).
2. **Two real, pre-existing test bugs**, both invisible until the suite
   ran against a genuinely fresh checkout for the first time:
   - `debian/rules`' `override_dh_auto_test` installed `.[dev]` only, not
     `.[dev,pi]` — `pyserial` wasn't importable, so 6 BrewPi platform
     tests failed with `ModuleNotFoundError` instead of exercising the
     real fake-serial-backed path. Fixed by installing `.[dev,pi]` (all
     three extras build/import cleanly on x86_64; `lgpio` only touches
     real hardware lazily, at instantiation).
   - `test_speed_and_advance_routes_no_longer_exist` asserted the response
     content-type excludes `application/json` — true only because this
     dev machine has a stale `frontend/dist` → `_static` build lying
     around. In a real fresh checkout (any CI run; specifically
     `debian/rules`' test step, which runs before `override_dh_auto_install`
     stages `_static`), `static.py`'s own fallback correctly returns a
     harmless JSON 404 — the test conflated "route retired" with
     "frontend happens to be built." Fixed to assert the actual invariant
     (no old speed-panel JSON payload) regardless of which shape wins.
3. **`Tag "v0.1.0" is not allowed to deploy to github-pages due to
   environment protection rules.`** Enabling Pages auto-creates a
   `github-pages` environment with a deployment branch policy that, by
   default, allows only `main` — no tag refs at all, so the tag-triggered
   `sign-and-publish` job's `deploy-pages` step was rejected outright.
   Fixed via the API, not the UI: added a `v*` tag policy
   (`POST .../environments/github-pages/deployment-branch-policies`,
   `{name: "v*", type: "tag"}`).

All green on the fourth run. Confirmed live, not just "workflow succeeded":
- `gh release view v0.1.0` → `krauken_0.1.0_all.deb` attached.
- `https://dneumann-carica.github.io/krauken/` serves the real
  `index.html`, with the actual Pages URL (not a placeholder) in both the
  `curl`/`gpg --dearmor` line and the `sources.list` entry.
- `dists/stable/Release` and `krauken-archive-keyring.asc` both resolve
  and contain real, signed content.

**Genuinely still unverified**: nothing has installed this `.deb` on a
real Raspberry Pi yet (the `postinst` venv-creation/piwheels/eeptools-build
path, the `python3-lgpio | python3-rpi-lgpio` dependency name, and the
debconf BrewPi-replacement prompt are all only validated by inspection and
by the x86_64 build-machine test run, not a real install).

## Homepage rewrite + three follow-on product decisions, per your review

Reviewing the live Pages homepage, you asked for six changes -- three are
copy/design, three are real behavior changes:

- **Reused the actual frontend design tokens** (`frontend/src/design/
  tokens/*.css`'s `--kr-*` colors/type/spacing/effects), copied inline
  rather than imported (this page has no bundler step of its own) so it
  doesn't read as a different product from the app it's installing.
  Dropped the squid emoji -- just the wordmark now.
- **Install is now 3 explained steps**, not one opaque block: trust the
  key (and why -- proves genuine vs. impostor), add the repository (and
  why -- registers the source, trusts only that key), update+install
  (and why -- re-reads package lists, then actually installs). Exact
  commands unchanged from before, verified byte-for-byte via a local
  substitution test against a real `.deb`.
- **API now defaults to `0.0.0.0`, not `127.0.0.1`.** Real code change,
  not just copy -- `krauken/config.py`'s `DEFAULT_API_HOST`. Per your
  call: the Pi is headless, so a loopback-only default left a fresh
  install unreachable until someone already knew to SSH in and edit a
  config file. Not a new security hole: `api/security.py`'s CSRF-style
  mitigation (required custom header + same-origin CORS on every
  mutating request) was already written assuming LAN reachability --
  this just matches the binding to what that layer already defends
  against. `deploy/krauken.conf.example` now documents
  `KRAUKEN_API_HOST=127.0.0.1` as the commented-out way to lock a given
  install back down. No test asserted the old default; full suite
  (291 tests) still green.
- **Homepage now explains both hardware paths**: BrewPi-compatible
  Arduino (available today) or the Krauken PCB (not released --
  explicitly framed as open-source hardware once it ships, not a
  product anyone will sell).
- **EEPROM tool (`krauken-hwid`/eeptools) build is now gated on a new
  debconf question**, `krauken/has-krauken-hardware` (default false),
  matching the existing `replace-brewpi` pattern -- `krauken.postinst`
  only compiles eeptools if that answer is true.  Unlike
  `replace-brewpi`'s `brewpi.service` detection, there's no runtime
  signal for "do you have a Krauken PCB" (that's exactly what the EEPROM
  this question gates would provision -- chicken-and-egg), so
  `krauken.config` guards the actual `db_input` call behind a hardcoded
  `KRAUKEN_HARDWARE_RELEASED=false` flag rather than asking a question
  nobody could truthfully answer yes to yet. The template's own
  `Default: false` is what silently applies to every install today, so
  the net effect is what you asked for -- "stubbed into the install but
  skipped" -- while leaving the real, working mechanism in place for
  when the flag flips.

Not yet re-verified against a real CI run as of this edit -- next step is
moving the `v0.1.0` tag forward again and confirming the rendered
homepage and the (still-skipped) eeptools gating both behave as
expected.

## Packaging shape — built

- **`.deb` + apt repository**, per your explicit direction over the
  git-clone-and-shell-installer alternative I'd originally proposed.
- **GitHub Actions** builds and signs on tag/release, using encrypted repo
  secrets for the GPG private key (ephemeral — imported into the runner's
  keyring for the signing step, discarded when the job ends).
- **GitHub Pages** hosts the actual apt repo (static `Release`/`Packages`
  file tree) since GitHub Packages has no native apt/`.deb` repository
  type; **GitHub Releases** hosts the raw `.deb` for direct
  download/`dpkg -i` as a simpler fallback path.
- **Architecture wrinkle resolved by design, not by cross-compiling**: the
  target hardware is `armv6l` (original Pi/Pi Zero), and most GitHub-hosted
  ARM runners/cross-build images don't cover it, plus `pydantic-core`
  (pydantic v2's compiled Rust extension -- a transitive dependency, not
  even one we chose directly) would need a real ARMv6 wheel. Solution:
  `Architecture: all` -- the `.deb` ships Python SOURCE, and `postinst`
  creates the venv and runs `pip install` **natively on the target
  device** at install time, not at CI build time. Raspberry Pi OS's own
  `pip` is pre-configured to pull prebuilt wheels from `piwheels.org`
  (the Raspberry Pi community's own ARMv6/armhf/arm64 wheel mirror),
  which correctly resolves for whatever the real device's architecture
  actually is. Trade-off: install needs network access (apt already
  requires this to fetch the `.deb` in the first place, so this isn't a
  new category of requirement, just one more use of it) and takes longer
  than a fully pre-built package would. Same reasoning applied to
  `krauken-hwid`'s `eeptools` dependency: vendored as source (`vendor/
  rpi-utils` submodule, pinned to a specific commit) and compiled via
  `cmake`/`make` in `postinst`, not at package-build time.
- `lgpio`: `Depends: python3-lgpio | python3-rpi-lgpio` (an "either"
  dependency) rather than installing it ourselves via pip -- lets `apt`
  resolve whichever one Raspberry Pi OS's own repo actually provides.
  **Not independently verified**: the exact current package name(s);
  worth confirming against a real Raspberry Pi OS install before trusting
  this at release time.
- **`brewpi.service` handling, per your answer**: NOT auto-disabled.
  `debian/krauken.templates` + `debian/krauken.config` ask a real
  `debconf` question (`krauken/replace-brewpi`) -- but only if
  `brewpi.service` is actually detected on the box in the first place;
  `debian/krauken.postinst` reads the answer back and only stops+
  disables if it was actually accepted. Never uninstalls the underlying
  BrewPi Remix files. Belt-and-suspenders addition, now actually in
  `deploy/krauken-daemon.service`: `Conflicts=brewpi.service` (a no-op if
  that unit doesn't exist on a given box) -- this was only documented as
  a plan last time, not yet added to the real file; it is now.
- **Ratified**: `CAP_NET_RAW` via `AmbientCapabilities` (from the BrewPi/
  Tilt build) — you confirmed this is fine.

Built: `debian/` (control, rules, changelog, postinst/postrm, debconf
templates+config, source format, symlinks into `deploy/` for
`dh_installsystemd`/`dh_installtmpfiles` to auto-discover), `.github/
workflows/build-deb.yml`, `scripts/publish-apt-repo.sh`. See the top of
this doc for the two things still blocking an actual (not just written)
release.

## `krauken-hwid` — built, tested, not yet hardware-verified

Provisions the Krauken HAT's ID EEPROM so Raspberry Pi OS (and the
still-unbuilt `krauken` PlatformDriver) can auto-identify genuine Krauken
hardware via `/proc/device-tree/hat/*` — the same "auto-discover, don't
ask the user" philosophy as BrewPi's serial auto-scan and Tilt's BLE scan.

**Confirmed you're DIYing the board, not manufacturing/pre-flashing it**
— so this is a genuine end-user setup step, not a factory step. Lives as
its own CLI (`krauken-hwid flash` / `krauken-hwid status`), not folded
into the `.deb`'s silent `postinst` — flashing a physical EEPROM is a
deliberate, one-time, physically-present action, not something that
should happen unattended during a package install.

### What it wraps
`eepmake`/`eepflash.sh`/`eepdump` from `raspberrypi/utils`'s `eeptools`
(BSD-3-Clause — confirmed via the actual repo, not memory; the older
`raspberrypi/hats` repo is deprecated, this is the current home). These
are external C tools, not pip-installable — `krauken-hwid` checks for
them on `PATH` and gives an actionable build-from-source error if
they're missing. **Real follow-up, not done**: vendor + build these as
part of our own `.deb`'s build process instead of requiring a separate
manual build forever.

### EEPROM settings, from the hardware design session's answers
- Chip: Microchip 24AA32A-I/P, standard HAT ID EEPROM convention (I2C
  address 0x50, dedicated ID_SD/ID_SC pins) — no deviation, matches
  `eepflash.sh`'s own defaults. **Caveat carried forward from that
  session, not independently re-verified here**: the pinout was checked
  against a KiCad library symbol, not a datasheet PDF — low-risk (24Cxx
  DIP-8 pinout is about as standardized as through-hole parts get), but
  worth knowing it's not datasheet-verified.
- GPIO map, confirmed against the real schematic (BCM numbering):
  `GPIO4` (1-Wire, input, no pull — a real 4.7k pull-up is already on the
  board), `GPIO17`/`GPIO27` (COOL_CTRL/HEAT_CTRL relay outputs, pulled
  down for fail-safe-off during the boot window), `GPIO22` (EEPROM_WP,
  pulled up — idle/safe state is write-protected). `GPIO0`/`GPIO1`
  (ID_SD/ID_SC) deliberately excluded from this map — those are the
  dedicated ID EEPROM bus itself, not general-purpose pins.
- Format verified against `eeptools`' own real example files
  (`eeprom_v1_settings.txt`) rather than guessed from memory — the exact
  `setgpio GPIO_NUMBER FUNCTION PULL` syntax, confirmed live via the
  actual repo.
- `vendor`/`product`/`product_id`/`product_ver`: **not decided by the
  hardware side, proposed by me** (`"TheKrauken"` / `"Krauken
  Fermentation Controller HAT"` / `0x0001` / `0x0001`) — matches the
  board's actual silkscreen branding, easy to change any time before
  wide deployment since (unlike the GPIO map) this lives entirely in our
  own software, not the physical board. Flag if you want different exact
  strings.
- `product_uuid`: generated fresh per physical unit at flash time
  (`uuid.uuid4()`), never a fixed/shared value — per the direction
  already discussed with the hardware side.

### No physical WP jumper — GPIO22 gates writes instead
Confirmed intentional (discussed with the hardware side specifically so
provisioning could be a pure software step, with no assembly-order
constraint — flashing works on a fully assembled, booted, cased unit,
any time). `krauken-hwid` drives GPIO22 low only for the duration of the
actual flash and **always** restores it high afterward, success or
failure (`_with_wp_unlocked()`'s try/finally) — the one safety-critical
property here, since there's no physical jumper as a second line of
defense. Covered by tests that deliberately fail the write step and
assert the pin still gets relocked.

### A design bug I caught before shipping it, not after
Original draft checked `/proc/device-tree/hat/product` immediately after
flashing to confirm success. That's wrong — Raspberry Pi OS only
refreshes that path on the **next boot** (the HAT device-tree fixup runs
once at boot, not on live EEPROM writes), so that check would have
failed on every single successful flash, not just broken ones. Fixed:
real post-flash verification now reads the EEPROM back via
`eepflash.sh -r` and byte-compares against what was written, still
under WP-unlock protection (a read also needs live i2c access, same as
the write). `/proc/device-tree/hat/*` is still the right check for
`already_flashed()`'s pre-flash idempotency guard, and for `status` —
both of those are legitimately asking "what does this booted OS already
believe," not "did the write I just did work."

### What's genuinely NOT verified yet
Everything above is unit-tested against fakes (`FakeGpioBackend`,
`FakeRunner`) — 13 tests, including the WP-relock-on-failure property.
**None of it has run against a real Krauken board**, because one doesn't
exist yet to test against (`lgpio` itself can't even be installed on a
non-Linux dev machine, and `eepmake`/`eepflash.sh` aren't built anywhere
yet either). The i2c bus number (`-d` flag, defaulted to `0`) is
similarly unconfirmed against the actual target hardware. Once a real
board exists, this needs an actual flash-and-reboot test before trusting
it against a customer's unit.
