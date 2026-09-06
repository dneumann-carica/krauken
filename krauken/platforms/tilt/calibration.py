"""Per-device gravity calibration for Tilt hydrometers -- a real, confirmed
need: a Tilt's own reported gravity commonly drifts from a freshly
calibrated reference (a user's own calibrated Tilt app, or a hydrometer/
refractometer) by several thousandths of SG, and there's no way to fix
that at the firmware/beacon level (it's a sealed device broadcasting a
fixed reading).

Registered under platforms/registry.py's PLATFORM_BINDINGS["tilt"].
test_runners -- device CONFIGURATION, not device OPERATION, same
reasoning as BrewPi's own device_config.py test runners (see that
module's docstring and contracts/interfaces.py's PlatformTestRunner
docstring for why this doesn't go through the GravitySource Protocol).

The one action here, calibrate_gravity, takes what the user's reference
reads RIGHT NOW (params["actual_sg"]) rather than a raw offset -- it
reads the scanner's current true-raw reading for this color
(TiltScanner.raw_latest(), never the already-calibrated latest()) and
computes actual - raw itself, so the user never has to do that
subtraction by hand. The resulting {color: offset_sg} map is persisted
through the generic settings table (TILT_CALIBRATION_SETTING_KEY -- same
table/idiom as chamber_location, no schema migration needed) AND pushed
immediately into the live TiltScanner (set_calibration()), so it takes
effect on the very next read with no daemon restart. daemon/app.py's
Daemon.start() reloads the persisted map into a fresh TiltScanner the
same way, right after the registry starts.

Deliberately NOT gated by requires_no_active_fermentation: recalibrating
mid-batch -- this project's own motivating case, a batch already running
with a Tilt reading off against a freshly calibrated reference -- is
exactly when a user needs this, not something that should have to wait
for the batch to finish.
"""
from __future__ import annotations

from typing import Any, Mapping

from krauken.contracts.interfaces import PlatformTestRunner
from krauken.daemon.timefmt import iso_now as _iso_now
from krauken.db import queries, writes
from krauken.platforms.tilt.scanner import TiltScanner

TILT_CALIBRATION_SETTING_KEY = "tilt_calibration"


def _tilt_scanner(ctx: Any) -> TiltScanner | None:
    scanner = ctx.registry.state_for("tilt")
    return scanner if isinstance(scanner, TiltScanner) else None


def _color_from_device_id(device_id: str) -> str:
    return device_id.split(":", 1)[1] if ":" in device_id else device_id


async def _run_calibrate_gravity(ctx: Any, job: Any, device_id: str, params: Mapping[str, Any]) -> None:
    scanner = _tilt_scanner(ctx)
    if scanner is None:
        job.state = "failed"
        job.error = "Tilt is not the current platform"
        return

    actual_sg = params.get("actual_sg")
    if not isinstance(actual_sg, (int, float)) or isinstance(actual_sg, bool):
        job.state = "failed"
        job.error = "calibrate_gravity requires a numeric actual_sg"
        return

    color = _color_from_device_id(device_id)
    raw = scanner.raw_latest(color)
    if raw is None:
        job.state = "failed"
        job.error = f"no live reading for {device_id} right now -- move it in range and try again"
        return

    offset = float(actual_sg) - raw.gravity_sg
    async with ctx.db_lock:
        offsets = dict(queries.setting(ctx.conn, TILT_CALIBRATION_SETTING_KEY) or {})
        offsets[color] = offset
        writes.write_setting(ctx.conn, TILT_CALIBRATION_SETTING_KEY, offsets, _iso_now(ctx.clock))

    # Pushed live immediately, same as every other setting derived from
    # this write -- see this module's own docstring on why TiltScanner
    # can't just poll the settings table itself.
    scanner.set_calibration(offsets)

    job.state = "completed"
    job.result = {
        "color": color,
        "raw_sg": raw.gravity_sg,
        "offset_sg": offset,
        "calibrated_sg": raw.gravity_sg + offset,
    }


TEST_RUNNERS: Mapping[str, PlatformTestRunner] = {
    "calibrate_gravity": PlatformTestRunner(run=_run_calibrate_gravity),
}


def reload_calibration(ctx: Any) -> None:
    """Daemon-startup counterpart to _run_calibrate_gravity's live push: a
    freshly constructed TiltScanner always starts with an empty offset
    table (it has no DB access of its own -- see that class's own
    docstring), so this reloads whatever's persisted under
    TILT_CALIBRATION_SETTING_KEY into it. Called unconditionally from
    daemon/app.py's Daemon.start(), right after the registry starts --
    cheap and a no-op if Tilt isn't the running platform, same shape as
    daemon/drivers.py's sync_remote_clocks.

    Deliberately NOT called from daemon/app.py directly via `isinstance(x,
    TiltScanner)`: DaemonContext's own docstring is explicit that the
    daemon composition root holds no reference to any concrete platform
    class, only generic registry questions. TiltScanner is a concrete
    class this module already legitimately imports (it's the whole point
    of this file); giving app.py one named function to call keeps that
    knowledge here instead of leaking it into the composition root."""
    scanner = _tilt_scanner(ctx)
    if scanner is not None:
        scanner.set_calibration(queries.setting(ctx.conn, TILT_CALIBRATION_SETTING_KEY) or {})
