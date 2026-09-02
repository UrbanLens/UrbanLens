"""Tests for the AI assistant (UL-293): the native-tool-calling loop and the chat views.

Per-tool scoping (search_pins/find_unvisited_pins/list_trips/create_trip/
add_trip_activity, each checked against another profile's data) lives in
``test_ai_tools_pins.py``/``test_ai_tools_trips.py`` - those exercise the
same handlers this loop calls, through ``registry.execute()`` directly. What
belongs here is the loop itself: budgets, the unknown-tool path, and the
gateway-unavailable/no-response paths.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.ai.assistant import (
    _NO_RESPONSE_REPLY,
    _TIMEOUT_REPLY,
    MAX_ROUNDS,
    MAX_TOOL_CALLS,
    AssistantUnavailableError,
    run_assistant_turn,
)
from urbanlens.dashboard.services.core.celery import TaskProgress
from urbanlens_ai.schema import InferenceResponse, TextBlock, ToolUseBlock, Usage


def _plain_profile():
    """A profile with SiteFeature.AI granted - every registry tool gates on it."""
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")  # bootstrap site admin absorption
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return Profile.objects.get(user=baker.make("auth.User"))


class _StubGateway:
    """Feeds a scripted sequence of native-tool-calling responses to the loop.

    Each step is either ``{"reply": "..."}`` (a plain end-of-turn text
    response) or ``{"tool": name, "args": {...}}``/``{"tools": [...]}`` (one
    or several parallel ``ToolUseBlock``s in a single round - the latter is
    what lets a test drive the tool-call budget independently of the round
    budget). Carries ``model``/``cost`` because ``run_assistant_turn`` reads
    both, once, in its ``finally`` block to log the turn's cost - same shape
    a real ``LLMGateway`` always provides.
    """

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []
        self.model = "gpt-5-nano"
        self.cost = Decimal("0.01")

    def send_with_tools(self, prompt: str, tools: list) -> InferenceResponse | None:
        self.prompts.append(prompt)
        if not self.steps:
            return None
        step = self.steps.pop(0)
        if "reply" in step:
            return InferenceResponse(content=[TextBlock(text=step["reply"])], stop_reason="end_turn", usage=Usage(output_tokens=5))
        calls = step.get("tools") or [step]
        blocks: list[TextBlock | ToolUseBlock] = [ToolUseBlock(id=f"tu_{i}", name=call["tool"], input=call.get("args", {})) for i, call in enumerate(calls)]
        return InferenceResponse(content=blocks, stop_reason="tool_use", usage=Usage(output_tokens=5))


class AssistantLoopTests(TestCase):
    """The tool loop executes, records actions, and stays budgeted."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_unavailable_when_gateway_is_none(self) -> None:
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=None), pytest.raises(AssistantUnavailableError):
            run_assistant_turn(self.profile, [], "hello")

    def test_read_only_tool_then_reply(self) -> None:
        gateway = _StubGateway([{"tool": "list_trips", "args": {}}, {"reply": "You have no trips yet."}])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "what are my trips?")
        self.assertEqual(turn.reply, "You have no trips yet.")
        self.assertEqual(turn.actions, ["Checked your trips"])
        self.assertEqual(turn.proposals, [])
        # The second round's prompt must include the tool result for the model to use.
        self.assertIn("TOOL RESULT (list_trips)", gateway.prompts[1])

    def test_write_tool_produces_a_proposal_without_executing(self) -> None:
        """A write tool never runs inside the loop - it becomes a proposal for the user to confirm."""
        gateway = _StubGateway([{"tool": "create_trip", "args": {"name": "Loop Trip"}}, {"reply": "I've proposed creating that trip - just confirm it."}])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "make me a trip")
        self.assertEqual(turn.reply, "I've proposed creating that trip - just confirm it.")
        # Not executed - no action to log, no Trip row.
        self.assertEqual(turn.actions, [])
        self.assertFalse(Trip.objects.filter(name="Loop Trip").exists())
        self.assertEqual(len(turn.proposals), 1)
        proposal = turn.proposals[0]
        self.assertEqual(proposal["n"], 0)
        self.assertEqual(proposal["tool"], "create_trip")
        self.assertEqual(proposal["args"], {"name": "Loop Trip", "description": ""})
        self.assertEqual(proposal["confirm_label"], "Create trip")
        # The model must see that it was proposed, not silently ignored.
        self.assertIn("proposed", gateway.prompts[1])

    def test_unknown_tool_feeds_error_back(self) -> None:
        gateway = _StubGateway([{"tool": "drop_database", "args": {}}, {"reply": "ok"}])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "do something sneaky")
        self.assertEqual(turn.reply, "ok")
        self.assertEqual(turn.actions, [])
        self.assertIn("Unknown tool", gateway.prompts[1])

    def test_direct_reply_with_no_tool_calls(self) -> None:
        gateway = _StubGateway([{"reply": "Here's a quick answer, no tools needed."}])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "hi")
        self.assertEqual(turn.reply, "Here's a quick answer, no tools needed.")
        self.assertEqual(turn.actions, [])
        self.assertEqual(len(gateway.prompts), 1)

    def test_no_response_from_gateway_is_surfaced(self) -> None:
        gateway = _StubGateway([])  # exhausted immediately -> send_with_tools returns None
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "hi")
        self.assertEqual(turn.reply, _NO_RESPONSE_REPLY)

    def test_round_budget_stops_a_single_tool_per_round_runaway(self) -> None:
        gateway = _StubGateway([{"tool": "list_trips", "args": {}}] * 50)
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "loop forever")
        self.assertIn("step limit", turn.reply)
        # Exactly the round budget, not merely "some small number": a bug
        # that widened MAX_ROUNDS (or dropped the cap) must fail this. One
        # tool call per round means MAX_ROUNDS (4) governs here, well below
        # MAX_TOOL_CALLS (6) - see test_tool_call_budget_stops below for
        # that budget exercised independently.
        self.assertEqual(len(gateway.prompts), MAX_ROUNDS)
        self.assertEqual(turn.actions, ["Checked your trips"] * MAX_ROUNDS)

    def test_tool_call_budget_stops_a_multi_call_round_runaway(self) -> None:
        # Two tool calls per round: the tool-call budget (6) is hit after 3
        # rounds - well under the round budget (4) - proving MAX_TOOL_CALLS
        # is enforced on its own, not merely as a side effect of MAX_ROUNDS.
        two_calls = {"tools": [{"tool": "list_trips", "args": {}}, {"tool": "list_trips", "args": {}}]}
        gateway = _StubGateway([two_calls] * 10)
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "loop forever, two at a time")
        self.assertIn("action limit", turn.reply)
        self.assertEqual(turn.actions, ["Checked your trips"] * MAX_TOOL_CALLS)
        self.assertEqual(len(gateway.prompts), 3)

    def test_deadline_exceeded_mid_turn_stops_before_executing_the_call(self) -> None:
        gateway = _StubGateway([{"tool": "list_trips", "args": {}}, {"reply": "too late"}])
        times = iter([0.0, 0.0, 100.0, 100.0])
        with (
            patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway),
            patch("urbanlens.dashboard.services.ai.assistant.time.monotonic", side_effect=lambda: next(times)),
        ):
            turn = run_assistant_turn(self.profile, [], "hi")
        self.assertEqual(turn.reply, _TIMEOUT_REPLY)
        self.assertEqual(turn.actions, [])
        # Only the first round's prompt was ever sent - the call the deadline
        # caught never executed, so there was nothing to feed a second round.
        self.assertEqual(len(gateway.prompts), 1)

    def test_log_api_call_regression_exactly_one_row_with_cost(self) -> None:
        """A multi-round-trip turn must log exactly once, with cost - not once per round."""
        from urbanlens.dashboard.models.api_call_log.model import ApiCallLog

        gateway = _StubGateway([{"tool": "list_trips", "args": {}}, {"reply": "Here are your trips."}])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            run_assistant_turn(self.profile, [], "what are my trips?")
        rows = list(ApiCallLog.objects.filter(service="assistant"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].success)
        self.assertIsNotNone(rows[0].cost_estimate)

    def test_log_api_call_records_failure_when_the_model_gives_up(self) -> None:
        """A dead gateway (send_with_tools returns None) still logs one failed call, not zero."""
        from urbanlens.dashboard.models.api_call_log.model import ApiCallLog

        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=_StubGateway([])):
            run_assistant_turn(self.profile, [], "hi")
        rows = list(ApiCallLog.objects.filter(service="assistant"))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].success)


