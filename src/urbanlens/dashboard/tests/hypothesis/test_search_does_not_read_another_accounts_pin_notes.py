"""Searching your own pins must not read private note rows belonging to a pin you cannot see.

``PinSearchProvider.search`` feeds ``notes__text`` to ``apply_text`` alongside ``labels__name`` and
``aliases__name`` - Pin's own reverse foreign key to ``PinNote`` (``related_name="notes"``). Notes are
private per-pin text (``PinNote``'s own docstring: "only the pin owner can see"), so this is not just
a capacity concern: the unscoped ``Exists(Pin._base_manager.filter(notes__text__icontains=..., ...))``
semijoin at the heart of P123 means the *content* of a stranger's private notes is read by Postgres to
answer the viewer's search, even though it can never appear in a result. No index covers
``PinNote.text``, so both the matching and non-matching variant reproduced, matching every other
relation P123 generalises to that lacks Label.name's migration-0049 GIN trigram index.

**Fixed 2026-09-17.** ``notes`` crosses at *path*'s own first segment, so ``_semijoin`` now bounds its
filter by the outer queryset's own candidate primary keys, materialised as a concrete list first
rather than left as a nested subquery - which also means the stranger's private note content is no
longer read at all to answer the viewer's search. See
``test_search_does_not_read_another_accounts_labels.py``'s module docstring for the measured
mechanism and ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for the full writeup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PinSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through the viewer's own pin note.
TERM = "quokka"

#: A word the viewer never searches for, for notes that must be scanned to be rejected.
OTHER = "wombat"

#: Notes the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's notes were read.
TOLERANCE = 20


class _PinNoteCase(TestCase):
    """A viewer with one pin matching through its own note, and a stranger's pin with unrelated notes."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.noted = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.mine = baker.make(Pin, profile=self.viewer, location=my_location, name="Unrelated", description="")
        baker.make(PinNote, pin=self.mine, text=f"{TERM} mine")

        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_pin = baker.make(
            Pin, profile=self.stranger, location=their_location, name="Their Spot", description=""
        )
        self.seed_strangers_notes(FIRST_BATCH)

    def seed_strangers_notes(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's pin *count* more private notes, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.noted += 1
            baker.make(PinNote, pin=self.their_pin, text=f"{word} theirs {self.noted}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {PinNote._meta.db_table}, {Pin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's bare-term search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PinSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def note_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the pin-note table.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search()
        table = PinNote._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.note_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the pin-note table, so nothing was measured")
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_notes(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            captured, statements = self.note_reading_statements()
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's pin search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} private notes of their own - {after - before} more rows of another account's "
                f"private text read to answer a search that can never surface them. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPinNotesTests(_PinNoteCase):
    """One account's private pin notes must not be read to answer a different account's pin search."""

    def test_it_does_not_read_a_strangers_matching_notes(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_notes(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PinNoteCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_pin(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the pin whose note matches the term, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_notes_match_the_term(self) -> None:
        self.seed_strangers_notes(SECOND_BATCH)
        matching = PinNote.objects.filter(pin=self.their_pin, text__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded notes do not match the search term"
        )

    def test_the_strangers_unrelated_notes_do_not_match_the_term(self) -> None:
        self.seed_strangers_notes(SECOND_BATCH, matching=False)
        matching = PinNote.objects.filter(pin=self.their_pin, text__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_pin_is_not_visible_to_the_viewer(self) -> None:
        visible = Pin.objects.filter(profile=self.viewer, pk=self.their_pin.pk).count()
        self.assertEqual(visible, 0, "the stranger's pin is visible to the viewer, so reading its notes is correct")

    def test_the_measured_statement_reads_the_pin_note_table(self) -> None:
        captured, statements = self.note_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the pin-note table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            PinNote._meta.db_table, touched, f"no plan read the pin-note table; relations read were {sorted(touched)}"
        )
