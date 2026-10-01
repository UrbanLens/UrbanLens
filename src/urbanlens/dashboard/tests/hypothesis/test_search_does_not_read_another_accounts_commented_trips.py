"""Searching your own comments must not read comments on a trip you do not belong to.

``test_search_does_not_read_another_accounts_trip_comments.py`` covers the same table from
``TripSearchProvider``, whose access scope crosses ``comments`` from the trip side. This one drives
``CommentSearchProvider``, which reaches the table the other way round - ``TripComment.objects``
scoped by ``trip__profiles=profile`` (``providers.py``) - and that path was never covered. It is
the one to-many access scope in the module that does not go through the bounded semi-join, which is
why it is measured separately rather than assumed to inherit the other's fix.

Measured at capacity scale before this file existed (docs/PROBLEMS.md P132): that statement was
30 ms and a sequential scan of 50,000 trip comments, on a population where the viewer belonged to a
handful of trips.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import CommentSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through a comment on the viewer's own trip.
TERM = "quokka"

#: A word the viewer never searches for, for comments that must be scanned to be rejected.
OTHER = "wombat"

#: Comments the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's comments were read.
TOLERANCE = 20


class _CommentedTripCase(TestCase):
    """A viewer who belongs to one commented trip, and a stranger's trip sharing the same table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.commented = 0

        self.mine = baker.make(Trip, creator=self.viewer, name="My Trip", description="")
        # CommentSearchProvider scopes by membership alone, so creating the trip is not enough.
        baker.make(TripMembership, trip=self.mine, profile=self.viewer)
        baker.make(TripComment, trip=self.mine, author=self.viewer, text=f"{TERM} mine")

        self.their_trip = baker.make(Trip, creator=self.stranger, name="Their Trip", description="")
        baker.make(TripMembership, trip=self.their_trip, profile=self.stranger)
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
        """Run the viewer's bare-term comment search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = CommentSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def comment_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the trip-comment table.

        Both are returned so a caller can pass a filtered statement's original position to
        ``session_preamble``: a statement's plan depends on the session GUCs active when it ran,
        and those are only meaningful against the one capture they came from.
        """
        _, captured = self.search()
        table = TripComment._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.comment_reading_statements()
        self.assertTrue(
            statements,
            "no statement of the search mentioned the trip-comment table, so nothing was measured",
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
                f"the viewer's comment search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} comments to a trip of their own - {after - before} more rows for a trip the viewer "
                f"does not belong to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsCommentedTripsTests(_CommentedTripCase):
    """One account's trip comments must not be read to answer a different account's comment search."""

    def test_it_does_not_read_a_strangers_matching_comments(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_comments(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_CommentedTripCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_comment(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.title for result in results],
            [f"Comment on {self.mine.name}"],
            "the viewer's search did not return the comment on their own trip, so the measured "
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
        visible = Trip.objects.filter(profiles=self.viewer, pk=self.their_trip.pk).count()
        self.assertEqual(visible, 0, "the stranger's trip is visible to the viewer, so reading its comments is correct")

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
