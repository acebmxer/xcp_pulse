"""Keeps docs/functions.md honest.

The index exists so the same thing does not get written three different ways.
That only works if it is accurate, and an index maintained purely by good
intentions is out of date within a release or two.

This test enforces *existence* in both directions: every public function has a
row, and every row names a function that still exists. It deliberately does not
check the prose columns ("Does", "Used by") — a check that can be satisfied by
typing anything is worse than no check, because it implies a guarantee it does
not give. Those columns are the reviewer's job.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
INDEX_PATH = REPO_ROOT / "docs" / "functions.md"

# Rows look like: | `name` | `(sig) -> ret` | does | used by | v0.1.0 |
ROW_PATTERN = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|")


def _public_functions() -> dict[str, set[str]]:
    """Map module path -> public top-level function names defined in it."""
    found: dict[str, set[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and not node.name.startswith("_")
        }
        if names:
            found[str(path.relative_to(REPO_ROOT))] = names
    return found


def _indexed_functions() -> set[str]:
    text = INDEX_PATH.read_text(encoding="utf-8")
    return {
        match.group(1) for line in text.splitlines() if (match := ROW_PATTERN.match(line.strip()))
    }


def test_function_index_exists() -> None:
    assert INDEX_PATH.exists(), "docs/functions.md is missing — the index is required"


def test_every_public_function_is_indexed() -> None:
    indexed = _indexed_functions()
    missing: list[str] = []
    for module, names in _public_functions().items():
        missing.extend(f"{module}::{name}" for name in sorted(names) if name not in indexed)

    assert not missing, (
        "These public functions have no row in docs/functions.md:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd a row in the same commit that adds the function."
    )


def test_index_names_no_function_that_was_removed() -> None:
    defined = {name for names in _public_functions().values() for name in names}
    stale = sorted(name for name in _indexed_functions() if name not in defined)

    assert not stale, (
        "docs/functions.md lists functions that no longer exist:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove the row in the same commit that removes the function."
    )
