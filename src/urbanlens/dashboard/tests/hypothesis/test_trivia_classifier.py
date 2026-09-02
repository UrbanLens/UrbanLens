"""Tests for services.trivia.classifier - the shared content-moderation classifier.

The gateway is mocked throughout (no live AI call) - these tests verify the
mechanical contract only (prompt construction, response parsing, fail-closed
defaulting on every failure mode), NOT real model judgment quality. See
docs/PROBLEMS.md / the session's own notes for why a live eval against the
spec's adversarial examples wasn't possible in this environment (no AI
provider key configured).
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.trivia.classifier import classify_trivia_question


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


def _make_location() -> Location:
    return baker.make(Location, official_name="Old Armory")


class ClassifyTriviaQuestionTests(TestCase):
    def setUp(self) -> None:
        self.location = _make_location()

    def _classify(self, answer_token: str | None):
        with patch("urbanlens.dashboard.services.trivia.classifier.get_gateway", return_value=_FakeGateway(answer_token)):
            return classify_trivia_question("What year was it built?", "1937", self.location)

    def test_approve_token_approves(self) -> None:
        verdict = self._classify("APPROVE")
        self.assertTrue(verdict.approved)
        self.assertIsNone(verdict.reason)

    def test_reject_person_token(self) -> None:
        verdict = self._classify("REJECT_PERSON")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "person")

    def test_reject_bullying_token(self) -> None:
        verdict = self._classify("REJECT_BULLYING")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "bullying")

    def test_reject_ingroup_token(self) -> None:
        verdict = self._classify("REJECT_INGROUP")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "ingroup")

    def test_reject_off_topic_token(self) -> None:
        verdict = self._classify("REJECT_OFF_TOPIC")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "off_topic")

    def test_unparseable_response_fails_closed(self) -> None:
        verdict = self._classify("MAYBE_APPROVE_I_GUESS")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "unparseable")

    def test_no_answer_tag_fails_closed(self) -> None:
        verdict = self._classify("I think this one is fine.")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "unparseable")

    def test_gateway_returning_none_fails_closed(self) -> None:
        verdict = self._classify(None)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "ai_unavailable")

    def test_gateway_unavailable_fails_closed(self) -> None:
        with patch("urbanlens.dashboard.services.trivia.classifier.get_gateway", return_value=None):
            verdict = classify_trivia_question("What year was it built?", "1937", self.location)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "ai_unavailable")

    def test_prompt_and_answer_are_wrapped_as_inert_user_data(self) -> None:
        """wrap_user_data marks submitted text as data, not instructions, for the model to see -
        the gateway's own send_prompt (mocked away here) does the separate injection-scan/sanitize pass."""
        captured = {}

        class _CapturingGateway(_FakeGateway):
            def send_prompt(self, prompt: str, **kwargs) -> str | None:
                captured["prompt"] = prompt
                return self._answer

        with patch("urbanlens.dashboard.services.trivia.classifier.get_gateway", return_value=_CapturingGateway("<ANSWER>APPROVE</ANSWER>")):
            classify_trivia_question("What year was it built?", "1937", self.location)

        self.assertIn("<USER_DATA>", captured["prompt"])
        self.assertIn("What year was it built?", captured["prompt"])


class ClassifyTriviaQuestionProfileGateTests(TestCase):
    """The real ``get_gateway`` (unmocked) requires the submitter to hold ``SiteFeature.AI``.

    A submission from a profile without the feature fails closed exactly like
    a site-wide AI outage does (``ai_unavailable``) - per
    ``SiteSettings.ai_trivia_moderation_enabled``'s own help text, that holds
    the question in pending review rather than rejecting it; it does not
    require moderation to bypass the gate.
    """

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.profile = _make_profile(ai_enabled=True, external_apis_enabled=True)
        self.location = _make_location()

    def test_submitter_without_ai_feature_fails_closed(self) -> None:
        verdict = classify_trivia_question("What year was it built?", "1937", self.location, profile=self.profile)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "ai_unavailable")

    def test_no_submitting_profile_is_unaffected(self) -> None:
        """AI-generated questions (profile=None) skip the per-profile gate entirely."""
        with patch("urbanlens.dashboard.services.trivia.classifier.get_gateway", return_value=_FakeGateway("APPROVE")) as mock_get_gateway:
            verdict = classify_trivia_question("What year was it built?", "1937", self.location)
        self.assertTrue(verdict.approved)
        self.assertIsNone(mock_get_gateway.call_args.kwargs["profile"])
