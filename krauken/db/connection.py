"""SQLite connection setup. Two distinct connection modes, deliberately:
read-only (API tier -- one per thread, since FastAPI's threadpool reuses
threads) and read-write (daemon -- single writer, enforced by writes.py
being importable only from the daemon package, see tests/db/test_write_boundary.py).

WAL means neither of these ever blocks the other.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

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
