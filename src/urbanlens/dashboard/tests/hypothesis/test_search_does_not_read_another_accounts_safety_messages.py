"""Searching your own safety check-ins must not read chat messages on a check-in you do not own.

Same mechanism as P123 (``test_search_does_not_read_another_accounts_labels.py``):
``SafetySearchProvider.search`` feeds ``messages__body`` to ``apply_text``, and it crosses a to-many
relation (``SafetyCheckinMessage`` is a reverse foreign key, ``related_name="messages"``), so it goes
through the same unscoped ``_semijoin`` as labels do - an ``Exists(SafetyCheckin._base_manager.filter(...))``
subquery built from the *unfiltered* manager, run before the outer
``SafetyCheckin.objects.filter(profile=profile)`` ownership check narrows anything.

Unlike Article/Trip, a check-in has exactly one owning profile (no membership model), so the
stranger's check-in here is simply owned by a different profile - the same "not mine" shape as the
original labels reproduction, just for a different to-many relation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.explain import relations_read, rows_examined
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinMessage
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import SafetySearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through a chat message, never through title/plan_details.
TERM = "quokka"

#: A word the viewer never searches for, for messages that must be scanned to be rejected.
OTHER = "wombat"

#: Messages the stranger's check-in holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's messages were read.
TOLERANCE = 20

_REASON = (
    "Same mechanism as P123, over messages__body instead of labels__name: the message semi-join reads "
    "the whole dashboard_safety_checkin_messages table for a non-matching term, because a true negative "
    "can't short-circuit the scan. Fixing it needs the join reordered to drive from the check-in, not "
    "the message, same as the pins provider's own labels path."
)


class _SafetyMessageCase(TestCase):
    """A viewer with one check-in they own (one message matching the term), and a stranger's
    check-in the viewer does not own, whose unrelated messages share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.messaged = 0

        checkin_by = timezone.now() + timedelta(days=1)
        self.mine = baker.make(
            SafetyCheckin, profile=self.viewer, title="My Trip Plan", plan_details="", checkin_by=checkin_by
        )
        baker.make(SafetyCheckinMessage, checkin=self.mine, sender_profile=self.viewer, body=f"{TERM} mine")

        # The viewer does not own this check-in - inaccessible, so it must never appear as a search
        # candidate for the viewer.
        self.their_checkin = baker.make(
            SafetyCheckin, profile=self.stranger, title="Their Trip Plan", plan_details="", checkin_by=checkin_by
        )
        self.seed_strangers_messages(FIRST_BATCH)

    def seed_strangers_messages(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's check-in *count* more messages, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.messaged += 1
            baker.make(
                SafetyCheckinMessage,
                checkin=self.their_checkin,
                sender_profile=self.stranger,
                body=f"{word} theirs {self.messaged}",
            )
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {SafetyCheckinMessage._meta.db_table}, {SafetyCheckin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = SafetySearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def message_reading_statements(self) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
        _, captured = self.search()
        table = SafetyCheckinMessage._meta.db_table
        return [(sql, params) for sql, params in captured if table in sql]

    def rows_read_searching(self) -> int:
        statements = self.message_reading_statements()
        self.assertTrue(
            statements, "no statement of the search mentioned the safety-message table, so nothing was measured"
        )
        return sum(rows_examined(sql, params) for sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_messages(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            per_relation = [relations_read(sql, params) for sql, params in self.message_reading_statements()]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's safety search read {before} rows, then {after} after a stranger's check-in "
                f"gained {count} {kind} messages - {after - before} more rows for a check-in the viewer does "
                f"not own. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsSafetyMessagesTests(_SafetyMessageCase):
    """One check-in's messages must not be read to answer a search for a different, owned check-in."""

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_matching_messages(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_unrelated_messages(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_SafetyMessageCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_checkin(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the check-in whose message matches the term, so the "
            "measured statement is not the one the defect is about",
        )

    def test_the_strangers_matching_messages_match_the_term(self) -> None:
        self.seed_strangers_messages(SECOND_BATCH)
        matching = SafetyCheckinMessage.objects.filter(checkin=self.their_checkin, body__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded messages do not match the search term"
        )

    def test_the_strangers_unrelated_messages_do_not_match_the_term(self) -> None:
        self.seed_strangers_messages(SECOND_BATCH, matching=False)
        matching = SafetyCheckinMessage.objects.filter(checkin=self.their_checkin, body__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_checkin_is_not_visible_to_the_viewer(self) -> None:
        visible = SafetyCheckin.objects.filter(profile=self.viewer, pk=self.their_checkin.pk).count()
        self.assertEqual(
            visible, 0, "the stranger's check-in is visible to the viewer, so reading its messages is correct"
        )

    def test_the_measured_statement_reads_the_safety_message_table(self) -> None:
        statements = self.message_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the safety-message table")
        touched: set[str] = set()
        for sql, params in statements:
            touched.update(relations_read(sql, params))
        self.assertIn(
            SafetyCheckinMessage._meta.db_table,
            touched,
            f"no plan read the safety-message table; relations read were {sorted(touched)}",
        )
