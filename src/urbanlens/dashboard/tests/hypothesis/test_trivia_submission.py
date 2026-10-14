"""Tests for services.trivia.submission - user-submitted question intake and classification."""

from __future__ import annotations

from unittest.mock import Mock, patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus
from urbanlens.dashboard.plugins.points import TRIVIA_QUESTION_MODERATORS
from urbanlens.dashboard.services.trivia.moderation import ModerationVerdict, TriviaQuestionModerator
from urbanlens.dashboard.services.trivia.submission import classify_and_update, submit_user_question


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class SubmitUserQuestionTests(TestCase):
    def test_creates_a_pending_review_question(self) -> None:
        profile = _make_profile()
        location = baker.make(Location)

        with (
            patch("urbanlens.dashboard.services.trivia.submission._enqueue_classification") as mock_enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            question = submit_user_question(profile, location, "What year was it built?", "1937")

        self.assertEqual(question.source, TriviaQuestionSource.USER_SUBMITTED)
        self.assertEqual(question.status, TriviaQuestionStatus.PENDING_REVIEW)
        self.assertEqual(question.submitted_by_id, profile.pk)
        self.assertEqual(question.answer_normalized, "1937")
        mock_enqueue.assert_called_once_with(question.pk)

    def test_prompt_and_answer_are_truncated(self) -> None:
        profile = _make_profile()
        location = baker.make(Location)
        with (
            patch("urbanlens.dashboard.services.trivia.submission._enqueue_classification"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            question = submit_user_question(profile, location, "Q" * 1000, "A" * 1000)
        self.assertEqual(len(question.prompt), 500)
        self.assertEqual(len(question.answer), 255)


class _Moderator(TriviaQuestionModerator):
    def __init__(self, verdict: ModerationVerdict | None) -> None:
        self.verdict = verdict
        self.asked = Mock()

    def moderate(
        self, prompt: str, answer: str, location: Location, *, profile: Profile | None
    ) -> ModerationVerdict | None:
        self.asked(prompt, answer, location, profile=profile)
        return self.verdict


class ClassifyAndUpdateTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = baker.make(Location)
        self.question = baker.make(
            TriviaQuestion,
            location=self.location,
            source=TriviaQuestionSource.USER_SUBMITTED,
            status=TriviaQuestionStatus.PENDING_REVIEW,
            submitted_by=self.profile,
            answer="1937",
        )

    def _classify(self, *moderators: TriviaQuestionModerator) -> None:
        with TRIVIA_QUESTION_MODERATORS.overridden(moderators):
            classify_and_update(self.question)
        self.question.refresh_from_db()

    def test_approved_verdict_flips_status_to_approved(self) -> None:
        self._classify(_Moderator(ModerationVerdict(approved=True)))
        self.assertEqual(self.question.status, TriviaQuestionStatus.APPROVED)
        self.assertIsNone(self.question.rejection_reason)

    def test_rejected_verdict_flips_status_to_rejected_with_reason(self) -> None:
        self._classify(_Moderator(ModerationVerdict(approved=False, reason="person")))
        self.assertEqual(self.question.status, TriviaQuestionStatus.REJECTED)
        self.assertEqual(self.question.rejection_reason, "person")

    def test_an_abstaining_moderator_leaves_the_question_pending(self) -> None:
        """A moderator that cannot decide must never mass-reject submissions - see classify_and_update's docstring."""
        self._classify(_Moderator(None))
        self.assertEqual(self.question.status, TriviaQuestionStatus.PENDING_REVIEW)

    def test_no_moderator_leaves_the_question_pending(self) -> None:
        self._classify()
        self.assertEqual(self.question.status, TriviaQuestionStatus.PENDING_REVIEW)

    def test_the_next_moderator_decides_when_the_first_abstains(self) -> None:
        abstains, approves = _Moderator(None), _Moderator(ModerationVerdict(approved=True))
        self._classify(abstains, approves)
        self.assertEqual(self.question.status, TriviaQuestionStatus.APPROVED)

    def test_the_first_verdict_is_final(self) -> None:
        rejects, approves = (
            _Moderator(ModerationVerdict(approved=False, reason="person")),
            _Moderator(ModerationVerdict(approved=True)),
        )
        self._classify(rejects, approves)
        self.assertEqual(self.question.status, TriviaQuestionStatus.REJECTED)
        approves.asked.assert_not_called()

    def test_already_decided_question_is_a_no_op(self) -> None:
        self.question.status = TriviaQuestionStatus.APPROVED
        self.question.save(update_fields=["status"])

        moderator = _Moderator(ModerationVerdict(approved=False, reason="person"))
        self._classify(moderator)
        moderator.asked.assert_not_called()
        self.assertEqual(self.question.status, TriviaQuestionStatus.APPROVED)
