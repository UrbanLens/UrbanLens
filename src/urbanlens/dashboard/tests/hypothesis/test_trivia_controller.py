"""HTTP-level tests for controllers.trivia - auth boundaries, request validation, and answer-leak safety."""

from __future__ import annotations

from itertools import count
import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaSession

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class TriviaStartViewTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        self.question = baker.make(
            TriviaQuestion,
            location=self.location,
            source=TriviaQuestionSource.DETERMINISTIC,
            prompt="What year was The Armory built?",
            answer="1937",
        )
        self.client.force_login(self.profile.user)
        self.start_url = reverse("trivia.start")

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 302)

    def test_starting_a_session_returns_a_round_without_leaking_the_answer(self) -> None:
        response = self.client.post(self.start_url, {"total_rounds": "3"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["finished"])
        self.assertIn("round_id", data["round"])
        self.assertIn("prompt", data["round"])
        self.assertNotIn("1937", json.dumps(data))
        self.assertEqual(TriviaSession.objects.get(pk=data["session_id"]).total_rounds, 3)

    def test_no_eligible_questions_reports_error_code_without_creating_a_session(self) -> None:
        self.question.delete()
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"error_code": "no_eligible_questions"})
        self.assertFalse(TriviaSession.objects.exists())

    def test_invalid_difficulty_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"difficulty": "not-a-number"})
        self.assertEqual(response.status_code, 400)


class TriviaAnswerFlowTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(
            TriviaQuestion,
            location=self.location,
            source=TriviaQuestionSource.DETERMINISTIC,
            prompt="What year was The Armory built?",
            answer="1937",
        )
        self.client.force_login(self.profile.user)
        start_response = self.client.post(reverse("trivia.start"), {"total_rounds": "1"})
        self.session_id = start_response.json()["session_id"]
        self.round_id = start_response.json()["round"]["round_id"]

    def test_correct_answer_is_case_and_punctuation_insensitive(self) -> None:
        response = self.client.post(
            reverse("trivia.answer", kwargs={"session_id": self.session_id, "round_id": self.round_id}),
            {"answer": "  1937!  "},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["is_correct"])
        self.assertEqual(data["points"], 1000)
        self.assertEqual(data["answer"], "1937")

    def test_wrong_answer_still_reveals_the_answer_once_the_round_completes(self) -> None:
        response = self.client.post(
            reverse("trivia.answer", kwargs={"session_id": self.session_id, "round_id": self.round_id}),
            {"answer": "1999"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["is_correct"])
        self.assertEqual(data["points"], 0)
        self.assertEqual(data["answer"], "1937")

    def test_answering_twice_is_rejected(self) -> None:
        answer_url = reverse("trivia.answer", kwargs={"session_id": self.session_id, "round_id": self.round_id})
        self.client.post(answer_url, {"answer": "1937"})
        response = self.client.post(answer_url, {"answer": "1937"})
        self.assertEqual(response.status_code, 400)

    def test_another_profiles_session_404s_instead_of_403ing(self) -> None:
        other = _make_profile()
        self.client.force_login(other.user)
        response = self.client.get(reverse("trivia.round", kwargs={"session_id": self.session_id}))
        self.assertEqual(response.status_code, 404)

    def test_summary_reports_points_after_finishing_the_only_round(self) -> None:
        answer_url = reverse("trivia.answer", kwargs={"session_id": self.session_id, "round_id": self.round_id})
        self.client.post(answer_url, {"answer": "1937"})

        response = self.client.get(reverse("trivia.summary", kwargs={"session_id": self.session_id}))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["participants"][0]["total_points"], 1000)


class TriviaQuestionVoteViewTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        self.question = baker.make(
            TriviaQuestion,
            location=self.location,
            source=TriviaQuestionSource.DETERMINISTIC,
            answer="1937",
        )
        self.client.force_login(self.profile.user)
        self.vote_url = reverse("trivia.vote", kwargs={"question_id": self.question.pk})

    def test_cannot_vote_on_a_question_never_answered(self) -> None:
        response = self.client.post(self.vote_url, {"kind": "upvote"})
        self.assertEqual(response.status_code, 403)

    def test_can_vote_after_answering(self) -> None:
        start_response = self.client.post(reverse("trivia.start"), {"total_rounds": "1"})
        round_id = start_response.json()["round"]["round_id"]
        session_id = start_response.json()["session_id"]
        self.client.post(reverse("trivia.answer", kwargs={"session_id": session_id, "round_id": round_id}), {"answer": "1937"})

        response = self.client.post(self.vote_url, {"kind": "upvote"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"kind": "upvote"})

    def test_invalid_kind_is_rejected(self) -> None:
        response = self.client.post(self.vote_url, {"kind": "no_reaction"})
        self.assertEqual(response.status_code, 400)
