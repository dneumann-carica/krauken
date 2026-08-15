"""ISO-8601 UTC timestamp formatting for the daemon's wire responses and DB
writes -- the one place a `float` unix-seconds value becomes the ISO string
every op response and DB row stores. Used to be seven near-identical
private `_iso`/`_iso_now` helpers, one per daemon module that needed one;
consolidated here so there's a single definition of "how a daemon-side
timestamp gets serialized" instead of seven copies that could quietly drift
apart (a UTC-vs-local mistake fixed in one, a microsecond-truncation added
to another, etc.).
"""
from __future__ import annotations

import datetime

from krauken.contracts.clock import Clock


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def iso_now(clock: Clock) -> str:
    """Shorthand for iso(clock.now()) -- for call sites that don't already
    have `now` as a local float."""
    return iso(clock.now())
