"""SQLite connection setup. Two distinct connection modes, deliberately:
read-only (API tier -- one per thread, since FastAPI's threadpool reuses
threads) and read-write (daemon -- single writer, enforced by writes.py
being importable only from the daemon package, see tests/db/test_write_boundary.py).

WAL means neither of these ever blocks the other.
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator

_PRAGMAS_RW = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)
_PRAGMAS_RO = (
    "PRAGMA query_only=ON",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=2000",
)


def open_rw(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS_RW:
        conn.execute(pragma)
    return conn


def open_ro(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS_RO:
        conn.execute(pragma)
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Wraps a block of writes in an explicit BEGIN/COMMIT.

    open_rw() uses isolation_level=None (autocommit) -- every individual
    execute() would otherwise commit on its own, so a daemon crash/restart
    mid-sequence (killed between, say, creating a fermentation row and
    creating its stages) doesn't just fail to happen -- it leaves genuinely
    inconsistent partial state committed and permanent. Confirmed for real:
    a `fermentation.start` call that landed mid-restart left a fully-formed
    profile and an `active`-status fermentation row with zero stage rows
    and no `fermentation_started` event, silently blocking every future
    start with a misleading "already active" state. Every multi-write
    daemon op needs this, not just whichever one happens to get caught."""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