class AssistantViewTests(TestCase):
    """The chat page and message endpoint."""

    def setUp(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature

        baker.make("auth.User")
        self.user: User = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        # Confirming a real proposal (AssistantProposalConfirmViewTests) runs
        # a real tool through registry.execute(), which gates on this.
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)

    def _enqueued(self, task_id: str = "task-abc-123"):
        """A patched safely_enqueue_task returning a fake AsyncResult with this id."""
        return patch("urbanlens.dashboard.controllers.assistant.safely_enqueue_task", return_value=mock.Mock(id=task_id))

    def test_page_reflects_ai_availability(self) -> None:
        # Unavailable: the disabled notice, no chat form.
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=False):
            response = self.client.get(reverse("assistant"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI features are turned off")
        self.assertNotContains(response, 'name="message"')

        # Available: the chat form, no disabled notice. Same profile/client -
        # this is the same gate, just the other branch of it.
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True):
            response = self.client.get(reverse("assistant"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "AI features are turned off")
        self.assertContains(response, 'name="message"')

    def test_message_enqueues_a_pending_bubble_then_polling_resolves_it(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True), self._enqueued():
            response = self.client.post(reverse("assistant.message"), {"message": "find pins"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "find pins")
        # Not resolved yet - a pending bubble that self-polls, not the real reply.
        self.assertContains(response, "assistant-msg--pending")
        self.assertNotContains(response, "Found 3 pins.")

        turn_id = self.client.session["assistant_chat"][-1]["turn_id"]
        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "Found 3 pins.", "actions": ["Searched your pins"]})
        with patch("urbanlens.dashboard.controllers.assistant.get_task_progress", return_value=progress):
            response = self.client.get(reverse("assistant.turn", args=[turn_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Found 3 pins.")
        self.assertContains(response, "Searched your pins")
        self.assertNotContains(response, "assistant-msg--pending")

        # Really saved, not just rendered once: reloading shows the resolved
        # reply, no more pending bubble or poll trigger.
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True):
            response = self.client.get(reverse("assistant"))
        self.assertContains(response, "Found 3 pins.")
        self.assertNotContains(response, "assistant-msg--pending")

        # Reset clears the log.
        response = self.client.post(reverse("assistant.reset"))
        self.assertNotContains(response, "Found 3 pins.")

    def test_poll_view_renders_an_error_bubble_on_failure(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True), self._enqueued():
            self.client.post(reverse("assistant.message"), {"message": "find pins"})
        turn_id = self.client.session["assistant_chat"][-1]["turn_id"]

        progress = TaskProgress(task_id="task-abc-123", state="FAILURE", error="boom")
        with patch("urbanlens.dashboard.controllers.assistant.get_task_progress", return_value=progress):
            response = self.client.get(reverse("assistant.turn", args=[turn_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "expired")
        self.assertNotContains(response, "assistant-msg--pending")

    def test_poll_view_still_pending_self_polls_again(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True), self._enqueued():
            self.client.post(reverse("assistant.message"), {"message": "find pins"})
        turn_id = self.client.session["assistant_chat"][-1]["turn_id"]

        progress = TaskProgress(task_id="task-abc-123", state="PROGRESS", message="Thinking…")
        with patch("urbanlens.dashboard.controllers.assistant.get_task_progress", return_value=progress):
            response = self.client.get(reverse("assistant.turn", args=[turn_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "assistant-msg--pending")
        self.assertContains(response, reverse("assistant.turn", args=[turn_id]))

    def test_poll_view_404s_for_an_unknown_turn_id(self) -> None:
        response = self.client.get(reverse("assistant.turn", args=["never-issued"]))
        self.assertEqual(response.status_code, 404)

    def test_poll_view_404s_for_another_profiles_turn(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True), self._enqueued():
            self.client.post(reverse("assistant.message"), {"message": "find pins"})
        turn_id = self.client.session["assistant_chat"][-1]["turn_id"]

        other_user: User = baker.make(User)
        other_client = self.client_class()
        other_client.force_login(other_user)
        response = other_client.get(reverse("assistant.turn", args=[turn_id]))
        self.assertEqual(response.status_code, 404)

    def test_second_message_while_a_turn_is_in_flight_is_dropped_with_a_toast(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True), self._enqueued() as mock_enqueue:
            self.client.post(reverse("assistant.message"), {"message": "first"})
            response = self.client.post(reverse("assistant.message"), {"message": "second"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_enqueue.call_count, 1)
        self.assertNotContains(response, "second")
        self.assertIn("Still working on your last message", response["HX-Trigger"])

    def test_message_when_ai_unavailable_shows_notice_and_saves_history(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=False), self._enqueued() as mock_enqueue:
            response = self.client.post(reverse("assistant.message"), {"message": "find pins"})
        mock_enqueue.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "find pins")
        self.assertContains(response, "AI features are currently turned off")

        # It's really saved (not just rendered once): reloading the page still shows
        # it in the log (the log only renders when AI is currently available).
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True):
            response = self.client.get(reverse("assistant"))
        self.assertContains(response, "AI features are currently turned off")

    def test_stale_pending_entry_resolves_to_an_error_on_next_read(self) -> None:
        """A turn record can expire (server restart, 15-minute TTL) while a pending marker is still in session."""
        session = self.client.session
        session["assistant_chat"] = [{"role": "user", "content": "find pins"}, {"role": "assistant", "pending": True, "turn_id": "long-gone"}]
        session.save()

        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True):
            response = self.client.get(reverse("assistant"))
        self.assertContains(response, "expired")
        self.assertNotContains(response, "assistant-msg--pending")
        # Repaired in place, not just rendered once.
        self.assertFalse(self.client.session["assistant_chat"][-1].get("pending"))

    def test_blank_message_is_a_no_op(self) -> None:
        with self._enqueued() as mock_enqueue:
            response = self.client.post(reverse("assistant.message"), {"message": "   "})
        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_not_called()
        self.assertFalse(self.client.session.get("assistant_chat"))


class AssistantOverlayBodyViewTests(TestCase):
    """GET /assistant/overlay/ - the global overlay's lazily-loaded body."""

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("assistant.overlay"))
        self.assertEqual(response.status_code, 302)

    def test_renders_the_chat_when_available(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True):
            response = self.client.get(reverse("assistant.overlay"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="message"')
        self.assertNotContains(response, "AI features are turned off")

    def test_renders_the_disabled_notice_when_unavailable(self) -> None:
        """Re-checked here even though the FAB/dialog that link here are themselves gated - defense in depth."""
        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=False):
            response = self.client.get(reverse("assistant.overlay"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI features are turned off")
        self.assertNotContains(response, 'name="message"')


class GlobalAssistantSurfaceTests(TestCase):
    """The FAB/dialog every page renders in themes/base.html, gated by assistant_enabled_flag."""

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_fab_and_dialog_render_for_an_enabled_profile(self) -> None:
        with patch("urbanlens.dashboard.services.ai.access.assistant_available", return_value=True):
            response = self.client.get(reverse("map.view"))
        self.assertContains(response, 'id="ul-assistant-fab"')
        self.assertContains(response, 'id="assistant-overlay"')
        self.assertContains(response, reverse("assistant.overlay"))

    def test_fab_and_dialog_absent_for_a_disabled_profile(self) -> None:
        with patch("urbanlens.dashboard.services.ai.access.assistant_available", return_value=False):
            response = self.client.get(reverse("map.view"))
        self.assertNotContains(response, 'id="ul-assistant-fab"')
        self.assertNotContains(response, 'id="assistant-overlay"')

    def test_fab_and_dialog_absent_for_an_anonymous_visitor(self) -> None:
        self.client.logout()
        with patch("urbanlens.dashboard.services.ai.access.assistant_available", return_value=True):
            response = self.client.get(reverse("login"))
        self.assertNotContains(response, 'id="ul-assistant-fab"')


class AssistantProposalConfirmViewTests(TestCase):
    """POST /assistant/turn/<turn_id>/confirm/<n>/ - the write's only real execution path."""

    def setUp(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature

        baker.make("auth.User")
        self.user: User = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)

    def _resolve_with_proposal(self, proposal: dict) -> str:
        """Send a message, then resolve its poll to a turn carrying exactly this one proposal."""
        with (
            patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True),
            patch("urbanlens.dashboard.controllers.assistant.safely_enqueue_task", return_value=mock.Mock(id="task-abc-123")),
        ):
            self.client.post(reverse("assistant.message"), {"message": "make me a trip"})
        turn_id = self.client.session["assistant_chat"][-1]["turn_id"]

        progress = TaskProgress(task_id="task-abc-123", state="SUCCESS", result={"reply": "Confirm to create it.", "actions": [], "proposals": [proposal]})
        with patch("urbanlens.dashboard.controllers.assistant.get_task_progress", return_value=progress):
            self.client.get(reverse("assistant.turn", args=[turn_id]))
        return turn_id

    def test_confirm_runs_the_write_exactly_once(self) -> None:
        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "Confirmed Trip"}, "confirm_label": "Create trip"})
        self.assertFalse(Trip.objects.filter(name="Confirmed Trip").exists())

        response = self.client.post(reverse("assistant.proposal.confirm", args=[turn_id, 0]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Trip.objects.filter(name="Confirmed Trip").exists())

        # A second confirm (double click, retry) must not create a second trip.
        second = self.client.post(reverse("assistant.proposal.confirm", args=[turn_id, 0]))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Trip.objects.filter(name="Confirmed Trip").count(), 1)

    def test_confirm_racing_a_still_executing_claim_does_not_500(self) -> None:
        """A loser arriving between another request's claim and its session write-back gets a safe reply, not a crash.

        Simulated by claiming the proposal directly (as the winner would)
        without ever calling ``_update_session_proposal`` - the session's
        copy is left at ``status: "pending"``, exactly the window this view
        must not trust when rendering the loser's response.
        """
        from urbanlens.dashboard.services.ai.turns import claim_turn_proposal

        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "Racing Trip"}, "confirm_label": "Create trip"})
        self.assertTrue(claim_turn_proposal(turn_id, 0))

        response = self.client.post(reverse("assistant.proposal.confirm", args=[turn_id, 0]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Already confirmed.")
        self.assertFalse(Trip.objects.filter(name="Racing Trip").exists())

    def test_confirm_marks_the_session_proposal_resolved(self) -> None:
        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "Resolved Trip"}, "confirm_label": "Create trip"})
        self.client.post(reverse("assistant.proposal.confirm", args=[turn_id, 0]))

        with patch("urbanlens.dashboard.controllers.assistant.assistant_available", return_value=True):
            response = self.client.get(reverse("assistant"))
        # Resolved - no more live confirm button for this proposal.
        self.assertContains(response, "assistant-proposal--done")
        self.assertNotContains(response, reverse("assistant.proposal.confirm", args=[turn_id, 0]))
        entries = self.client.session["assistant_chat"]
        self.assertEqual(entries[-1]["proposals"][0]["status"], "done")

    def test_confirm_404s_for_an_unknown_or_out_of_range_proposal(self) -> None:
        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "X"}, "confirm_label": "Create trip"})
        self.assertEqual(self.client.post(reverse("assistant.proposal.confirm", args=[turn_id, 5])).status_code, 404)
        self.assertEqual(self.client.post(reverse("assistant.proposal.confirm", args=["never-issued", 0])).status_code, 404)

    def test_confirm_404s_for_another_profiles_proposal(self) -> None:
        turn_id = self._resolve_with_proposal({"n": 0, "tool": "create_trip", "args": {"name": "Not Yours"}, "confirm_label": "Create trip"})

        other_user: User = baker.make(User)
        other_client = self.client_class()
        other_client.force_login(other_user)
        response = other_client.post(reverse("assistant.proposal.confirm", args=[turn_id, 0]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Trip.objects.filter(name="Not Yours").exists())
