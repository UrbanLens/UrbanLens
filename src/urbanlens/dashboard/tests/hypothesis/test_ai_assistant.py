"""Tests for the AI assistant (UL-293): tool scoping, the loop, and the chat views."""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.ai.assistant import (
    AssistantUnavailableError,
    _parse_step,
    _tool_add_trip_activity,
    _tool_create_trip,
    _tool_find_unvisited_pins,
    _tool_search_pins,
    run_assistant_turn,
)


class ParseStepTests(TestCase):
    """The tolerant JSON step parser."""

    def test_parses_clean_and_wrapped_json(self) -> None:
        self.assertEqual(_parse_step('{"reply": "hi"}'), {"reply": "hi"})
        self.assertEqual(_parse_step('Sure! {"tool": "list_trips", "args": {}} done'), {"tool": "list_trips", "args": {}})

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(_parse_step("I could not decide"))
        self.assertIsNone(_parse_step("{broken"))


class AssistantToolTests(TestCase):
    """Every tool is scoped to the requesting profile."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.other = Profile.objects.get(user=baker.make("auth.User"))
        self.location = baker.make(Location, latitude="42.500000", longitude="-73.500000", locality="Troy", administrative_area_level_1="NY")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Steel Mill", name_is_user_provided=True)
        self.foreign_pin = baker.make(Pin, profile=self.other, location=self.location, name="Steel Mill Twin", name_is_user_provided=True)

    def test_search_pins_only_sees_own(self) -> None:
        result = _tool_search_pins(self.profile, {"query": "steel"})
        names = [row["name"] for row in result["pins"]]
        self.assertEqual(names, ["Steel Mill"])
        self.assertEqual(result["pins"][0]["city"], "Troy")

    def test_search_requires_query(self) -> None:
        self.assertIn("error", _tool_search_pins(self.profile, {}))

    def test_find_unvisited_excludes_visited(self) -> None:
        second_location = baker.make(Location, latitude="42.600000", longitude="-73.600000", administrative_area_level_1="NY")
        visited_pin = baker.make(Pin, profile=self.profile, location=second_location, name="Visited Works", name_is_user_provided=True)
        baker.make(PinVisit, pin=visited_pin, profile=self.profile)
        result = _tool_find_unvisited_pins(self.profile, {})
        names = [row["name"] for row in result["pins"]]
        self.assertIn("Steel Mill", names)
        self.assertNotIn("Visited Works", names)

    def test_create_trip_and_membership(self) -> None:
        result = _tool_create_trip(self.profile, {"name": "Assistant Run"})
        trip = Trip.objects.get(slug=result["created"]["slug"])
        self.assertEqual(trip.creator_id, self.profile.id)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED).exists())

    def test_create_trip_blank_name_generates_one(self) -> None:
        result = _tool_create_trip(self.profile, {})
        self.assertTrue(result["created"]["name"].strip())

    def test_add_trip_activity_scoping(self) -> None:
        trip_result = _tool_create_trip(self.profile, {"name": "Scoped Trip"})
        trip_slug = trip_result["created"]["slug"]

        # Someone else's trip: rejected.
        foreign_trip = baker.make(Trip, name="Not Yours", creator=self.other)
        self.assertIn("error", _tool_add_trip_activity(self.profile, {"trip_slug": foreign_trip.slug, "pin_slug": self.pin.slug}))
        # Someone else's pin: rejected.
        self.assertIn("error", _tool_add_trip_activity(self.profile, {"trip_slug": trip_slug, "pin_slug": self.foreign_pin.slug}))

        result = _tool_add_trip_activity(self.profile, {"trip_slug": trip_slug, "pin_slug": self.pin.slug, "scheduled_date": "2026-08-01"})
        activity = TripActivity.objects.get(pk=result["added"]["activity_id"])
        self.assertEqual(activity.status, TripActivity.STATUS_PROPOSED)
        self.assertEqual(activity.pin_id, self.pin.id)
        self.assertIsNotNone(activity.scheduled_at)


class _StubGateway:
    """Feeds a scripted sequence of answers to the loop."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class AssistantLoopTests(TestCase):
    """The tool loop executes, records actions, and stays budgeted."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def test_unavailable_when_gateway_is_none(self) -> None:
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=None), pytest.raises(AssistantUnavailableError):
            run_assistant_turn(self.profile, [], "hello")

    def test_tool_then_reply(self) -> None:
        gateway = _StubGateway(['{"tool": "create_trip", "args": {"name": "Loop Trip"}}', '{"reply": "Created your trip!"}'])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "make me a trip")
        self.assertEqual(turn.reply, "Created your trip!")
        self.assertEqual(turn.actions, ["Created a trip"])
        self.assertTrue(Trip.objects.filter(name="Loop Trip").exists())
        # The second prompt must include the tool result for the model to use.
        self.assertIn("TOOL RESULT (create_trip)", gateway.prompts[1])

    def test_unknown_tool_feeds_error_back(self) -> None:
        gateway = _StubGateway(['{"tool": "drop_database", "args": {}}', '{"reply": "ok"}'])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "do something sneaky")
        self.assertEqual(turn.reply, "ok")
        self.assertEqual(turn.actions, [])
        self.assertIn("unknown tool", gateway.prompts[1])

    def test_loop_budget_stops_runaway(self) -> None:
        gateway = _StubGateway(['{"tool": "list_trips", "args": {}}'] * 50)
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "loop forever")
        self.assertIn("action limit", turn.reply)
        self.assertLessEqual(len(gateway.prompts), 8)

    def test_unparseable_answer_is_surfaced_as_text(self) -> None:
        gateway = _StubGateway(["Here are some thoughts without JSON."])
        with patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway):
            turn = run_assistant_turn(self.profile, [], "hi")
        self.assertEqual(turn.reply, "Here are some thoughts without JSON.")


class AssistantViewTests(TestCase):
    """The chat page and message endpoint."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def test_page_renders_disabled_state_without_ai(self) -> None:
        with patch("urbanlens.dashboard.controllers.assistant.get_gateway", return_value=None):
            response = self.client.get(reverse("assistant"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI features are turned off")

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
