"""Searching your own comments must not read comments on somebody else's pin.

``test_search_does_not_read_another_accounts_commented_trips.py`` covers the trip half of
:class:`CommentSearchProvider`. This is the other half - ``Comment.objects`` scoped by
``Q(profile=me) | Q(pin__profile=me) | Q(wiki__location_id__in=...)`` (``providers.py``). Two of
those three disjuncts reach the scope through a join, so the whole predicate cannot be answered
from an index on the comment table and Postgres reads the table instead.

Measured at capacity scale before this file existed: a sequential scan of 8,000 comments, of which
7,980 were discarded, for a viewer with 20 of their own - 34.85 ms whose cost is the site's comment
count rather than the viewer's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import CommentSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through a comment on the viewer's own pin.
TERM = "quokka"

#: A word the viewer never searches for, for comments that must be scanned to be rejected.
OTHER = "wombat"

#: Comments the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's comments were read.
TOLERANCE = 20


class _PinCommentCase(TestCase):
    """A viewer who has commented on their own pin, and a stranger commenting on theirs."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.commented = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.my_pin = baker.make(Pin, profile=self.viewer, location=my_location, name="My Pin", description="")
        baker.make(Comment, pin=self.my_pin, profile=self.viewer, text=f"{TERM} mine")

        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_pin = baker.make(
            Pin, profile=self.stranger, location=their_location, name="Their Pin", description=""
        )
        self.seed_strangers_comments(FIRST_BATCH)

    def seed_strangers_comments(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's pin *count* more comments, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.commented += 1
            baker.make(Comment, pin=self.their_pin, profile=self.stranger, text=f"{word} theirs {self.commented}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {Comment._meta.db_table}, {Pin._meta.db_table}")  # noqa: S608

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
        """The full capture, and the statements in it that read the comment table.

        Both are returned so a caller can pass a filtered statement's original position to
        ``session_preamble``: a statement's plan depends on the session GUCs active when it ran.
        """
        _, captured = self.search()
        table = Comment._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.comment_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the comment table, so nothing was measured")
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
                f"{kind} comments to a pin of their own - {after - before} more rows for a pin the viewer cannot "
                f"see. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPinCommentsTests(_PinCommentCase):
    """One account's pin comments must not be read to answer a different account's comment search."""

    def test_it_does_not_read_a_strangers_matching_comments(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_comments(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PinCommentCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_comment(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.title for result in results],
            [f"Comment on {self.my_pin.effective_name}"],
            "the viewer's search did not return the comment on their own pin, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_comments_match_the_term(self) -> None:
        self.seed_strangers_comments(SECOND_BATCH)
        matching = Comment.objects.filter(pin=self.their_pin, text__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded comments do not match the search term"
        )

    def test_the_strangers_unrelated_comments_do_not_match_the_term(self) -> None:
        self.seed_strangers_comments(SECOND_BATCH, matching=False)
        matching = Comment.objects.filter(pin=self.their_pin, text__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_pin_is_not_visible_to_the_viewer(self) -> None:
        self.assertEqual(Pin.objects.filter(profile=self.viewer, pk=self.their_pin.pk).count(), 0)

    def test_the_measured_statement_reads_the_comment_table(self) -> None:
        captured, statements = self.comment_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the comment table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            Comment._meta.db_table, touched, f"no plan read the comment table; relations read were {sorted(touched)}"
        )
