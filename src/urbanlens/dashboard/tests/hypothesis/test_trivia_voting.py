"""Tests for services.trivia.voting - vote recording and weighted scoring."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import (
    TriviaQuestion,
    TriviaQuestionSource,
    TriviaQuestionVote,
    TriviaQuestionVoteKind,
)
from urbanlens.dashboard.services.trivia.voting import (
    DOWNVOTE_WEIGHT,
    NO_REACTION_WEIGHT,
    REPORT_WEIGHT,
    UPVOTE_WEIGHT,
    backfill_no_reaction,
    effective_score,
    record_vote,
)


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_question() -> TriviaQuestion:
    return baker.make(
        TriviaQuestion, location=baker.make(Location), source=TriviaQuestionSource.DETERMINISTIC, answer="1937"
    )


class EffectiveScoreTests(TestCase):
    def test_never_voted_question_scores_zero(self) -> None:
        self.assertEqual(effective_score(_make_question()), 0.0)

    def test_a_single_upvote(self) -> None:
        question = _make_question()
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.UPVOTE)
        self.assertAlmostEqual(effective_score(question), UPVOTE_WEIGHT)

    def test_a_single_downvote(self) -> None:
        question = _make_question()
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.DOWNVOTE)
        self.assertAlmostEqual(effective_score(question), DOWNVOTE_WEIGHT)

    def test_a_report_outweighs_a_downvote(self) -> None:
        question = _make_question()
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.REPORT)
        self.assertLess(effective_score(question), DOWNVOTE_WEIGHT)
        self.assertAlmostEqual(effective_score(question), REPORT_WEIGHT)

    def test_no_reaction_passive_default_matches_spec_value(self) -> None:
        question = _make_question()
        baker.make(
            TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.NO_REACTION
        )
        self.assertAlmostEqual(effective_score(question), 0.05)
        self.assertAlmostEqual(NO_REACTION_WEIGHT, 0.05)

    def test_enough_downvotes_alone_can_take_a_question_negative(self) -> None:
        """Unlike SpotGuessr's photo thumbs-down, Trivia's downvote must carry real weight
        on its own - the spec wants downvotes alone able to retire a question."""
        question = _make_question()
        for _ in range(3):
            baker.make(
                TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.DOWNVOTE
            )
        self.assertLess(effective_score(question), 0.0)

    def test_mixed_votes_sum_correctly(self) -> None:
        question = _make_question()
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.UPVOTE)
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.UPVOTE)
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.DOWNVOTE)
        self.assertAlmostEqual(effective_score(question), 2 * UPVOTE_WEIGHT + DOWNVOTE_WEIGHT)


class RecordVoteTests(TestCase):
    def test_records_a_new_vote(self) -> None:
        question = _make_question()
        profile = _make_profile()
        vote = record_vote(question, profile, TriviaQuestionVoteKind.UPVOTE)
        self.assertEqual(vote.kind, TriviaQuestionVoteKind.UPVOTE)

    def test_changing_a_vote_overwrites_the_prior_one(self) -> None:
        question = _make_question()
        profile = _make_profile()
        record_vote(question, profile, TriviaQuestionVoteKind.UPVOTE)
        record_vote(question, profile, TriviaQuestionVoteKind.REPORT)

        self.assertEqual(TriviaQuestionVote.objects.filter(question=question, profile=profile).count(), 1)
        self.assertAlmostEqual(effective_score(question), REPORT_WEIGHT)


class BackfillNoReactionTests(TestCase):
    def test_backfills_a_profile_with_no_prior_vote(self) -> None:
        question = _make_question()
        profile = _make_profile()
        backfill_no_reaction(question, [profile])
        vote = TriviaQuestionVote.objects.get(question=question, profile=profile)
        self.assertEqual(vote.kind, TriviaQuestionVoteKind.NO_REACTION)

    def test_never_clobbers_an_explicit_vote(self) -> None:
        question = _make_question()
        profile = _make_profile()
        record_vote(question, profile, TriviaQuestionVoteKind.UPVOTE)
        backfill_no_reaction(question, [profile])

        vote = TriviaQuestionVote.objects.get(question=question, profile=profile)
        self.assertEqual(vote.kind, TriviaQuestionVoteKind.UPVOTE)
