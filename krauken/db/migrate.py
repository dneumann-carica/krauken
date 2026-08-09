"""Forward-only migrations, tracked via PRAGMA user_version. Idempotent --
safe to run on every daemon startup before anything else touches the DB."""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from krauken.db.connection import open_rw

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"^(\d+)_", p.name)
        if not m:
            continue
        files.append((int(m.group(1)), p))
    return sorted(files, key=lambda t: t[0])


def migrate(db_path: Path | str) -> int:
    """Applies every migration numbered above the DB's current user_version.
    Returns the resulting version."""
    conn = open_rw(db_path)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        applied = 0
        for version, path in _migration_files():
            if version <= current:
                continue
            sql = path.read_text()
            # executescript() runs with its own implicit transaction handling
            # (it commits any pending transaction first, then runs as one
            # script) -- wrapping it in an explicit BEGIN/COMMIT conflicts
            # with that, so we let it manage its own transaction here.
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            applied += 1
            current = version
        return current
    finally:
        conn.close()


def current_version(db_path: Path | str) -> int:
    conn = open_rw(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def latest_known_version() -> int:
    files = _migration_files()
    return files[-1][0] if files else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="krauken-db")
    parser.add_argument("command", choices=["migrate", "reset", "seed-demo"])
    parser.add_argument("--db", default="krauken.db", help="Path to the SQLite file")
    args = parser.parse_args()

    if args.command == "migrate":
        version = migrate(args.db)
        print(f"Migrated {args.db} to user_version={version}")
    elif args.command == "reset":
        Path(args.db).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(args.db) + suffix).unlink(missing_ok=True)
        version = migrate(args.db)
        print(f"Reset {args.db}, migrated to user_version={version}")
    elif args.command == "seed-demo":
        from krauken.db.seed import seed_demo_batch

        migrate(args.db)
        seed_demo_batch(args.db)
        print(f"Seeded demo batch into {args.db}")
    else:  # pragma: no cover
        sys.exit(f"unknown command {args.command}")


if __name__ == "__main__":
    main()
