"""Searching your own trips must not read comment rows belonging to a trip you are not on.

Same mechanism as P123 (``test_search_does_not_read_another_accounts_labels.py``):
``TripSearchProvider.search`` feeds ``comments__text`` to ``apply_text``, and it crosses a to-many
relation (``TripComment`` is a reverse foreign key, ``related_name="comments"``), so it goes through
the same unscoped ``_semijoin`` as labels do - an ``Exists(Trip._base_manager.filter(...))``
subquery built from the *unfiltered* manager, run before the outer membership check
(``Trip.objects.filter(Q(pk__in=...) | Q(creator=profile))``) narrows anything.

**Fixed 2026-09-17.** ``comments`` crosses at *path*'s own first segment, so ``_semijoin`` now bounds
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
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import TripSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through a comment, never through the trip's own name/description.
TERM = "quokka"

#: A word the viewer never searches for, for comments that must be scanned to be rejected.
OTHER = "wombat"

#: Comments the stranger's trip holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's comments were read.
TOLERANCE = 20


class _TripCommentCase(TestCase):
    """A viewer with one trip they belong to (one comment matching the term), and a stranger's trip
    the viewer is not a member of, whose unrelated comments share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.commented = 0

        self.mine = baker.make(Trip, creator=self.viewer, name="My Trip", description="")
        baker.make(TripComment, trip=self.mine, author=self.viewer, text=f"{TERM} mine")

        # The viewer is neither the creator nor a member of this trip - inaccessible, so it must
        # never appear as a search candidate for the viewer.
        self.their_trip = baker.make(Trip, creator=self.stranger, name="Their Trip", description="")
        self.seed_strangers_comments(FIRST_BATCH)

    def seed_strangers_comments(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's trip *count* more comments, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.commented += 1
            baker.make(TripComment, trip=self.their_trip, author=self.stranger, text=f"{word} theirs {self.commented}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {TripComment._meta.db_table}, {Trip._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = TripSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def comment_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the trip-comment table.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search()
        table = TripComment._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.comment_reading_statements()
        self.assertTrue(
            statements, "no statement of the search mentioned the trip-comment table, so nothing was measured"
        )
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_comments(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            captured, statements = self.comment_reading_statements()
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's trip search read {before} rows, then {after} after a stranger's trip gained "
                f"{count} {kind} comments - {after - before} more rows for a trip the viewer isn't on. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsTripCommentsTests(_TripCommentCase):
    """One trip's comments must not be read to answer a search for a different, accessible trip."""

    def test_it_does_not_read_a_strangers_matching_comments(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_comments(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_TripCommentCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_trip(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the trip whose comment matches the term, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_comments_match_the_term(self) -> None:
        self.seed_strangers_comments(SECOND_BATCH)
        matching = TripComment.objects.filter(trip=self.their_trip, text__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded comments do not match the search term"
        )

    def test_the_strangers_unrelated_comments_do_not_match_the_term(self) -> None:
        self.seed_strangers_comments(SECOND_BATCH, matching=False)
        matching = TripComment.objects.filter(trip=self.their_trip, text__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_trip_is_not_visible_to_the_viewer(self) -> None:
        access = Q(pk__in=TripMembership.objects.filter(profile=self.viewer).values("trip_id")) | Q(creator=self.viewer)
        self.assertFalse(
            Trip.objects.filter(pk=self.their_trip.pk).filter(access).exists(),
            "the stranger's trip is visible to the viewer, so reading its comments is correct",
        )

    def test_the_measured_statement_reads_the_trip_comment_table(self) -> None:
        captured, statements = self.comment_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the trip-comment table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            TripComment._meta.db_table,
            touched,
            f"no plan read the trip-comment table; relations read were {sorted(touched)}",
        )
