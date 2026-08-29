"""Tests for the AI assistant (UL-293): tool scoping, the loop, and the chat views."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.ai.assistant import (
    MAX_TOOL_CALLS,
    AssistantUnavailableError,
    _parse_step,
    _tool_add_trip_activity,
    _tool_create_trip,
    _tool_find_unvisited_pins,
    _tool_list_trips,
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
        baker.make(PinVisit, pin=visited_pin)
        result = _tool_find_unvisited_pins(self.profile, {})
        names = [row["name"] for row in result["pins"]]
        self.assertIn("Steel Mill", names)
        self.assertNotIn("Visited Works", names)

    def test_list_trips_only_sees_own(self) -> None:
        mine = _tool_create_trip(self.profile, {"name": "My Trip"})
        my_trip = Trip.objects.get(slug=mine["created"]["slug"])
        my_trip.start_date = date.today() + timedelta(days=3)
        my_trip.save(update_fields=["start_date"])

        theirs = _tool_create_trip(self.other, {"name": "Not Mine"})
        their_trip = Trip.objects.get(slug=theirs["created"]["slug"])
        their_trip.start_date = date.today() + timedelta(days=3)
        their_trip.save(update_fields=["start_date"])

        result = _tool_list_trips(self.profile, {})
        self.assertEqual([row["name"] for row in result["trips"]], ["My Trip"])
        row = result["trips"][0]
        self.assertEqual(row["slug"], my_trip.slug)
        self.assertEqual(row["start_date"], my_trip.start_date.isoformat())
        self.assertEqual(row["activities"], 0)

    def test_create_trip_and_membership(self) -> None:
        result = _tool_create_trip(self.profile, {"name": "Assistant Run"})
        trip = Trip.objects.get(slug=result["created"]["slug"])
        self.assertEqual(trip.creator_id, self.profile.id)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED).exists())

    def test_create_trip_blank_name_generates_one(self) -> None:
        result = _tool_create_trip(self.profile, {})
        self.assertTrue(result["created"]["name"].strip())

    def test_create_trip_respects_upcoming_trip_limit(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_upcoming_trips_per_user = 1
        settings.save()

        # Under the cap: succeeds. Move it into the "upcoming" bucket the quota
        # counts (an undated trip with no activities doesn't count as upcoming).
        first = _tool_create_trip(self.profile, {"name": "First Trip"})
        first_trip = Trip.objects.get(slug=first["created"]["slug"])
        first_trip.start_date = date.today() + timedelta(days=3)
        first_trip.save(update_fields=["start_date"])

        # At the cap: rejected, and nothing is created.
        blocked = _tool_create_trip(self.profile, {"name": "Second Trip"})
        self.assertIn("error", blocked)
        self.assertFalse(Trip.objects.filter(name="Second Trip").exists())

        # Raise the cap (real state transition): the same request now succeeds.
        settings.max_upcoming_trips_per_user = 2
        settings.save()
        allowed = _tool_create_trip(self.profile, {"name": "Second Trip"})
        self.assertNotIn("error", allowed)
        self.assertTrue(Trip.objects.filter(name="Second Trip", creator=self.profile).exists())

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

    def test_add_trip_activity_respects_activity_limit(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_activities = 1
        settings.save()

        trip_result = _tool_create_trip(self.profile, {"name": "Capped Trip"})
        trip_slug = trip_result["created"]["slug"]
        second_location = baker.make(Location, latitude="42.700000", longitude="-73.700000", administrative_area_level_1="NY")
        second_pin = baker.make(Pin, profile=self.profile, location=second_location, name="Second Pin", name_is_user_provided=True)

        # Under the cap: succeeds.
        first = _tool_add_trip_activity(self.profile, {"trip_slug": trip_slug, "pin_slug": self.pin.slug})
        self.assertNotIn("error", first)

        # At the cap: rejected, and no second activity is created.
        blocked = _tool_add_trip_activity(self.profile, {"trip_slug": trip_slug, "pin_slug": second_pin.slug})
        self.assertIn("error", blocked)
        self.assertEqual(TripActivity.objects.filter(trip__slug=trip_slug).count(), 1)

        # Raise the cap (real state transition): the same request now succeeds.
        settings.max_trip_activities = 2
        settings.save()
        allowed = _tool_add_trip_activity(self.profile, {"trip_slug": trip_slug, "pin_slug": second_pin.slug})
        self.assertNotIn("error", allowed)
        self.assertEqual(TripActivity.objects.filter(trip__slug=trip_slug).count(), 2)


class _StubGateway:
    """Feeds a scripted sequence of answers to the loop.

    Carries ``model``/``cost`` because ``run_assistant_turn`` reads both,
    once, in its ``finally`` block to log the turn's cost - same shape a real
    ``LLMGateway`` always provides.
    """

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.model = "gpt-5-nano"
        self.cost = Decimal("0.01")

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
        # Exactly the budget, not merely "some small number": a bug that widened
        # MAX_TOOL_CALLS (or dropped the cap) must fail this.
        self.assertEqual(len(gateway.prompts), MAX_TOOL_CALLS)
        self.assertEqual(turn.actions, ["Checked your trips"] * MAX_TOOL_CALLS)

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
