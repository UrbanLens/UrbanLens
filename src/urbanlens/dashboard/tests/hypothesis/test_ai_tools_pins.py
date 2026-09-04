"""Tests for services.ai.tools.pins - search_pins and find_unvisited_pins.

Ported (unchanged in behavior) from the pre-registry ``_tool_search_pins``/
``_tool_find_unvisited_pins`` in services.ai.assistant, which
test_ai_assistant.py still covers directly for as long as that module's own
loop keeps calling them - see that file's own tests for the same scoping
assertions against the pre-migration code path. These exercise the same
tools through the new registry.execute() entry point instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, execute


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


class SearchPinsTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.location = baker.make(
            Location, latitude="42.5", longitude="-73.5", locality="Troy", administrative_area_level_1="NY"
        )
        self.pin = baker.make(
            Pin, profile=self.profile, location=self.location, name="Steel Mill", name_is_user_provided=True
        )
        self.foreign_pin = baker.make(
            Pin, profile=self.other, location=self.location, name="Steel Mill Twin", name_is_user_provided=True
        )

    def test_only_sees_own_pins(self) -> None:
        result = execute("search_pins", {"query": "steel"}, _context(self.profile))
        self.assertEqual(len(result.data["pins"]), 1)
        self.assertEqual(result.data["pins"][0]["slug"], self.pin.slug)

    def test_another_profiles_pin_never_leaks_through(self) -> None:
        # Even searching for the foreign pin's own name returns nothing for
        # the requesting profile - the query is scoped, not just filtered
        # client-side.
        result = execute("search_pins", {"query": "Twin"}, _context(self.profile))
        self.assertEqual(result.data["pins"], [])

    def test_blank_query_is_an_error(self) -> None:
        result = execute("search_pins", {"query": "   "}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_city_is_included(self) -> None:
        result = execute("search_pins", {"query": "steel"}, _context(self.profile))
        self.assertIn("Troy", result.data["pins"][0]["city"])

    def test_limit_is_respected(self) -> None:
        # Locations must be globally unique by (latitude, longitude), and a
        # profile can only have one pin per location - each pin needs its own,
        # distinct from self.location (42.5, -73.5) too.
        for i in range(3):
            location = baker.make(Location, latitude=f"{50.0 + i}", longitude="-73.5")
            baker.make(
                Pin, profile=self.profile, location=location, name=f"Steel Annex {i}", name_is_user_provided=True
            )
        result = execute("search_pins", {"query": "steel", "limit": 2}, _context(self.profile))
        self.assertEqual(len(result.data["pins"]), 2)

    def test_limit_above_the_row_cap_is_rejected(self) -> None:
        result = execute("search_pins", {"query": "steel", "limit": 999}, _context(self.profile))
        self.assertIn("error", result.data)


class FindUnvisitedPinsTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.location = baker.make(Location, latitude="42.5", longitude="-73.5", administrative_area_level_1="NY")
        self.pin = baker.make(
            Pin, profile=self.profile, location=self.location, name="Unvisited Works", name_is_user_provided=True
        )

    def test_excludes_visited(self) -> None:
        visited_location = baker.make(Location, latitude="43.0", longitude="-74.0", administrative_area_level_1="NY")
        visited_pin = baker.make(
            Pin, profile=self.profile, location=visited_location, name="Visited Works", name_is_user_provided=True
        )
        baker.make(PinVisit, pin=visited_pin)

        result = execute("find_unvisited_pins", {}, _context(self.profile))
        names = [row["name"] for row in result.data["pins"]]
        self.assertTrue(any("Unvisited Works" in n for n in names))
        self.assertFalse(any("Visited Works" in n for n in names))

    def test_only_sees_own_pins(self) -> None:
        baker.make(
            Pin,
            profile=self.other,
            location=self.location,
            name="Someone Else's Unvisited Pin",
            name_is_user_provided=True,
        )

        result = execute("find_unvisited_pins", {}, _context(self.profile))
        names = "".join(row["name"] for row in result.data["pins"])
        self.assertNotIn("Someone Else's Unvisited Pin", names)

    def test_state_filter(self) -> None:
        other_location = baker.make(Location, latitude="40.0", longitude="-75.0", administrative_area_level_1="PA")
        baker.make(Pin, profile=self.profile, location=other_location, name="PA Pin", name_is_user_provided=True)

        result = execute("find_unvisited_pins", {"state": "PA"}, _context(self.profile))
        self.assertEqual(len(result.data["pins"]), 1)
        self.assertIn("PA Pin", result.data["pins"][0]["name"])
        self.assertEqual(result.data["pins"][0]["state"], "PA")
