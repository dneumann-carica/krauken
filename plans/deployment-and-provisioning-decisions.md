# Deployment, packaging, and Krauken HAT provisioning: decisions to ratify

Companion to `plans/brewpi-tilt-implementation-decisions.md`. Covers the
deployment-shape conversation (packaging, CI/CD, legacy-BrewPi handling),
`krauken-hwid` (the Krauken HAT EEPROM provisioning tool), and now the
actual `.deb`/Actions/Pages pipeline itself.

## Two things blocking a real (not just written) release — need your input

1. **The repo is private** (confirmed via `gh repo view`). `actions/
   deploy-pages` needs GitHub Pages enabled, and Pages on a private repo
   is a paid-plan feature (Free doesn't support it) — I can't tell from
   here whether your account's plan covers this. Options: confirm your
   plan supports it and I'll enable Pages via the API; make the repo
   public; or host the apt repo somewhere other than Pages (a different
   static host, or skip the "add our apt repo" experience and only ship
   the raw `.deb` via GitHub Releases, which needs no Pages at all).
2. **No GPG signing key exists yet.** I didn't generate one unilaterally
   — a production signing key that ends up trusted by everyone who
   installs this is exactly the kind of thing worth your explicit call
   rather than me silently picking parameters. Do you want me to generate
   a fresh keypair now and install it as `KRAUKEN_GPG_PRIVATE_KEY`/
   `KRAUKEN_GPG_KEY_ID` repo secrets (I have the `gh` access to do this
   directly), or do you have an existing key/process you'd rather use?

Nothing else below is blocked on these two — the actual pipeline code is
written, and validated as much as possible without a real Debian/Linux
host: `dpkg-source`/`dpkg-parsechangelog`/`dpkg-buildpackage`'s own
pre-flight checks all pass locally (source format, changelog, control
field syntax), and `actionlint` + `shellcheck` are clean on the workflow
and every shell script. What's NOT yet verified: a full `dpkg-buildpackage`
run (needs `debhelper`, which isn't packaged for macOS -- Docker/Colima
wouldn't start locally either, so this needs a real CI run to confirm),
and obviously the signing/Pages steps themselves, pending the two
decisions above.

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
