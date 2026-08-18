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
