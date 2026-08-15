# Deployment, packaging, and Krauken HAT provisioning: decisions to ratify

Companion to `plans/brewpi-tilt-implementation-decisions.md`. Covers the
deployment-shape conversation (packaging, CI/CD, legacy-BrewPi handling)
and the one piece of it that's actually built and tested so far:
`krauken-hwid`, the Krauken HAT EEPROM provisioning tool.

## Packaging shape (design only — nothing built yet)

- **`.deb` + apt repository**, per your explicit direction over the
  git-clone-and-shell-installer alternative I'd originally proposed.
- **GitHub Actions** builds and signs on tag/release, using encrypted repo
  secrets for the GPG private key (ephemeral — imported into the runner's
  keyring for the signing step, discarded when the job ends).
- **GitHub Pages** hosts the actual apt repo (static `Release`/`Packages`
  file tree) since GitHub Packages has no native apt/`.deb` repository
  type; **GitHub Releases** hosts the raw `.deb` for direct
  download/`dpkg -i` as a simpler fallback path.
- Real architecture wrinkle, not yet resolved: the target hardware is
  `armv6l` (the original Pi/Pi Zero chip) — most GitHub-hosted ARM
  runners and prebuilt cross-build images target ARMv7+/ARM64. Our own
  dependencies are pure Python (`pyserial`, `aioblescan`) so this mostly
  doesn't matter, **except** `lgpio` (a compiled C extension via `swig`).
  Plan: avoid bundling a compiled `lgpio` wheel ourselves — `Depends:` on
  whatever GPIO package Raspberry Pi OS's own repo already ships prebuilt
  for ARMv6, so `apt` resolves it from Raspberry Pi's own archive instead
  of us cross-building it. **Not yet verified**: the exact current package
  name for that.
- **`brewpi.service` handling, per your answer**: NOT auto-disabled.
  Requires an explicit `--replace-brewpi` flag, or an interactive y/n
  prompt if run from a terminal without the flag; refuses and prints
  instructions if run non-interactively with neither. This logic will
  live in the package's `postinst` maintainer script once the `.deb`
  itself exists — likely via `debconf` (Debian's standard
  interactive/preseedable during-install prompt mechanism), not a
  bespoke flag, since that's the idiomatic home for exactly this kind of
  "found something conflicting, what do you want to do" question in a
  proper package. Belt-and-suspenders addition: `Conflicts=brewpi.service`
  in our own systemd unit, so the two can never both be active regardless
  of what the installer decided — a no-op if `brewpi.service` doesn't
  exist on a given box.
- **Ratified**: `CAP_NET_RAW` via `AmbientCapabilities` (from the BrewPi/
  Tilt build) — you confirmed this is fine.

None of the above is built yet — no `debian/` packaging directory, no
Actions workflow, no signing key generated. This section is the agreed
shape to build against, not a status report.

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
