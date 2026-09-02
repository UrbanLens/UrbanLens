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

    def test_tool_then_reply(self) -> None:
        gateway = _StubGateway([{"tool": "create_trip", "args": {"name": "Loop Trip"}}, {"reply": "Created your trip!"}])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "make me a trip")
        self.assertEqual(turn.reply, "Created your trip!")
        self.assertEqual(turn.actions, ["Created a trip"])
        self.assertTrue(Trip.objects.filter(name="Loop Trip").exists())
        # The second round's prompt must include the tool result for the model to use.
        self.assertIn("TOOL RESULT (create_trip)", gateway.prompts[1])

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


class AssistantViewTests(TestCase):
    """The chat page and message endpoint."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user: User = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def test_page_reflects_ai_availability(self) -> None:
        # Without a gateway: the disabled notice, no chat form.
        with patch("urbanlens.dashboard.controllers.assistant.get_gateway", return_value=None):
            response = self.client.get(reverse("assistant"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI features are turned off")
        self.assertNotContains(response, 'name="message"')

        # With a gateway: the chat form, no disabled notice. Same profile/client -
        # this is the same gate, just the other branch of it.
        with patch("urbanlens.dashboard.controllers.assistant.get_gateway", return_value=object()):
            response = self.client.get(reverse("assistant"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "AI features are turned off")
        self.assertContains(response, 'name="message"')

    def test_message_round_trip_persists_in_session(self) -> None:
        from urbanlens.dashboard.services.ai.assistant import AssistantTurn

        with patch("urbanlens.dashboard.controllers.assistant.run_assistant_turn", return_value=AssistantTurn(reply="Found 3 pins.", actions=["Searched your pins"])):
            response = self.client.post(reverse("assistant.message"), {"message": "find pins"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "find pins")
        self.assertContains(response, "Found 3 pins.")
        self.assertContains(response, "Searched your pins")

        # Reset clears the log.
        response = self.client.post(reverse("assistant.reset"))
        self.assertNotContains(response, "Found 3 pins.")

    def test_message_when_ai_unavailable_shows_notice_and_saves_history(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.run_assistant_turn", side_effect=AssistantUnavailableError("off")):
            response = self.client.post(reverse("assistant.message"), {"message": "find pins"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "find pins")
        self.assertContains(response, "AI features are currently turned off")

        # It's really saved (not just rendered once): reloading the page still shows
        # it in the log (the log only renders when AI is currently available).
        with patch("urbanlens.dashboard.controllers.assistant.get_gateway", return_value=object()):
            response = self.client.get(reverse("assistant"))
        self.assertContains(response, "AI features are currently turned off")

    def test_blank_message_is_a_no_op(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.run_assistant_turn") as mock_turn:
            response = self.client.post(reverse("assistant.message"), {"message": "   "})
        self.assertEqual(response.status_code, 200)
        mock_turn.assert_not_called()
        self.assertFalse(self.client.session.get("assistant_chat"))
