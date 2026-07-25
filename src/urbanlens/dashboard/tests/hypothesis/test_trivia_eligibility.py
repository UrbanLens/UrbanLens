"""Tests for services.trivia.eligibility - "pinned by every participant" + in-rotation question."""

from __future__ import annotations

from itertools import count

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus, TriviaQuestionVote, TriviaQuestionVoteKind
from urbanlens.dashboard.services.trivia.eligibility import eligible_questions, has_eligible_questions

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_question(location: Location, *, status: str = TriviaQuestionStatus.APPROVED) -> TriviaQuestion:
    return baker.make(TriviaQuestion, location=location, source=TriviaQuestionSource.DETERMINISTIC, status=status, answer="1937")


class EligibleQuestionsTests(TestCase):
    def setUp(self) -> None:
        self.alice = _make_profile()
        self.bob = _make_profile()

    def test_no_profiles_returns_nothing(self) -> None:
        self.assertFalse(eligible_questions([]).exists())

    def test_pinned_location_with_no_question_is_not_eligible(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        self.assertEqual(list(eligible_questions([self.alice])), [])

    def test_pinned_location_with_an_approved_question_is_eligible(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        question = _make_question(location)
        self.assertEqual(list(eligible_questions([self.alice])), [question])

    def test_unpinned_location_is_never_eligible_even_with_a_question(self) -> None:
        location = _make_location()
        _make_question(location)
        self.assertEqual(list(eligible_questions([self.alice])), [])

    def test_pending_review_question_is_not_eligible(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        _make_question(location, status=TriviaQuestionStatus.PENDING_REVIEW)
        self.assertEqual(list(eligible_questions([self.alice])), [])

    def test_rejected_question_is_not_eligible(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        _make_question(location, status=TriviaQuestionStatus.REJECTED)
        self.assertEqual(list(eligible_questions([self.alice])), [])

    def test_location_must_be_pinned_by_every_participant(self) -> None:
        both_pinned = _make_location()
        only_alice = _make_location()
        baker.make(Pin, profile=self.alice, location=both_pinned)
        baker.make(Pin, profile=self.bob, location=both_pinned)
        baker.make(Pin, profile=self.alice, location=only_alice)
        both_question = _make_question(both_pinned)
        _make_question(only_alice)

        self.assertEqual(list(eligible_questions([self.alice, self.bob])), [both_question])

    def test_exclude_question_ids_removes_already_asked_questions(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        question = _make_question(location)
        self.assertEqual(list(eligible_questions([self.alice], exclude_question_ids=[question.pk])), [])

    def test_significantly_downvoted_question_drops_out_of_rotation(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        question = _make_question(location)
        for _ in range(3):
            baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.DOWNVOTE)

        self.assertEqual(list(eligible_questions([self.alice])), [])


class HasEligibleQuestionsTests(TestCase):
    """The cheap pre-check TriviaStartView uses before creating a solo session."""

    def test_false_for_a_profile_with_no_pins(self) -> None:
        profile = _make_profile()
        self.assertFalse(has_eligible_questions([profile]))

    def test_true_once_a_pinned_location_has_an_approved_question(self) -> None:
        profile = _make_profile()
        location = _make_location()
        baker.make(Pin, profile=profile, location=location)
        _make_question(location)
        self.assertTrue(has_eligible_questions([profile]))
