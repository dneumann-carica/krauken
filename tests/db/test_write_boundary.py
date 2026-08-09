"""Makes the single-writer rule a build failure, not a convention: the API
package must never import krauken.db.writes -- only the daemon writes to
SQLite. See krauken/db/writes.py's own docstring.
"""
from __future__ import annotations

import ast
from pathlib import Path

API_PACKAGE = Path(__file__).parents[2] / "krauken" / "api"


def _imports_writes(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "krauken.db.writes" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "krauken.db.writes" or module == "krauken.db" and any(
                alias.name == "writes" for alias in node.names
            ):
                return True
    return False


def test_api_package_never_imports_writes():
    offenders = [p for p in API_PACKAGE.rglob("*.py") if _imports_writes(p)]
    assert not offenders, (
        f"krauken.api must never import krauken.db.writes (single-writer rule) -- "
        f"found imports in: {offenders}"
    )
