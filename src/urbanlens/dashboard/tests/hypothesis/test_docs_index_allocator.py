"""The index allocates ids, so archiving one must not hand it out again.

`docs/INDEX.md` lists only live records - a resolved problem is removed from it
and moved to `docs/archive/PROBLEMS-ARCHIVE.md`. `bin/check_docs_index.py`
derives "next free id" from the highest id it can see, so if the archive does
not record what it holds, finishing the highest-numbered problem *lowers* the
next free id and the check then demands that the next writer reuse it. That is
the collision the index exists to prevent, arriving through the checker.

These tests pin the archive's `id:` metadata line as load-bearing rather than
decorative, and cover the two half-moves it makes detectable: an entry copied to
the archive without being removed from the live file, and an id archived twice.
"""

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


def _claim(number: int) -> str:
    """The one-line claim used for `P<number>` in both the index and the entry."""
    return f"something measurable went wrong in subsystem {number}"


def _index(live: list[int], next_free: int) -> str:
    """Build an `INDEX.md` listing exactly `live`, declaring `next_free`."""
    rows = "\n".join(f"| P{n} | open | 2026-09-05 | {_claim(n)} | [`docs/PROBLEMS.md`](PROBLEMS.md) |" for n in live)
    return f"# INDEX\n\n**Next free id:** `P{next_free}`\n\n| id | status | updated | claim | path |\n|---|---|---|---|---|\n{rows}\n"


def _problems(live: list[int]) -> str:
    """Build a `PROBLEMS.md` holding exactly the `live` entries."""
    entries = "\n\n".join(
        f"## P{n} — {_claim(n)}\n\n`id: P{n}` · `status: open` · `updated: 2026-09-05`\n\nBody." for n in live
    )
    return f"# PROBLEMS\n\n{entries}\n"


def _archive(archived: list[int], *, record_ids: bool = True) -> str:
    """Build an archive holding `archived`, optionally without their id lines."""
    entries = []
    for n in archived:
        metadata = f"\n\n`id: P{n}` · `status: fixed` · `resolved: 2026-09-05`" if record_ids else ""
        entries.append(f"## RESOLVED 2026-09-05: {_claim(n)}{metadata}\n\nBody.")
    return "# Resolved problems (archive)\n\n" + "\n\n".join(entries) + "\n"


class DocsIndexAllocatorTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def test_a_consistent_index_is_clean(self) -> None:
        self.assertEqual(self.checker.audit(_index([1, 2, 3], 4), _problems([1, 2, 3]), _archive([])), [])

    def test_archiving_the_highest_id_does_not_free_it_for_reuse(self) -> None:
        """P3 resolved: gone from both live files, still allocated by the archive."""
        self.assertEqual(self.checker.audit(_index([1, 2], 4), _problems([1, 2]), _archive([3])), [])

    def test_an_archive_that_drops_the_id_line_demands_the_id_back(self) -> None:
        """Without `id:` in the archive the checker cannot see P3 was ever used."""
        failures = self.checker.audit(_index([1, 2], 4), _problems([1, 2]), _archive([3], record_ids=False))
        self.assertTrue(any("next free P is 4" in failure for failure in failures), failures)

    def test_an_entry_copied_to_the_archive_but_left_live_is_flagged(self) -> None:
        failures = self.checker.audit(_index([1, 2, 3], 4), _problems([1, 2, 3]), _archive([3]))
        self.assertTrue(
            any("P3 is archived but still live in PROBLEMS.md and INDEX.md" in failure for failure in failures),
            failures,
        )

    def test_an_id_archived_twice_is_flagged(self) -> None:
        failures = self.checker.audit(_index([1], 4), _problems([1]), _archive([3, 3]))
        self.assertTrue(any("P3 appears 2 times in the archive" in failure for failure in failures), failures)

    def test_archiving_a_middle_id_leaves_the_next_free_id_alone(self) -> None:
        """Archiving a middle id changes nothing: the highest is what allocates."""
        self.assertEqual(self.checker.audit(_index([1, 3], 4), _problems([1, 3]), _archive([2])), [])

    @given(st.integers(min_value=1, max_value=12), st.data())
    @settings(max_examples=60, deadline=None)
    def test_next_free_is_one_past_the_highest_id_ever_allocated(self, highest: int, data: st.DataObject) -> None:
        """However the ids split between live and archived, the ceiling holds."""
        allocated = list(range(1, highest + 1))
        archived = data.draw(st.lists(st.sampled_from(allocated), unique=True, max_size=highest))
        live = [n for n in allocated if n not in archived]
        index, problems, archive = _index(live, highest + 1), _problems(live), _archive(archived)

        self.assertEqual(self.checker.audit(index, problems, archive), [])

        wrong = data.draw(st.integers(min_value=1, max_value=highest))
        failures = self.checker.audit(_index(live, wrong), problems, archive)
        self.assertTrue(any("next free P" in failure for failure in failures), failures)
