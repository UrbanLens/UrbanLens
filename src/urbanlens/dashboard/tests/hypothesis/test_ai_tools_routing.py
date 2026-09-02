"""Tests for services.ai.tools.routing - distance_and_drive_time, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import DistanceUnit
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, available_tools, execute
from urbanlens.dashboard.services.geo.distance import haversine_km

_OSRM = "urbanlens.dashboard.services.apis.routing.osrm.OSRMGateway.get_route_between"


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


def _pin(profile, lat: str, lng: str, name: str) -> Pin:
    # Pin.slug is unique per-profile, not globally - an unnamed pin's
    # baker-generated default name could collide across two different
    # profiles' otherwise-unrelated pins, so a "belongs to someone else"
    # test needs an explicit, distinct name to actually exercise that.
    location = baker.make(Location, latitude=lat, longitude=lng)
    return baker.make(Pin, profile=profile, location=location, name=name, name_is_user_provided=True)


class DistanceAndDriveTimeTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.origin = _pin(self.profile, "42.5", "-73.5", "Origin")
        self.destination = _pin(self.profile, "43.0", "-74.0", "Destination")

    def test_appears_in_available_tools(self) -> None:
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("distance_and_drive_time", names)

    def test_hidden_when_external_apis_are_disabled(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertNotIn("distance_and_drive_time", names)

    def test_two_of_the_profiles_own_pins(self) -> None:
        with mock.patch(_OSRM, return_value={"distance_meters": 10_000, "duration_seconds": 900}):
            result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug, "to_pin_slug": self.destination.slug}, _context(self.profile))
        self.assertNotIn("error", result.data)
        expected_km = haversine_km(42.5, -73.5, 43.0, -74.0)
        self.assertAlmostEqual(result.data["distance_km"], round(expected_km, 1))
        self.assertEqual(result.data["source"], "osrm")
        self.assertEqual(result.data["drive_time_minutes"], 15)

    def test_explicit_coordinates_for_both_endpoints(self) -> None:
        with mock.patch(_OSRM, return_value=None):
            result = execute("distance_and_drive_time", {"from_lat": 42.5, "from_lng": -73.5, "to_lat": 43.0, "to_lng": -74.0}, _context(self.profile))
        self.assertNotIn("error", result.data)
        self.assertGreater(result.data["distance_km"], 0)

    def test_a_mixed_pin_and_coordinate_endpoint(self) -> None:
        with mock.patch(_OSRM, return_value=None):
            result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug, "to_lat": 43.0, "to_lng": -74.0}, _context(self.profile))
        self.assertNotIn("error", result.data)

    def test_osrm_failure_still_returns_haversine_distance_with_unavailable_source(self) -> None:
        with mock.patch(_OSRM, return_value=None):
            result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug, "to_pin_slug": self.destination.slug}, _context(self.profile))
        self.assertNotIn("error", result.data)
        self.assertEqual(result.data["source"], "unavailable")
        self.assertNotIn("drive_time_minutes", result.data)
        self.assertGreater(result.data["distance_km"], 0)

    def test_missing_endpoint_is_an_error_block_not_a_raise(self) -> None:
        result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_another_profiles_pin_slug_never_resolves(self) -> None:
        theirs = _pin(self.other, "44.0", "-75.0", "Theirs")
        result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug, "to_pin_slug": theirs.slug}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_both_units_are_returned_with_the_profiles_preference_first(self) -> None:
        self.profile.distance_units = DistanceUnit.MILES
        self.profile.save(update_fields=["distance_units"])
        with mock.patch(_OSRM, return_value=None):
            result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug, "to_pin_slug": self.destination.slug}, _context(self.profile))
        self.assertEqual(result.data["preferred_unit"], "mi")
        self.assertIn("distance_km", result.data)
        self.assertIn("distance_mi", result.data)

    def test_never_calls_a_redata_gateway(self) -> None:
        """The bypass rationale in the module docstring, verified: no REData routing gateway is touched."""
        with (
            mock.patch(_OSRM, return_value=None),
            mock.patch("urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", side_effect=AssertionError("must not be called")),
        ):
            result = execute("distance_and_drive_time", {"from_pin_slug": self.origin.slug, "to_pin_slug": self.destination.slug}, _context(self.profile))
        self.assertNotIn("error", result.data)
