"""Error hierarchy shared across daemon/api/ipc. Each maps to a stable string
code used verbatim on the wire (IPC error envelopes, HTTP error bodies)."""
from __future__ import annotations

from typing import Any, Mapping


class KraukenError(Exception):
    code: str = "internal_error"

    def __init__(self, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})


class NoActiveFermentation(KraukenError):
    code = "no_active_fermentation"


class FermentationAlreadyActive(KraukenError):
    code = "fermentation_already_active"


class HardwareIncomplete(KraukenError):
    code = "hardware_incomplete"


class UnqualifiedAssignment(KraukenError):
    code = "unqualified_assignment"


class UnknownDevice(KraukenError):
    code = "unknown_device"


class StageNotRunning(KraukenError):
    code = "stage_not_running"


class StaleRevision(KraukenError):
    code = "stale_revision"


class DaemonUnavailable(KraukenError):
    code = "daemon_unavailable"


class TestAlreadyRunning(KraukenError):
    code = "test_already_running"


class UnknownTest(KraukenError):
    code = "unknown_test"


class ValidationError(KraukenError):
    code = "validation_error"


class UnknownOp(KraukenError):
    code = "unknown_op"


class PlatformUnavailable(KraukenError):
    """Raised by a platform driver's discover() when the platform genuinely
    cannot be scanned right now (adapter missing, socket absent, dependency
    not installed) -- distinct from "scanned and found nothing"."""

    code = "platform_unavailable"


class DevPanelDisabled(KraukenError):
    """The dev panel (platforms/manual/live.py's settable readings) is
    gated behind Config.dev_panel_enabled at the API tier -- it's a testing
    hook, not something a real deployment should expose by default."""

    code = "dev_panel_disabled"
