"""The index allocates the ids it still holds, and refuses the ones that moved out."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_docs_index.py"


def _load_checker():
    """Import ``bin/check_docs_index.py`` as a module.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_docs_index", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _row(ident: str, status: str = "current") -> str:
    return f"| {ident} | {status} | 2026-10-07 | something measurable about {ident} | [`docs/NOTES.md`](NOTES.md) |"


def _index(rows: list[str], next_free: str) -> str:
    """Build an `INDEX.md` holding `rows`, declaring `next_free`."""
    body = "\n".join(rows)
    return f"# INDEX\n\n**Next free id:** {next_free}\n\n| id | status | updated | claim | path |\n|---|---|---|---|---|\n{body}\n"


class DocsIndexAllocatorTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def test_a_consistent_index_is_clean(self) -> None:
        index = _index([_row("N1"), _row("N2"), _row("PL1", "live")], "`N3` · `PL2`")
        self.assertEqual(self.checker.audit(index, ["0001-a.md", "0002-b.md", "README.md"]), [])

    def test_a_problem_row_is_refused(self) -> None:
        failures = self.checker.audit(_index([_row("P339", "open")], "`N1`"), [])
        self.assertTrue(any("P339: problems are GitHub issues" in failure for failure in failures), failures)

    def test_a_task_or_decision_row_is_refused(self) -> None:
        failures = self.checker.audit(_index([_row("T4", "open"), _row("D28", "accepted")], "`N1`"), [])
        self.assertTrue(any("T4: tasks are GitHub issues" in failure for failure in failures), failures)
        self.assertTrue(any("D28: decisions are ADRs" in failure for failure in failures), failures)

    def test_next_free_naming_a_retired_prefix_is_flagged(self) -> None:
        failures = self.checker.audit(_index([_row("N1")], "`N2` · `P339`"), [])
        self.assertTrue(any("next free names P339" in failure for failure in failures), failures)

    def test_a_duplicated_id_is_flagged(self) -> None:
        failures = self.checker.audit(_index([_row("N1"), _row("N1")], "`N2`"), [])
        self.assertTrue(any("N1 appears 2 times" in failure for failure in failures), failures)

    def test_a_wrong_status_is_flagged(self) -> None:
        failures = self.checker.audit(_index([_row("N1", "open")], "`N2`"), [])
        self.assertTrue(any("N1: status 'open'" in failure for failure in failures), failures)

    def test_two_adrs_sharing_a_number_are_flagged(self) -> None:
        failures = self.checker.audit(_index([], "`N1`"), ["0003-a.md", "0003-b.md"])
        self.assertTrue(any("ADR-0003 is used by 0003-a.md, 0003-b.md" in failure for failure in failures), failures)

    def test_a_misnamed_adr_is_flagged(self) -> None:
        failures = self.checker.audit(_index([], "`N1`"), ["use-postgres.md"])
        self.assertTrue(any("use-postgres.md: not named" in failure for failure in failures), failures)

    @given(st.integers(min_value=1, max_value=12), st.data())
    @settings(max_examples=60, deadline=None)
    def test_next_free_is_one_past_the_highest_id_listed(self, highest: int, data: st.DataObject) -> None:
        """However many lower ids are missing, the highest one sets the ceiling."""
        listed = data.draw(st.sets(st.integers(min_value=1, max_value=highest))) | {highest}
        rows = [_row(f"N{n}") for n in sorted(listed)]

        self.assertEqual(self.checker.audit(_index(rows, f"`N{highest + 1}`"), []), [])

        wrong = data.draw(st.integers(min_value=1, max_value=highest))
        failures = self.checker.audit(_index(rows, f"`N{wrong}`"), [])
        self.assertTrue(any("next free N" in failure for failure in failures), failures)
