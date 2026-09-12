"""Two rounds rating the same question at once must both count."""

from __future__ import annotations

from unittest import mock

from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import (
    PlayerTriviaRating,
    TriviaAnswer,
    TriviaAnswerMatchKind,
    TriviaQuestion,
    TriviaQuestionRating,
    TriviaQuestionSource,
    TriviaRound,
    TriviaSession,
    TriviaSessionParticipant,
)
from urbanlens.dashboard.services.trivia.ratings import apply_round_ratings


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RoundRatingRaceTests(TransactionTestCase):
    """Cache pinned to locmem and background dispatch stubbed, matching the consensus race tests."""

    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        self.question = baker.make(
            TriviaQuestion,
            location=baker.make(Location, latitude=40.0, longitude=-74.0),
            source=TriviaQuestionSource.DETERMINISTIC,
            answer="1937",
        )

    def _round_and_answer(self) -> tuple[TriviaRound, TriviaAnswer]:
        profile = Profile.objects.get(user=baker.make("auth.User"))
        session = baker.make(TriviaSession, host_profile=profile)
        baker.make(TriviaSessionParticipant, session=session, profile=profile)
        round_ = baker.make(TriviaRound, session=session, question=self.question, sequence_index=0)
        answer = baker.make(
            TriviaAnswer,
            round=round_,
            profile=profile,
            is_correct=True,
            matched_via=TriviaAnswerMatchKind.EXACT,
            points=1000,
        )
        return round_, answer

    def test_two_rounds_on_one_question_both_count(self) -> None:
        # The rating row must already exist.
        seeded_round, seeded_answer = self._round_and_answer()
        apply_round_ratings(seeded_round, [seeded_answer])
        self.assertEqual(TriviaQuestionRating.objects.get(question=self.question).games_played, 1)

        rounds = [self._round_and_answer(), self._round_and_answer()]

        run_concurrently([lambda pair=pair: apply_round_ratings(pair[0], [pair[1]]) for pair in rounds])

        rating = TriviaQuestionRating.objects.get(question=self.question)
        self.assertEqual(rating.games_played, 3, "a concurrent round's rating update was discarded")

    def test_sequential_rounds_still_accumulate(self) -> None:
        """The ordinary, uncontended path the locking must not change."""
        for round_, answer in (self._round_and_answer(), self._round_and_answer()):
            apply_round_ratings(round_, [answer])

        rating = TriviaQuestionRating.objects.get(question=self.question)
        self.assertEqual(rating.games_played, 2)
        self.assertEqual(PlayerTriviaRating.objects.count(), 2, "each player should still get their own rating row")

    def test_a_players_own_rating_still_moves(self) -> None:
        """The complement: locking must not stop the per-player update landing."""
        round_, answer = self._round_and_answer()

        apply_round_ratings(round_, [answer])

        player_rating = PlayerTriviaRating.objects.get(profile=answer.profile)
        self.assertEqual(player_rating.games_played, 1)
        self.assertGreater(player_rating.mu, 0)
