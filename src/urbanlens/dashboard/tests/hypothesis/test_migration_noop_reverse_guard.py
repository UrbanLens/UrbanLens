"""Fail the build when a new data migration reverses to ``noop`` without a reason."""

from __future__ import annotations

import ast
from pathlib import Path

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import migrations as migrations_package

MIGRATIONS_DIR = Path(migrations_package.__file__).resolve().parent

#: Migrations whose ``noop`` reverses were read and judged correct, with why.
REVIEWED: dict[str, str] = {
    "0001_initial.py": "seed_rows - a new install's starting rows; reversing 0001 drops the tables they are in.",
}


def _noop_reverse_files() -> dict[str, list[str]]:
    """
    Migration files containing a ``RunPython``/``RunSQL`` that reverses to noop.

    Returns:
        ``{filename: [forward callable names]}``.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"RunPython", "RunSQL"}:
                continue
            reverse = ""
            for keyword in node.keywords:
                if keyword.arg in {"reverse_code", "reverse_sql"}:
                    reverse = ast.unparse(keyword.value)
            if not reverse and len(node.args) >= 2:
                reverse = ast.unparse(node.args[1])
            if "noop" in reverse:
                code = (
                    node.args[0]
                    if node.args
                    else next((kw.value for kw in node.keywords if kw.arg in {"code", "sql"}), None)
                )
                forward = ast.unparse(code) if code is not None else "?"
                found.setdefault(path.name, []).append(forward)
    return found


class MigrationNoopReverseGuardTests(SimpleTestCase):
    def test_every_noop_reverse_is_reviewed(self) -> None:
        unreviewed = {name: ops for name, ops in _noop_reverse_files().items() if name not in REVIEWED}

        self.assertEqual(
            unreviewed,
            {},
            "a migration reverses to RunPython.noop without being reviewed - confirm the pre-migration code can still "
            "read the data after a reverse (a format change cannot), then add it to REVIEWED with the reason",
        )

    def test_no_reviewed_entry_is_stale(self) -> None:
        """A file that no longer has a noop reverse should leave the list."""
        actual = set(_noop_reverse_files())
        self.assertEqual(set(REVIEWED) - actual, set(), "REVIEWED names a migration that no longer reverses to noop")

    def test_every_run_python_declares_some_reverse(self) -> None:
        """Omitting a reverse entirely makes `migrate` refuse - which is a different, louder failure."""
        missing = []
        for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in {"RunPython", "RunSQL"}:
                    continue
                declared = len(node.args) >= 2 or any(kw.arg in {"reverse_code", "reverse_sql"} for kw in node.keywords)
                if not declared:
                    missing.append(f"{path.name}: {ast.unparse(node)[:60]}")
        self.assertEqual(missing, [], "these operations declare no reverse at all")

    # -- guard the guard ----------------------------------------------------

    def test_the_scan_still_finds_migrations(self) -> None:
        self.assertGreaterEqual(
            len(list(MIGRATIONS_DIR.glob("[0-9]*.py"))),
            1,
            "the migration scan found almost nothing - the path resolution broke",
        )

    def test_the_scan_still_finds_noop_reverses(self) -> None:
        """Without this, an AST change that matched nothing would pass silently."""
        found = _noop_reverse_files()
        self.assertGreaterEqual(
            len(found), 1, f"only {len(found)} files with noop reverses found - the matcher stopped working"
        )
