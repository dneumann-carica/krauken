"""krauken-hwid: provisions the Krauken HAT's ID EEPROM so Raspberry Pi OS
(and this project's own `krauken` PlatformDriver, once built) can
auto-identify genuine Krauken hardware via /proc/device-tree/hat/* --
matching the exact same "auto-discover, don't ask the user" philosophy
already used for BrewPi (serial port + version query) and Tilt (BLE scan +
iBeacon UUID).

Wraps three external C tools from raspberrypi/utils's eeptools (BSD-3-
Clause; https://github.com/raspberrypi/utils/tree/master/eeptools):
eepmake, eepflash.sh, eepdump. These are NOT Python dependencies and can't
be pip-installed -- vendoring/building them as part of our own .deb
packaging is a real, still-open follow-up (see the deployment-decisions
doc); until that lands, this module just checks for them on PATH and
gives an actionable error if they're missing. Same "graceful missing
dependency" spirit as platforms/base.py's @requires_optional, just for an
external binary instead of a Python module -- and deliberately NOT reusing
that decorator, since it's shaped for the async platform-driver Protocol
methods elsewhere in this codebase, and there's nothing actually
concurrent or I/O-overlapped about a one-shot provisioning script like
this one.

EEPROM_WP wiring (GPIO22, confirmed against the board's real schematic --
no physical jumper on this design, deliberately, so provisioning could be
a pure software step): idle state is HIGH (write-protected). This module
drives it LOW only for the duration of the actual flash and ALWAYS
restores HIGH afterward, even if the flash step raises -- see
_with_wp_unlocked()'s try/finally. That's the one safety-critical property
here: a write-protected EEPROM left accidentally unlocked is a real risk
on a board with no physical jumper as a second line of defense, so it's
covered by tests that deliberately make the flash step fail and assert
the pin still gets locked again.

Uses lgpio (the modern gpiochip character-device GPIO library, not the
older sysfs-based RPi.GPIO) via a small injectable GpioBackend so the
provisioning LOGIC here is fully unit-testable without real hardware --
lgpio itself is a compiled extension that can't even be installed on a
non-Linux dev machine, let alone exercised against a real EEPROM.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

EEPROM_WP_GPIO = 22
# Confirmed against the board's schematic: 24AA32A-I/P, standard HAT ID
# EEPROM convention (I2C address 0x50, A0/A1/A2 tied to GND) -- no
# deviation, so these match eepflash.sh's own defaults for a standard
# HAT/HAT+ ID EEPROM. -d (i2c bus number) is left configurable since it
# varies by Pi board revision and hasn't been confirmed against the exact
# target hardware -- flag this to verify before relying on the default.
DEFAULT_I2C_BUS = 0
I2C_ADDRESS_HEX = "50"
EEPROM_TYPE = "24c32"

HAT_PRODUCT_PATH = Path("/proc/device-tree/hat/product")
HAT_UUID_PATH = Path("/proc/device-tree/hat/uuid")
TEMPLATE_PATH = Path(__file__).parent / "hat_eeprom_template.txt"

REQUIRED_BINARIES = ("eepmake", "eepflash.sh", "eepdump")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class HwidError(Exception):
    pass


class GpioBackend(Protocol):
    def claim_output(self, gpio: int, level: int) -> None: ...
    def write(self, gpio: int, level: int) -> None: ...
    def close(self) -> None: ...


class LgpioBackend:
    """The real backend -- lazily imports lgpio so this module still
    imports cleanly on a dev machine without the `pi` extra (only
    constructing this class, i.e. only actually running `flash`, needs
    lgpio importable)."""

    def __init__(self, chip: int = 0):
        try:
            import lgpio
        except ImportError as exc:
            raise HwidError(
                "lgpio is not installed -- run `pip install -e '.[pi]'` to enable EEPROM flashing"
            ) from exc
        self._lgpio = lgpio
        self._chip = lgpio.gpiochip_open(chip)

    def claim_output(self, gpio: int, level: int) -> None:
        self._lgpio.gpio_claim_output(self._chip, gpio, level)

    def write(self, gpio: int, level: int) -> None:
        self._lgpio.gpio_write(self._chip, gpio, level)

    def close(self) -> None:
        self._lgpio.gpiochip_close(self._chip)


def check_binaries_present() -> None:
    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        raise HwidError(
            f"Missing required tool(s) on PATH: {', '.join(missing)}. These come from "
            "raspberrypi/utils's eeptools (BSD-3-Clause) -- build them from "
            "https://github.com/raspberrypi/utils/tree/master/eeptools "
            "(cmake, make, sudo make install) until they're bundled into our own .deb packaging."
        )


def already_flashed() -> str | None:
    """Returns the currently-flashed product string, or None if this
    board's EEPROM has never been provisioned. Reads the SAME device-tree
    path Raspberry Pi OS itself populates from the EEPROM at boot -- if
    it's set, a real, valid EEPROM is already in place, not just "we ran
    eepflash once and hope it worked"."""
    if HAT_PRODUCT_PATH.exists():
        return HAT_PRODUCT_PATH.read_text().rstrip("\x00").strip()
    return None


def render_settings(product_uuid: str) -> str:
    return TEMPLATE_PATH.read_text().format(product_uuid=product_uuid)


