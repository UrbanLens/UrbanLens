"""Tests for the external API's AI assistant domain.

Never calls a real model provider - every test patches ``get_gateway`` at
``services.ai.assistant`` (the same seam ``test_ai_assistant.py`` patches for
the internal chat view) with a scripted stub. The one thing worth a dedicated
regression test here is the bug fixed alongside this domain: ``run_assistant_turn``
previously never called ``log_api_call``, so AI-assistant spend was invisible
to the cost-reporting tables every other gateway-backed feature feeds.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.ai.assistant import MAX_HISTORY_ENTRIES, MAX_MESSAGE_CHARS
from urbanlens.dashboard.services.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _StubGateway:
    """Feeds a scripted sequence of answers to the loop.

    Carries ``model``/``cost`` because ``run_assistant_turn`` reads both -
    once, in its ``finally`` block - the same shape a real ``LLMGateway``
    always provides.
    """

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.model = "gpt-5-nano"
        self.cost = Decimal("0.01")

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        return self.answers.pop(0) if self.answers else None


class _AssistantApiTestCase(TestCase):
    """Shared fixture: a key owner with an assistant:write-scoped key."""

    def setUp(self) -> None:
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.raw_key = self._key_with_scopes([ApiKeyScope.ASSISTANT_WRITE.value])

    def _key_with_scopes(self, scopes: list[str], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(user or self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw


class AssistantMessageTests(_AssistantApiTestCase):
    """POST /assistant/message/ - a stateless chat turn."""

    def test_missing_scope_is_refused(self) -> None:
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        response = self.client.post(reverse("external_api:assistant.message"), {"message": "hi"}, content_type="application/json", **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_reply_and_history_round_trip(self) -> None:
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=_StubGateway(['{"reply": "Hi there!"}'])):
            response = self.client.post(reverse("external_api:assistant.message"), {"message": "hi", "history": []}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["reply"], "Hi there!")
        self.assertEqual(body["history"], [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hi there!"}])

    def test_history_is_capped_to_max_entries(self) -> None:
        long_history = [{"role": "user", "content": f"message {i}"} for i in range(MAX_HISTORY_ENTRIES + 10)]
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=_StubGateway(['{"reply": "ok"}'])):
            response = self.client.post(
                reverse("external_api:assistant.message"), {"message": "hi", "history": long_history}, content_type="application/json", **_bearer(self.raw_key)
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["history"]), MAX_HISTORY_ENTRIES)

    def test_message_is_required(self) -> None:
        response = self.client.post(reverse("external_api:assistant.message"), {"message": ""}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_message_over_the_length_cap_is_rejected(self) -> None:
        response = self.client.post(
            reverse("external_api:assistant.message"), {"message": "x" * (MAX_MESSAGE_CHARS + 1)}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_ai_unavailable_returns_503(self) -> None:
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=None):
            response = self.client.post(reverse("external_api:assistant.message"), {"message": "hi"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())

    def test_log_api_call_regression_exactly_one_row_with_cost(self) -> None:
        """The bug this phase fixed: a multi-round-trip turn must log exactly once, with cost."""
        gateway = _StubGateway(['{"tool": "list_trips", "args": {}}', '{"reply": "Here are your trips."}'])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            response = self.client.post(reverse("external_api:assistant.message"), {"message": "what are my trips?"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200, response.content)
        rows = list(ApiCallLog.objects.filter(service="assistant"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].success)
        self.assertIsNotNone(rows[0].cost_estimate)

    def test_log_api_call_records_failure_when_the_model_gives_up(self) -> None:
        """A dead gateway (send_prompt returns None) still logs one failed call, not zero."""
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=_StubGateway([None])):
            response = self.client.post(reverse("external_api:assistant.message"), {"message": "hi"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        rows = list(ApiCallLog.objects.filter(service="assistant"))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].success)


class AssistantResetTests(_AssistantApiTestCase):
    """POST /assistant/reset/ - a genuine no-op for this stateless shape."""

    def test_missing_scope_is_refused(self) -> None:
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        response = self.client.post(reverse("external_api:assistant.reset"), **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_returns_empty_history_with_no_side_effects(self) -> None:
        response = self.client.post(reverse("external_api:assistant.reset"), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"history": []})
        self.assertFalse(ApiCallLog.objects.filter(service="assistant").exists())
