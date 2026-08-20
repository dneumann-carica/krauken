"""Shared fixtures for tests/unit/. Autouse fixtures here apply across
every test file in this directory, not just the one that happens to
define them -- see _fast_query_timeout's own docstring for why this one
in particular needs to be shared rather than file-scoped."""
from __future__ import annotations

import pytest

from krauken.platforms.brewpi import connection


@pytest.fixture(autouse=True)
def _fast_query_timeout(monkeypatch: pytest.MonkeyPatch):
    """connection.py's QUERY_TIMEOUT_S deadline loop uses real
    time.monotonic(), not any injected clock (correct for production,
    where a real serial port's own readline() naturally blocks/paces
    itself via its own timeout) -- but a FakeSerial double that never
    answers a command returns b"" instantly on every call, so without
    this, any test exercising a "never answers" BrewPiConnection path
    would genuinely busy-wait the real 15s. Confirmed live: this
    regressed the full suite's runtime by ~35s (135s vs ~100s) the first
    time QUERY_TIMEOUT_S was introduced, from a single test in
    test_brewpi_identify.py that wasn't in the same file as an
    equivalent, file-scoped fixture -- hence living here instead, so
    every test file automatically gets it regardless of which one
    happens to exercise this path.

    No test in this codebase cares about the exact timeout value, only
    the eventual give-up behavior, so shrinking it well below its real
    value is always safe here."""
    monkeypatch.setattr(connection, "QUERY_TIMEOUT_S", 0.05)


@pytest.fixture(autouse=True)
def _fast_install_settle(monkeypatch: pytest.MonkeyPatch):
    """install_device()'s INSTALL_SETTLE_S is a real asyncio.sleep()
    (deliberately not clock-relative -- see the constant's own comment),
    and every device_config.py test runner installs multiple devices per
    call. Left at its real 0.3s value, this would add real, cumulative
    wall-clock time across the whole suite (many tests each doing several
    installs) for no test-value: no test in this codebase cares about the
    exact settle duration, only that a settle happens at all (see
    test_brewpi_connection.py's own test for that)."""
    monkeypatch.setattr(connection, "INSTALL_SETTLE_S", 0.0)
