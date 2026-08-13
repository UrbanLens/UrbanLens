"""Concurrency tests for cross-session tentative-answer accumulation.

``record_tentative_answers`` is check-then-act: find a matching tentative answer,
then either bump its ``support_count`` or create one. Two Consensus rounds
resolving at once for the same wiki is ordinary - separate sessions play the same
popular wiki concurrently - and unserialised, both reads miss and both write.

The damage is quiet and it is the whole point of the feature: support for one
value splits across two rows, so a value the community actually agreed on never
reaches the promotion threshold.

Uses ``TransactionTestCase`` (not the project's default ``TestCase``) because the
threads need to see each other's committed rows, which a single wrapping
transaction would hide.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.models.consensus.model import ConsensusAnswer, ConsensusFieldKind, ConsensusRound, ConsensusTentativeAnswer
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus import tentative


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class TentativeAnswerRaceTests(TransactionTestCase):
    """Cache pinned to locmem and background dispatch stubbed: the subject here is which
    rows end up in the table, and the default Redis-backed cache/broker is unreachable
    under the suite's localhost-only network guard."""

    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.wiki = baker.make(Wiki, location=self.location, name="Powerhouse", officially_created=True)

    def _round_with_answer(self, text: str) -> tuple[ConsensusRound, ConsensusAnswer]:
        round_ = baker.make(ConsensusRound, wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_NAME)
        answer = baker.make(ConsensusAnswer, round=round_, text_value=text)
        return round_, answer

    def _record_in_thread(self, round_: ConsensusRound, answer: ConsensusAnswer, barrier: threading.Barrier, errors: list) -> threading.Thread:
        def run() -> None:
            try:
                # Both threads are inside the call before either touches the table.
                barrier.wait(timeout=10)
                tentative.record_tentative_answers(round_, [answer])
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        return threading.Thread(target=run)

    def test_two_rounds_proposing_the_same_name_at_once_produce_one_row(self) -> None:
        round_a, answer_a = self._round_with_answer("Turbine Hall")
        round_b, answer_b = self._round_with_answer("Turbine Hall")

        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        threads = [
            self._record_in_thread(round_a, answer_a, barrier, errors),
            self._record_in_thread(round_b, answer_b, barrier, errors),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [], f"round resolution raised under concurrency: {errors}")

        rows = ConsensusTentativeAnswer.objects.filter(wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_NAME)
        self.assertEqual(rows.count(), 1, "the same proposed value must accumulate onto one row, not split across two")
        self.assertEqual(rows.first().support_count, 2, "both proposals must count toward the same value's support")

    def test_sequential_proposals_still_accumulate(self) -> None:
        """The ordinary, uncontended path the locking must not change."""
        round_a, answer_a = self._round_with_answer("Turbine Hall")
        round_b, answer_b = self._round_with_answer("turbine hall")

        tentative.record_tentative_answers(round_a, [answer_a])
        tentative.record_tentative_answers(round_b, [answer_b])

        rows = ConsensusTentativeAnswer.objects.filter(wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_NAME)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().support_count, 2)

    def test_distinct_values_still_get_their_own_rows(self) -> None:
        round_a, answer_a = self._round_with_answer("Turbine Hall")
        round_b, answer_b = self._round_with_answer("Boiler House")

        tentative.record_tentative_answers(round_a, [answer_a])
        tentative.record_tentative_answers(round_b, [answer_b])

        self.assertEqual(ConsensusTentativeAnswer.objects.filter(wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_NAME).count(), 2)
