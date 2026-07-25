"""Tests for services.trivia.answer_check - AI fallback for a non-exact-match answer."""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.trivia.answer_check import is_answer_equivalent


class _FakeGateway:
    def __init__(self, answer: str | None):
        self._answer = answer
        self.model = "fake-model"

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        return self._answer

    @property
    def cost(self):
        from decimal import Decimal

        return Decimal("0.001")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _grant_ai_to_everyone() -> None:
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)


class IsAnswerEquivalentTests(TestCase):
    def test_without_ai_feature_never_calls_the_gateway(self) -> None:
        # A prior user absorbs the first-user site-admin bootstrap promotion
        # (see promote_first_user_if_needed) - a site admin bypasses every
        # SiteFeature gate, which would otherwise make this profile appear
        # to have AI access it was never actually granted.
        baker.make("auth.User")
        profile = _make_profile()
        with patch("urbanlens.dashboard.services.trivia.answer_check.get_gateway") as mock_get_gateway:
            result = is_answer_equivalent("nineteen thirty seven", "1937", profile=profile)
        self.assertFalse(result)
        mock_get_gateway.assert_not_called()

    def test_match_token_returns_true(self) -> None:
        _grant_ai_to_everyone()
        profile = _make_profile()
        with patch("urbanlens.dashboard.services.trivia.answer_check.get_gateway", return_value=_FakeGateway("MATCH")):
            self.assertTrue(is_answer_equivalent("nineteen thirty seven", "1937", profile=profile))

    def test_no_match_token_returns_false(self) -> None:
        _grant_ai_to_everyone()
        profile = _make_profile()
        with patch("urbanlens.dashboard.services.trivia.answer_check.get_gateway", return_value=_FakeGateway("NO_MATCH")):
            self.assertFalse(is_answer_equivalent("nineteen forty two", "1937", profile=profile))

    def test_gateway_unavailable_returns_false(self) -> None:
        _grant_ai_to_everyone()
        profile = _make_profile()
        with patch("urbanlens.dashboard.services.trivia.answer_check.get_gateway", return_value=None):
            self.assertFalse(is_answer_equivalent("nineteen thirty seven", "1937", profile=profile))

    def test_no_response_returns_false(self) -> None:
        _grant_ai_to_everyone()
        profile = _make_profile()
        with patch("urbanlens.dashboard.services.trivia.answer_check.get_gateway", return_value=_FakeGateway(None)):
            self.assertFalse(is_answer_equivalent("nineteen thirty seven", "1937", profile=profile))


class SubmitAnswerAiFallbackWiringTests(TestCase):
    """Verifies services.trivia.session.submit_answer actually consults the AI fallback on mismatch."""

    def setUp(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaRound, TriviaSession, TriviaSessionParticipant

        self.profile = _make_profile()
        question = baker.make(TriviaQuestion, location=baker.make(Location), source=TriviaQuestionSource.DETERMINISTIC, answer="1937")
        session = baker.make(TriviaSession, host_profile=self.profile)
        baker.make(TriviaSessionParticipant, session=session, profile=self.profile)
        self.round_ = baker.make(TriviaRound, session=session, question=question, sequence_index=0)

    def test_ai_equivalent_answer_is_marked_correct(self) -> None:
        from urbanlens.dashboard.models.trivia.model import TriviaAnswerMatchKind
        from urbanlens.dashboard.services.trivia.session import submit_answer

        with patch("urbanlens.dashboard.services.trivia.session.is_answer_equivalent", return_value=True):
            answer = submit_answer(self.round_, self.profile, "nineteen thirty seven")

        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.matched_via, TriviaAnswerMatchKind.AI)

    def test_ai_says_not_equivalent_stays_wrong(self) -> None:
        from urbanlens.dashboard.models.trivia.model import TriviaAnswerMatchKind
        from urbanlens.dashboard.services.trivia.session import submit_answer

        with patch("urbanlens.dashboard.services.trivia.session.is_answer_equivalent", return_value=False):
            answer = submit_answer(self.round_, self.profile, "totally wrong")

        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.matched_via, TriviaAnswerMatchKind.EXACT)

    def test_exact_match_never_consults_ai(self) -> None:
        from urbanlens.dashboard.services.trivia.session import submit_answer

        with patch("urbanlens.dashboard.services.trivia.session.is_answer_equivalent") as mock_equivalent:
            submit_answer(self.round_, self.profile, "1937")
        mock_equivalent.assert_not_called()
