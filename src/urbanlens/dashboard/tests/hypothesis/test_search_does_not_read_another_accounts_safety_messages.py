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

**Fixed 2026-09-17.** ``messages`` crosses at *path*'s own first segment, so ``_semijoin`` now bounds
its filter by the outer queryset's own candidate primary keys, materialised as a concrete list first
rather than left as a nested subquery. See
``test_search_does_not_read_another_accounts_labels.py``'s module docstring for the measured
mechanism and ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for the full writeup.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
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

    def message_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the safety-message table.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search()
        table = SafetyCheckinMessage._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.message_reading_statements()
        self.assertTrue(
            statements, "no statement of the search mentioned the safety-message table, so nothing was measured"
        )
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_messages(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            captured, statements = self.message_reading_statements()
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's safety search read {before} rows, then {after} after a stranger's check-in "
                f"gained {count} {kind} messages - {after - before} more rows for a check-in the viewer does "
                f"not own. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsSafetyMessagesTests(_SafetyMessageCase):
    """One check-in's messages must not be read to answer a search for a different, owned check-in."""

    def test_it_does_not_read_a_strangers_matching_messages(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_messages(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
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
        captured, statements = self.message_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the safety-message table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            SafetyCheckinMessage._meta.db_table,
            touched,
            f"no plan read the safety-message table; relations read were {sorted(touched)}",
        )
