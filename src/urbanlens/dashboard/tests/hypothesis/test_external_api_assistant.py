"""Tests for the external API's AI assistant domain (async 202+poll shape, batch 2c).

A turn now runs on ai-worker: POST enqueues and returns 202 with a turn id,
GET polls it. Every test patches ``safely_enqueue_task``/``get_task_progress``
at ``external_api.views_assistant`` (the same seam ``test_ai_assistant.py``
patches for the web view) rather than a gateway - the gateway only matters
inside the task now, which is exercised directly in
``test_ai_assistant.py``/``test_ai_tasks.py``.
"""

from __future__ import annotations

from unittest import mock
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.ai.assistant import MAX_HISTORY_ENTRIES, MAX_MESSAGE_CHARS
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.core.celery import TaskProgress


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _enqueued(task_id: str = "task-abc-123"):
    return patch("urbanlens.dashboard.external_api.views_assistant.safely_enqueue_task", return_value=mock.Mock(id=task_id))


class _AssistantApiTestCase(TestCase):
    """Shared fixture: a key owner with an assistant:write-scoped key."""

    def setUp(self) -> None:
        from urbanlens.dashboard.models.site_settings.model import SiteSettings
        from urbanlens.dashboard.models.subscriptions import SiteFeature

        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.raw_key = self._key_with_scopes([ApiKeyScope.ASSISTANT_WRITE.value])
        # assistant_available() requires SiteFeature.AI - unlike the old
        # test suite, which mocked get_gateway directly and never actually
        # evaluated that gate.
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)

    def _key_with_scopes(self, scopes: list[str], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(user or self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _post_message(self, message: str, history: list | None = None, page_path: str | None = None):
        body = {"message": message, "history": history or []}
        if page_path is not None:
            body["page_path"] = page_path
        return self.client.post(reverse("external_api:assistant.message"), body, content_type="application/json", **_bearer(self.raw_key))

    def _poll(self, turn_id: str, attempt: int | None = None):
        url = reverse("external_api:assistant.turn", args=[turn_id])
        if attempt is not None:
            url += f"?attempt={attempt}"
        return self.client.get(url, **_bearer(self.raw_key))

    def _start_turn(self, message: str = "hi", history: list | None = None) -> str:
        with _enqueued():
            response = self._post_message(message, history)
        return response.json()["turn_id"]


class AssistantMessageTests(_AssistantApiTestCase):
    """POST /assistant/message/ - enqueues a turn; 202 with a turn id to poll."""

    def test_missing_scope_is_refused(self) -> None:
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        response = self.client.post(reverse("external_api:assistant.message"), {"message": "hi"}, content_type="application/json", **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_enqueues_and_returns_a_pollable_turn_id(self) -> None:
        with _enqueued():
            response = self._post_message("hi")
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertFalse(body["ready"])
        self.assertTrue(body["turn_id"])
        self.assertGreater(body["poll_after_seconds"], 0)

    def test_message_is_required(self) -> None:
        response = self.client.post(reverse("external_api:assistant.message"), {"message": ""}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_message_over_the_length_cap_is_rejected(self) -> None:
        response = self.client.post(
            reverse("external_api:assistant.message"), {"message": "x" * (MAX_MESSAGE_CHARS + 1)}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_ai_unavailable_returns_503(self) -> None:
        with patch("urbanlens.dashboard.external_api.views_assistant.assistant_available", return_value=False):
            response = self._post_message("hi")
        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())

    def test_a_turn_already_in_flight_is_refused_with_409(self) -> None:
        with _enqueued() as mock_enqueue:
            first = self._post_message("first")
            second = self._post_message("second")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(mock_enqueue.call_count, 1)

    def test_page_path_resolves_to_a_page_object_on_the_enqueued_task(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        with _enqueued() as mock_enqueue:
            self._post_message("hi", page_path=reverse("pin.details", args=[pin.slug]))
        self.assertEqual(mock_enqueue.call_args.kwargs["page"], {"kind": "pin", "id": pin.pk})

    def test_an_unresolvable_page_path_enqueues_no_page(self) -> None:
        with _enqueued() as mock_enqueue:
            self._post_message("hi", page_path="/not/a/real/route/")
        self.assertIsNone(mock_enqueue.call_args.kwargs["page"])

    def test_no_page_path_enqueues_no_page(self) -> None:
        with _enqueued() as mock_enqueue:
            self._post_message("hi")
        self.assertIsNone(mock_enqueue.call_args.kwargs["page"])

    def test_queue_failure_releases_the_lock_and_returns_503(self) -> None:
        with patch("urbanlens.dashboard.external_api.views_assistant.safely_enqueue_task", return_value=None):
            response = self._post_message("hi")
        self.assertEqual(response.status_code, 503)
        # The lock was released, not left stuck - a follow-up message can proceed.
        with _enqueued():
            retry = self._post_message("hi")
        self.assertEqual(retry.status_code, 202)


class AssistantTurnPollTests(_AssistantApiTestCase):
    """GET /assistant/turn/<turn_id>/ - poll one enqueued turn."""

    def test_still_pending_returns_202(self) -> None:
        turn_id = self._start_turn()
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=TaskProgress(task_id="task-abc-123", state="PROGRESS")):
            response = self._poll(turn_id)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["ready"])

    def test_success_returns_reply_and_history(self) -> None:
        turn_id = self._start_turn("hi", history=[])
        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "Hi there!", "actions": ["Searched your pins"]})
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            response = self._poll(turn_id)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["reply"], "Hi there!")
        self.assertEqual(body["actions"], ["Searched your pins"])
        self.assertEqual(body["history"], [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hi there!"}])

    def test_history_is_capped_to_max_entries(self) -> None:
        long_history = [{"role": "user", "content": f"message {i}"} for i in range(MAX_HISTORY_ENTRIES + 10)]
        turn_id = self._start_turn("hi", history=long_history)
        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "ok", "actions": []})
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            response = self._poll(turn_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["history"]), MAX_HISTORY_ENTRIES)

    def test_failure_returns_a_friendly_reply_not_an_error_status(self) -> None:
        turn_id = self._start_turn()
        progress = TaskProgress(task_id="task-abc-123", state="FAILURE", error="boom")
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            response = self._poll(turn_id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("expired", response.json()["reply"])

    def test_polling_a_resolved_turn_again_returns_the_same_reply(self) -> None:
        # A GET must be idempotent. The first poll to see SUCCESS clears the
        # Celery result (AsyncResult.forget), so a client that retries -
        # because it lost the response, or simply because its poll loop asks
        # again - would otherwise find the task id unknown (Celery reports
        # PENDING) and be told to keep waiting for a reply it can never get.
        turn_id = self._start_turn("hi", history=[])
        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "Hi there!", "actions": []})
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            first = self._poll(turn_id)
        self.assertEqual(first.status_code, 200, first.content)

        forgotten = TaskProgress(task_id="task-abc-123", state="PENDING")
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=forgotten) as backend:
            second = self._poll(turn_id)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json(), first.json())
        backend.assert_not_called()

    def test_polling_a_failed_turn_again_returns_the_same_error(self) -> None:
        turn_id = self._start_turn()
        progress = TaskProgress(task_id="task-abc-123", state="FAILURE", error="boom")
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            first = self._poll(turn_id)

        forgotten = TaskProgress(task_id="task-abc-123", state="PENDING")
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=forgotten) as backend:
            second = self._poll(turn_id)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        backend.assert_not_called()

    def test_exhausted_attempts_gives_up_gracefully(self) -> None:
        turn_id = self._start_turn()
        response = self._poll(turn_id, attempt=10_000)
        self.assertEqual(response.status_code, 200)
        self.assertIn("longer than expected", response.json()["reply"])

    def test_unknown_turn_id_is_404(self) -> None:
        response = self._poll("never-issued")
        self.assertEqual(response.status_code, 404)

    def test_another_profiles_turn_is_404(self) -> None:
        turn_id = self._start_turn()
        other_user = baker.make(User)
        other_key = self._key_with_scopes([ApiKeyScope.ASSISTANT_WRITE.value], user=other_user)
        response = self.client.get(reverse("external_api:assistant.turn", args=[turn_id]), **_bearer(other_key))
        self.assertEqual(response.status_code, 404)

    def test_success_includes_proposals_without_args(self) -> None:
        turn_id = self._start_turn()
        proposal = {"n": 0, "tool": "create_trip", "args": {"name": "X"}, "confirm_label": "Create trip"}
        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "ok", "actions": [], "proposals": [proposal]})
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            response = self._poll(turn_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["proposals"], [{"n": 0, "tool": "create_trip", "confirm_label": "Create trip"}])


