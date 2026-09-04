"""Tests for services.trivia.ratings.apply_round_ratings.

The underlying Glicko-2 math (services.spotguessr.glicko2) is already
exhaustively tested against Glickman's own worked example in
test_spotguessr_glicko2.py and reused here unmodified - these tests only
verify the ORM-facing wiring: which side is treated as which opponent, and
that both PlayerTriviaRating and TriviaQuestionRating actually get updated.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
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


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_round_and_answer(profile: Profile, *, is_correct: bool) -> tuple[TriviaRound, TriviaAnswer]:
    question = baker.make(
        TriviaQuestion, location=baker.make(Location), source=TriviaQuestionSource.DETERMINISTIC, answer="1937"
    )
    session = baker.make(TriviaSession, host_profile=profile)
    baker.make(TriviaSessionParticipant, session=session, profile=profile)
    round_ = baker.make(TriviaRound, session=session, question=question, sequence_index=0)
    answer = baker.make(
        TriviaAnswer,
        round=round_,
        profile=profile,
        is_correct=is_correct,
        matched_via=TriviaAnswerMatchKind.EXACT,
        points=1000 if is_correct else 0,
    )
    return round_, answer


class ApplyRoundRatingsTests(TestCase):
    def test_no_answers_is_a_no_op(self) -> None:
        question = baker.make(
            TriviaQuestion, location=baker.make(Location), source=TriviaQuestionSource.DETERMINISTIC, answer="1937"
        )
        session = baker.make(TriviaSession, host_profile=_make_profile())
        round_ = baker.make(TriviaRound, session=session, question=question, sequence_index=0)
        apply_round_ratings(round_, [])
        self.assertFalse(PlayerTriviaRating.objects.exists())
        self.assertFalse(TriviaQuestionRating.objects.exists())

    def test_answering_correctly_raises_the_players_rating(self) -> None:
        profile = _make_profile()
        round_, answer = _make_round_and_answer(profile, is_correct=True)

        apply_round_ratings(round_, [answer])

        rating = PlayerTriviaRating.objects.get(profile=profile)
        self.assertGreater(rating.mu, 0.0)
        self.assertEqual(rating.games_played, 1)
        self.assertIsNotNone(rating.last_played_at)

    def test_answering_incorrectly_lowers_the_players_rating(self) -> None:
        profile = _make_profile()
        round_, answer = _make_round_and_answer(profile, is_correct=False)

        apply_round_ratings(round_, [answer])

        rating = PlayerTriviaRating.objects.get(profile=profile)
        self.assertLess(rating.mu, 0.0)

    def test_a_question_everyone_answers_correctly_gets_easier(self) -> None:
        """A question every participant nails is 'losing' against the field -
        its difficulty rating (mu) should fall, mirroring LocationModeRating."""
        profile = _make_profile()
        round_, answer = _make_round_and_answer(profile, is_correct=True)

        apply_round_ratings(round_, [answer])

        question_rating = TriviaQuestionRating.objects.get(question=round_.question)
        self.assertLess(question_rating.mu, 0.0)
        self.assertEqual(question_rating.games_played, 1)
        self.assertIsNotNone(question_rating.last_asked_at)

    def test_a_question_everyone_misses_gets_harder(self) -> None:
        profile = _make_profile()
        round_, answer = _make_round_and_answer(profile, is_correct=False)

        apply_round_ratings(round_, [answer])

        question_rating = TriviaQuestionRating.objects.get(question=round_.question)
        self.assertGreater(question_rating.mu, 0.0)

    def test_calling_twice_double_counts_games_played(self) -> None:
        """apply_round_ratings has no internal idempotency guard - session.submit_answer
        is responsible for calling it exactly once per round; this documents that contract."""
        profile = _make_profile()
        round_, answer = _make_round_and_answer(profile, is_correct=True)

        apply_round_ratings(round_, [answer])
        apply_round_ratings(round_, [answer])

        self.assertEqual(PlayerTriviaRating.objects.get(profile=profile).games_played, 2)