def _with_wp_unlocked(gpio: GpioBackend, fn: Callable[[], Any]) -> Any:
    """Drives EEPROM_WP low, runs fn(), then ALWAYS restores it high --
    even if fn() raises. Ordering matters: claim as output already LOW
    (unlocked) in one call (avoids a brief unprotected-then-locked glitch
    from claiming high then writing low), run the risky part under
    try/finally, restore high, and only release the GPIO chip handle in
    an outer finally so a failure to restore-high still gets a real
    exception surfaced rather than being swallowed by cleanup."""
    gpio.claim_output(EEPROM_WP_GPIO, 0)  # unlock
    try:
        return fn()
    finally:
        gpio.write(EEPROM_WP_GPIO, 1)  # always re-lock, success or failure


def _run(runner: Runner, args: list[str]) -> "subprocess.CompletedProcess[str]":
    result = runner(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise HwidError(f"{args[0]} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def flash(
    *,
    force: bool = False,
    i2c_bus: int = DEFAULT_I2C_BUS,
    gpio_backend_factory: Callable[[], GpioBackend] | None = None,
    runner: Runner | None = None,
    uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str:
    """Returns the UUID that was flashed. Raises HwidError on any failure
    -- including "already flashed" (unless force=True), missing tools, or
    an eepmake/eepflash.sh/eepdump failure.

    runner defaults to None (not `subprocess.run` directly) and is
    resolved to the module's current `subprocess.run` INSIDE the function
    body, not as a bound default value -- a default argument expression is
    evaluated exactly once, at module-import time, so binding straight to
    subprocess.run here would capture that one reference permanently;
    monkeypatching subprocess.run afterward (as main()'s own tests do,
    since its CLI surface has no --runner override for real users to set)
    would then silently have no effect on any caller that omits `runner=`."""
    if runner is None:
        runner = subprocess.run
    existing = already_flashed()
    if existing and not force:
        raise HwidError(
            f"This board's EEPROM already reports product {existing!r} -- "
            "refusing to reflash. Pass force=True (--force) to override."
        )

    check_binaries_present()
    new_uuid = uuid_factory()
    settings_text = render_settings(new_uuid)

    if gpio_backend_factory is None:
        gpio_backend_factory = LgpioBackend
    gpio = gpio_backend_factory()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "krauken.txt"
            eep_path = Path(tmp) / "krauken.eep"
            readback_path = Path(tmp) / "krauken-readback.eep"
            settings_path.write_text(settings_text)

            _run(runner, ["eepmake", str(settings_path), str(eep_path)])

            def _write_and_verify():
                _run(
                    runner,
                    [
                        "eepflash.sh",
                        "-w",
                        "-y",
                        f"-f={eep_path}",
                        f"-t={EEPROM_TYPE}",
                        f"-a={I2C_ADDRESS_HEX}",
                        f"-d={i2c_bus}",
                    ],
                )
                # Read the EEPROM's own contents back and byte-compare
                # against what we intended to write -- NOT
                # /proc/device-tree/hat/product, which Raspberry Pi OS
                # only refreshes on the NEXT boot (the HAT device-tree
                # fixup runs once at boot, not on live EEPROM writes), so
                # it would read as unset here even after a fully
                # successful flash. This has to happen while WP is still
                # unlocked -- eepflash.sh -r also needs live i2c access to
                # the chip, same as the write.
                _run(
                    runner,
                    [
                        "eepflash.sh",
                        "-r",
                        "-y",
                        f"-f={readback_path}",
                        f"-t={EEPROM_TYPE}",
                        f"-a={I2C_ADDRESS_HEX}",
                        f"-d={i2c_bus}",
                    ],
                )
                if readback_path.read_bytes() != eep_path.read_bytes():
                    raise HwidError("Post-flash readback did not match what was written -- EEPROM may be damaged.")

            _with_wp_unlocked(gpio, _write_and_verify)
    finally:
        gpio.close()

    return new_uuid


def status() -> dict[str, str | None]:
    def _read(path: Path) -> str | None:
        return path.read_text().rstrip("\x00").strip() if path.exists() else None

    return {"product": _read(HAT_PRODUCT_PATH), "uuid": _read(HAT_UUID_PATH)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krauken-hwid", description="Provision the Krauken HAT's ID EEPROM.")
    sub = parser.add_subparsers(dest="command", required=True)

    flash_p = sub.add_parser("flash", help="Write this board's identity to its EEPROM (requires root).")
    flash_p.add_argument("--force", action="store_true", help="Reflash even if already provisioned.")
    flash_p.add_argument("--i2c-bus", type=int, default=DEFAULT_I2C_BUS, help="I2C bus number (default: %(default)s).")

    sub.add_parser("status", help="Show whether this board's EEPROM is already provisioned.")

    args = parser.parse_args(argv)

    try:
        if args.command == "flash":
            new_uuid = flash(force=args.force, i2c_bus=args.i2c_bus)
            print(f"Flashed. UUID: {new_uuid}")
            print("Reboot for Raspberry Pi OS to pick up the new EEPROM contents.")
        elif args.command == "status":
            info = status()
            if info["product"] is None:
                print("Not flashed -- no HAT identity present.")
            else:
                print(f"Product: {info['product']}")
                print(f"UUID: {info['uuid']}")
    except HwidError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
