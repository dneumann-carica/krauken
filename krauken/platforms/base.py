"""Graceful optional-dependency handling for platforms whose real-hardware
libraries (pyserial, aioblescan -- see pyproject.toml's `pi` extra) aren't
installed on every machine that imports krauken.platforms.*. A dev Mac
running only Manual/Simulator should never fail to import this package
just because it has no `pip install -e ".[pi]"` -- and a Pi that DOES have
the extra installed should still degrade cleanly (not crash the whole
scan) if, say, aioblescan's raw HCI socket can't be opened because the
process lacks CAP_NET_RAW.

Both failure modes funnel through the same PlatformUnavailable exception
(contracts/errors.py already documents "dependency not installed" as one
of that exception's intended cases) so discovery.py's existing "one
platform's failure never sinks the whole scan" handling (its
_discover_one's `except PlatformUnavailable` branch) covers this for free
-- nothing in discovery.py needed to change for BrewPi/Tilt to land.
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, TypeVar

from krauken.contracts.errors import PlatformUnavailable

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def requires_optional(module_name: str, *, extra: str = "pi") -> Callable[[F], F]:
    """Decorator for an async method whose body needs `module_name`
    actually importable. Turns a raw ImportError (which would otherwise
    surface as an opaque ModuleNotFoundError from deep inside a driver
    call) into a PlatformUnavailable with an actionable message -- and,
    for a discover() method specifically, that's exactly the exception
    type discovery.py already knows how to fold into a clean "unavailable"
    scan status instead of failing the whole scan.

    Checked on every call, not just once at import time -- cheap (a
    dict lookup in sys.modules after the first successful import) and
    correct for the case where the dependency becomes available without
    a process restart (e.g. installed into a venv the daemon already
    has open), though that's a minor benefit; the main win is never
    raising at class-definition/import time, only when the method is
    actually invoked."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                __import__(module_name)
            except ImportError as exc:
                raise PlatformUnavailable(
                    f"{module_name} is not installed -- run `pip install -e '.[{extra}]'` "
                    "to enable this platform"
                ) from exc
            return await fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
