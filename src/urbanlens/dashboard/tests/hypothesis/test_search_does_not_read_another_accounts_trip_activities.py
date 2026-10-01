"""Searching your own trips must not read activity rows belonging to a trip you are not on.

Same mechanism as P123 (``test_search_does_not_read_another_accounts_labels.py``):
``TripSearchProvider.search`` feeds ``activities__title`` and ``activities__notes`` to
``apply_text``, and both cross a to-many relation (``TripActivity`` is a reverse foreign key,
``related_name="activities"``), so both go through the same unscoped ``_semijoin`` - an
``Exists(Trip._base_manager.filter(...))`` subquery built from the *unfiltered* manager, run before
the outer ``Trip.objects.filter(Q(pk__in=...) | Q(creator=profile))`` membership check narrows
anything.

``title`` and ``activities__notes`` are two separate ``_semijoin`` calls (``term_filter`` builds one
per field path, OR-ed together) but land in the same statement against the same
``dashboard_trip_activities`` table, so one search that matches through either column measures both
at once. The viewer's own activity matches through both columns independently, so the two matching
variants below are genuinely distinct reproductions, not the same query run twice.

**Fixed 2026-09-17.** ``activities`` crosses at *path*'s own first segment, so ``_semijoin`` now bounds
its filter by the outer queryset's own candidate primary keys, materialised as a concrete list first
rather than left as a nested subquery. See
``test_search_does_not_read_another_accounts_labels.py``'s module docstring for the measured
mechanism and ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for the full writeup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Q
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import TripSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Matches only through the viewer's own activity's title.
TERM_TITLE = "quokka-title"

#: Matches only through the viewer's own activity's notes.
TERM_NOTES = "wombat-notes"

#: Matches neither column anywhere, for the true-negative variant.
NOWHERE = "narwhal-nowhere"

#: Activities the stranger's trip holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's activities were read.
TOLERANCE = 20


class _TripActivityCase(TestCase):
    """A viewer with one trip they belong to (one activity matching two ways), and a stranger's
    trip the viewer is not a member of, whose unrelated activities share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.seeded = 0

        self.mine = baker.make(Trip, creator=self.viewer, name="My Trip", description="")
        baker.make(TripActivity, trip=self.mine, title=f"{TERM_TITLE} mine", notes=f"{TERM_NOTES} mine")

        # The viewer is neither the creator nor a member of this trip - inaccessible, so it must
        # never appear as a search candidate for the viewer.
        self.their_trip = baker.make(Trip, creator=self.stranger, name="Their Trip", description="")
        self.seed_strangers_activities(FIRST_BATCH)

    def seed_strangers_activities(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's trip *count* more activities, then re-analyse."""
        for _ in range(count):
            self.seeded += 1
            if matching:
                title, notes = f"{TERM_TITLE} theirs {self.seeded}", f"{TERM_NOTES} theirs {self.seeded}"
            else:
                title, notes = f"unrelated title {self.seeded}", f"unrelated notes {self.seeded}"
            baker.make(TripActivity, trip=self.their_trip, title=title, notes=notes)
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {TripActivity._meta.db_table}, {Trip._meta.db_table}")  # noqa: S608

    def search(self, term: str) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's search for *term*, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = TripSearchProvider().search(self.viewer, parse_query(term), 20)
        return list(results), captured

    def activity_reading_statements(
        self, term: str
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the trip-activity table.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search(term)
        table = TripActivity._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self, term: str) -> int:
        captured, statements = self.activity_reading_statements(term)
        self.assertTrue(
            statements, "no statement of the search mentioned the trip-activity table, so nothing was measured"
        )
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, term: str, *, matching: bool) -> None:
        before = self.rows_read_searching(term)
        self.seed_strangers_activities(SECOND_BATCH, matching=matching)
        after = self.rows_read_searching(term)
        if after > before + TOLERANCE:
            captured, statements = self.activity_reading_statements(term)
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            self.fail(
                f"the viewer's trip search for {term!r} read {before} rows, then {after} after a stranger's trip "
                f"gained {SECOND_BATCH} activities - {after - before} more rows for a trip the viewer isn't on. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsTripActivitiesTests(_TripActivityCase):
    """One trip's activities must not be read to answer a search for a different, accessible trip."""

    def test_it_does_not_read_a_strangers_activities_when_the_term_matches_via_title(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(TERM_TITLE, matching=True)

    def test_it_does_not_read_a_strangers_activities_when_the_term_matches_via_notes(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(TERM_NOTES, matching=True)

    def test_it_does_not_read_a_strangers_activities_when_the_term_matches_nothing(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(NOWHERE, matching=False)


class TheMeasurementIsRealTests(_TripActivityCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_trip_via_title(self) -> None:
        results, _ = self.search(TERM_TITLE)
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's title search did not return their own trip, so the measured statement is not "
            "the one the defect is about",
        )

    def test_the_search_finds_the_viewers_own_trip_via_notes(self) -> None:
        results, _ = self.search(TERM_NOTES)
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's notes search did not return their own trip, so the measured statement is not "
            "the one the defect is about",
        )

    def test_the_term_that_matches_nothing_really_matches_nothing(self) -> None:
        self.seed_strangers_activities(SECOND_BATCH, matching=False)
        collides = TripActivity.objects.filter(title__icontains=NOWHERE) | TripActivity.objects.filter(
            notes__icontains=NOWHERE
        )
        self.assertEqual(
            collides.count(),
            0,
            "NOWHERE matched a real activity, so the non-matching variant is measuring something else",
        )

    def test_the_strangers_matching_activities_match_the_term(self) -> None:
        self.seed_strangers_activities(SECOND_BATCH, matching=True)
        matching = TripActivity.objects.filter(
            trip=self.their_trip, title__icontains=TERM_TITLE, notes__icontains=TERM_NOTES
        ).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded activities do not match the search terms"
        )

    def test_the_strangers_trip_is_not_visible_to_the_viewer(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripMembership

        access = Q(pk__in=TripMembership.objects.filter(profile=self.viewer).values("trip_id")) | Q(creator=self.viewer)
        self.assertFalse(
            Trip.objects.filter(pk=self.their_trip.pk).filter(access).exists(),
            "the stranger's trip is visible to the viewer, so reading its activities is correct",
        )

    def test_the_measured_statement_reads_the_trip_activity_table(self) -> None:
        captured, statements = self.activity_reading_statements(TERM_TITLE)
        self.assertTrue(statements, "the search issued no statement mentioning the trip-activity table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            TripActivity._meta.db_table,
            touched,
            f"no plan read the trip-activity table; relations read were {sorted(touched)}",
        )
