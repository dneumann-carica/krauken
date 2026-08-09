"""Control-loop constants -- single source of truth, imported by both the
daemon (beer-side enforcement) and the Hardware Supervisor (compressor-
protection enforcement). See fermentation_controller_master_spec's sibling
software plan and the resolved-decisions plan for why enforcement is split
by tier while the values live in one place:

Daemon enforces the beer-side numbers below (needs beer temp, which the
Supervisor must never know about, so it keeps protecting the compressor
even if the daemon/cascade is completely broken). Supervisor enforces the
compressor-protection numbers (must survive daemon crashes).

Source: the UI prototype's simulator (designs/Krauken Fermentation Chart.dc.html)
and the handoff README's "Control model" section, confirmed as the real
intended values -- not placeholders -- by the project owner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# --- beer-side (enforced in the control daemon's cascade) ---
BEER_TRIGGER_BAND_F: Final[float] = 0.5  # fire when |beer - target| exceeds this
BEER_RELEASE_OFFSET_F: Final[float] = 0.0  # release when beer crosses back through target
CHAMBER_COOL_CLAMP_F: Final[float] = 5.0  # cool demand -> chamber target = beer_target - 5
CHAMBER_HEAT_CLAMP_F: Final[float] = 4.0  # heat demand -> chamber target = beer_target + 4

# --- chamber/actuator side (enforced in the Hardware Supervisor) ---
CHAMBER_DEADBAND_F: Final[float] = 0.5  # NOT from the mock -- see module docstring; the
# mock's simulated fridge asymptotically approaches the clamp and never arrives, so it
# never needed a chamber-side thermostat. A real fridge reaches it and must stop.
MIN_ON_S: Final[float] = 5 * 60
MIN_OFF_S: Final[float] = 5 * 60
OPPOSITE_LOCKOUT_S: Final[float] = 30 * 60

# --- absolute safety envelope ---
CHAMBER_TARGET_MIN_F: Final[float] = 28.0
CHAMBER_TARGET_MAX_F: Final[float] = 90.0

# --- degradation thresholds ---
CHAMBER_TEMP_LOSS_S: Final[float] = 5 * 60  # supervisor de-energizes if it goes blind
DAEMON_SILENCE_ALERT_S: Final[float] = 5 * 60  # alertable; does not change actuation
BEER_TEMP_LOST_THRESHOLD_S: Final[float] = 15 * 60  # default, user-configurable


@dataclass(frozen=True, slots=True)
class ControlTuning:
    """Override point for settings + scenario time-compression. scaled()
    lets a compressed-time test scenario assert against the SCALED value of
    a timer rather than the literal 45/90 minutes."""

    beer_trigger_band_f: float = BEER_TRIGGER_BAND_F
    beer_release_offset_f: float = BEER_RELEASE_OFFSET_F
    chamber_cool_clamp_f: float = CHAMBER_COOL_CLAMP_F
    chamber_heat_clamp_f: float = CHAMBER_HEAT_CLAMP_F
    chamber_deadband_f: float = CHAMBER_DEADBAND_F
    min_on_s: float = MIN_ON_S
    min_off_s: float = MIN_OFF_S
    opposite_lockout_s: float = OPPOSITE_LOCKOUT_S
    chamber_target_min_f: float = CHAMBER_TARGET_MIN_F
    chamber_target_max_f: float = CHAMBER_TARGET_MAX_F

    def scaled(self, factor: float) -> "ControlTuning":
        return ControlTuning(
            beer_trigger_band_f=self.beer_trigger_band_f,
            beer_release_offset_f=self.beer_release_offset_f,
            chamber_cool_clamp_f=self.chamber_cool_clamp_f,
            chamber_heat_clamp_f=self.chamber_heat_clamp_f,
            chamber_deadband_f=self.chamber_deadband_f,
            min_on_s=self.min_on_s * factor,
            min_off_s=self.min_off_s * factor,
            opposite_lockout_s=self.opposite_lockout_s * factor,
            chamber_target_min_f=self.chamber_target_min_f,
            chamber_target_max_f=self.chamber_target_max_f,
        )
