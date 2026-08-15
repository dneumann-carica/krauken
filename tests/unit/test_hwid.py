"""Tests krauken-hwid's provisioning logic against fakes -- lgpio can't
even be installed on a non-Linux dev machine (let alone exercised against
real EEPROM hardware), and eepmake/eepflash.sh/eepdump are external C
tools, not something a unit test should require on PATH. FakeGpioBackend
and FakeRunner stand in for both, mirroring the same "fake the transport,
test the real logic" shape used for BrewPi's FakeSerial and Tilt's raw
packet fixtures.

The one property these tests exist to nail down above all: EEPROM_WP must
be restored HIGH (locked) no matter what fails partway through a flash --
there's no physical jumper on this board as a second line of defense, so
this logic IS the only thing protecting the chip from an accidental write
outside a deliberate flash.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from krauken.cli import hwid

# Captured before any test's autouse fixture replaces hwid.check_binaries_present
# with a no-op stub -- a direct reference to the ORIGINAL function object survives
# that later monkeypatch.setattr() on the module attribute, since setattr replaces
# what the module's name points to, not the function object itself.
_real_check_binaries_present = hwid.check_binaries_present


class FakeGpioBackend:
    def __init__(self):
        self.calls: list[tuple[str, int, int]] = []
        self.closed = False
        self.level: int | None = None

    def claim_output(self, gpio: int, level: int) -> None:
        self.calls.append(("claim_output", gpio, level))
        self.level = level

    def write(self, gpio: int, level: int) -> None:
        self.calls.append(("write", gpio, level))
        self.level = level

    def close(self) -> None:
        self.closed = True


class FakeRunner:
    """Stands in for subprocess.run against eepmake/eepflash.sh. eepmake
    is faked to actually write a file at its output path (so later steps
    that read it back get real bytes to compare, not nothing) --
    everything else about the real tools' behavior is irrelevant to the
    logic being tested here."""

    def __init__(self, *, write_fails: bool = False, readback_corrupted: bool = False):
        self.calls: list[list[str]] = []
        self.write_fails = write_fails
        self.readback_corrupted = readback_corrupted

    def __call__(self, args, capture_output=True, text=True):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "eepmake":
            Path(args[2]).write_bytes(b"FAKE-EEPROM-IMAGE")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == "eepflash.sh":
            if "-w" in args:
                if self.write_fails:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="simulated write failure")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if "-r" in args:
                f_arg = next(a for a in args if a.startswith("-f="))
                readback_path = Path(f_arg.split("=", 1)[1])
                readback_path.write_bytes(b"CORRUPTED" if self.readback_corrupted else b"FAKE-EEPROM-IMAGE")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {args}")


@pytest.fixture(autouse=True)
def _no_real_binary_check(monkeypatch):
    # Every test here fakes the runner directly -- none of them should
    # depend on eepmake/eepflash.sh/eepdump actually being on PATH.
    monkeypatch.setattr(hwid, "check_binaries_present", lambda: None)


@pytest.fixture
def not_yet_flashed(monkeypatch, tmp_path):
    monkeypatch.setattr(hwid, "HAT_PRODUCT_PATH", tmp_path / "product")
    monkeypatch.setattr(hwid, "HAT_UUID_PATH", tmp_path / "uuid")


@pytest.fixture
def already_flashed_as(monkeypatch, tmp_path):
    def _set(product: str) -> None:
        (tmp_path / "product").write_text(product + "\x00")
        monkeypatch.setattr(hwid, "HAT_PRODUCT_PATH", tmp_path / "product")
        monkeypatch.setattr(hwid, "HAT_UUID_PATH", tmp_path / "uuid")

    return _set


def test_render_settings_substitutes_the_uuid():
    text = hwid.render_settings("11111111-2222-3333-4444-555555555555")
    assert "product_uuid 11111111-2222-3333-4444-555555555555" in text
    assert "vendor \"TheKrauken\"" in text
    assert "setgpio 4 INPUT NONE" in text
    assert "setgpio 17 OUTPUT DOWN" in text
    assert "setgpio 27 OUTPUT DOWN" in text
    assert "setgpio 22 OUTPUT UP" in text
    # GPIO0/GPIO1 (ID_SD/ID_SC) must never appear in the gpio map -- see
    # the template's own comment on why that's a category error.
    assert "setgpio 0 " not in text
    assert "setgpio 1 " not in text


def test_already_flashed_reports_none_on_a_virgin_board(not_yet_flashed):
    assert hwid.already_flashed() is None


def test_already_flashed_reports_the_product_string(already_flashed_as):
    already_flashed_as("Krauken Fermentation Controller HAT")
    assert hwid.already_flashed() == "Krauken Fermentation Controller HAT"


def test_flash_refuses_when_already_flashed_without_force(already_flashed_as):
    already_flashed_as("Krauken Fermentation Controller HAT")
    with pytest.raises(hwid.HwidError, match="already reports product"):
        hwid.flash(gpio_backend_factory=FakeGpioBackend, runner=FakeRunner())


def test_flash_proceeds_with_force_even_if_already_flashed(already_flashed_as):
    already_flashed_as("Krauken Fermentation Controller HAT")
    gpio = FakeGpioBackend()
    result = hwid.flash(force=True, gpio_backend_factory=lambda: gpio, runner=FakeRunner())
    assert result  # a UUID string came back, no exception


def test_flash_happy_path_writes_reads_back_and_locks_wp_again(not_yet_flashed):
    gpio = FakeGpioBackend()
    fixed_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    runner = FakeRunner()

    result = hwid.flash(gpio_backend_factory=lambda: gpio, runner=runner, uuid_factory=lambda: fixed_uuid)

    assert result == fixed_uuid
    # unlocked (low) before the risky work, locked (high) again after --
    # in that order, on the real EEPROM_WP pin.
    assert gpio.calls[0] == ("claim_output", hwid.EEPROM_WP_GPIO, 0)
    assert gpio.calls[-1] == ("write", hwid.EEPROM_WP_GPIO, 1)
    assert gpio.level == 1  # ends locked
    assert gpio.closed is True

    commands = [c[0] for c in runner.calls]
    assert commands == ["eepmake", "eepflash.sh", "eepflash.sh"]
    assert "-w" in runner.calls[1]
    assert "-r" in runner.calls[2]


def test_wp_is_relocked_even_when_the_write_step_fails(not_yet_flashed):
    # The actual safety property this module exists for: no physical
    # jumper backs this up, so the software MUST restore write-protection
    # on failure, not just on success.
    gpio = FakeGpioBackend()
    runner = FakeRunner(write_fails=True)

    with pytest.raises(hwid.HwidError, match="failed"):
        hwid.flash(gpio_backend_factory=lambda: gpio, runner=runner)

    assert gpio.calls[-1] == ("write", hwid.EEPROM_WP_GPIO, 1)
    assert gpio.level == 1  # locked again despite the failure
    assert gpio.closed is True


def test_flash_detects_a_corrupted_readback(not_yet_flashed):
    gpio = FakeGpioBackend()
    runner = FakeRunner(readback_corrupted=True)

    with pytest.raises(hwid.HwidError, match="did not match"):
        hwid.flash(gpio_backend_factory=lambda: gpio, runner=runner)

    # Still relocked even though the failure was detected after both
    # eepflash.sh calls completed "successfully" from their own exit codes.
    assert gpio.calls[-1] == ("write", hwid.EEPROM_WP_GPIO, 1)
    assert gpio.closed is True


def test_status_reports_not_flashed(not_yet_flashed, capsys):
    assert hwid.main(["status"]) == 0
    assert "Not flashed" in capsys.readouterr().out


def test_status_reports_flashed_product_and_uuid(monkeypatch, tmp_path, capsys):
    (tmp_path / "product").write_text("Krauken Fermentation Controller HAT\x00")
    (tmp_path / "uuid").write_text(str(uuid.uuid4()) + "\x00")
    monkeypatch.setattr(hwid, "HAT_PRODUCT_PATH", tmp_path / "product")
    monkeypatch.setattr(hwid, "HAT_UUID_PATH", tmp_path / "uuid")

    assert hwid.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Krauken Fermentation Controller HAT" in out


def test_main_flash_prints_the_uuid_and_reboot_reminder(not_yet_flashed, monkeypatch, capsys):
    monkeypatch.setattr(hwid, "LgpioBackend", lambda: FakeGpioBackend())
    monkeypatch.setattr("subprocess.run", FakeRunner())

    rc = hwid.main(["flash"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Flashed. UUID:" in out
    assert "Reboot" in out


def test_main_flash_reports_the_already_flashed_error_cleanly(already_flashed_as, capsys):
    already_flashed_as("Krauken Fermentation Controller HAT")
    rc = hwid.main(["flash"])
    assert rc == 1
    assert "already reports product" in capsys.readouterr().err


def test_missing_binaries_gives_an_actionable_error(monkeypatch):
    # Calls the captured ORIGINAL function, not hwid.check_binaries_present
    # -- the autouse fixture above has already replaced that module
    # attribute with a no-op stub for every test, this one included.
    monkeypatch.setattr(hwid.shutil, "which", lambda name: None)
    with pytest.raises(hwid.HwidError, match="eeptools"):
        _real_check_binaries_present()