class AssistantProposalConfirmTests(_AssistantApiTestCase):
    """POST /assistant/turn/<turn_id>/confirm/<n>/ - the write's only real execution path."""

    def _resolve_with_proposal(self, proposal: dict) -> str:
        turn_id = self._start_turn("make me a trip")
        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "Confirm to create it.", "actions": [], "proposals": [proposal]})
        with patch("urbanlens.dashboard.external_api.views_assistant.get_task_progress", return_value=progress):
            self._poll(turn_id)
        return turn_id

    def test_confirm_runs_the_write_exactly_once(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip

        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "Confirmed Trip"}, "confirm_label": "Create trip"})
        self.assertFalse(Trip.objects.filter(name="Confirmed Trip").exists())

        url = reverse("external_api:assistant.proposal.confirm", args=[turn_id, 0])
        response = self.client.post(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "done")
        self.assertTrue(Trip.objects.filter(name="Confirmed Trip").exists())

        # A second confirm (a client retry) must not create a second trip.
        second = self.client.post(url, **_bearer(self.raw_key))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Trip.objects.filter(name="Confirmed Trip").count(), 1)

    def test_confirm_404s_for_an_unknown_or_out_of_range_proposal(self) -> None:
        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "X"}, "confirm_label": "Create trip"})
        self.assertEqual(self.client.post(reverse("external_api:assistant.proposal.confirm", args=[turn_id, 5]), **_bearer(self.raw_key)).status_code, 404)
        self.assertEqual(self.client.post(reverse("external_api:assistant.proposal.confirm", args=["never-issued", 0]), **_bearer(self.raw_key)).status_code, 404)

    def test_confirm_404s_for_another_profiles_proposal(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip

        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "Not Yours"}, "confirm_label": "Create trip"})
        other_user = baker.make(User)
        other_key = self._key_with_scopes([ApiKeyScope.ASSISTANT_WRITE.value], user=other_user)

        response = self.client.post(reverse("external_api:assistant.proposal.confirm", args=[turn_id, 0]), **_bearer(other_key))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Trip.objects.filter(name="Not Yours").exists())


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
