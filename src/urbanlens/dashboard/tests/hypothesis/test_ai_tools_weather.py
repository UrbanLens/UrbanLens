"""Tests for services.ai.tools.weather - get_weather, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, available_tools, execute
from urbanlens.UrbanLens.settings.app import settings

_OWM = "urbanlens.dashboard.services.apis.weather.gateway.OpenWeatherMapGateway.get_weather_forecast"
_OPEN_METEO = "urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast"
#: ForecastSlot.date is documented as naive UTC (see services.apis.weather.forecast) - a real slot never carries tzinfo.
_SLOT_DATE = datetime(2026, 6, 15, 9, 0)  # noqa: DTZ001


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


def _pin(profile, lat: str = "42.5", lng: str = "-73.5", name: str = "Weather Pin") -> Pin:
    # Locations are globally unique by (latitude, longitude), and Pin.slug is
    # unique per-profile, not globally - an unnamed pin's baker-generated
    # default name could collide across two different profiles' otherwise-
    # unrelated pins, so each call needs its own distinct name too.
    location = baker.make(Location, latitude=lat, longitude=lng)
    return baker.make(Pin, profile=profile, location=location, name=name, name_is_user_provided=True)


class GetWeatherTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.pin = _pin(self.profile)

    def test_appears_in_available_tools(self) -> None:
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("get_weather", names)

    def test_hidden_when_external_apis_are_disabled(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertNotIn("get_weather", names)

    def test_falls_back_to_open_meteo_when_owm_is_not_configured(self) -> None:
        slot = {
            "date": _SLOT_DATE,
            "temp": 70.0,
            "condition": "Clear",
            "icon": "wb_sunny",
            "humidity": None,
            "wind_speed": None,
        }
        with (
            mock.patch.object(settings, "openweathermap_api_key", None),
            mock.patch(_OPEN_METEO, return_value=[slot]) as open_meteo,
        ):
            result = execute("get_weather", {"pin_slug": self.pin.slug}, _context(self.profile))
        open_meteo.assert_called_once()
        self.assertEqual(result.data["source"], "open_meteo")
        self.assertEqual(result.data["condition"], "Clear")
        self.assertEqual(result.data["temp_f"], 70)

    def test_prefers_openweathermap_when_configured(self) -> None:
        raw_item = {"date": _SLOT_DATE, "main": {"temp": 55.4}, "weather": [{"main": "Rain"}]}
        with (
            mock.patch.object(settings, "openweathermap_api_key", "test-key"),
            mock.patch(_OWM, return_value=[raw_item]),
            mock.patch(_OPEN_METEO) as open_meteo,
        ):
            result = execute("get_weather", {"pin_slug": self.pin.slug}, _context(self.profile))
        open_meteo.assert_not_called()
        self.assertEqual(result.data["source"], "openweathermap")
        self.assertEqual(result.data["condition"], "Rain")
        self.assertEqual(result.data["temp_f"], 55)

    def test_owm_failure_falls_back_to_open_meteo(self) -> None:
        slot = {
            "date": _SLOT_DATE,
            "temp": 70.0,
            "condition": "Clear",
            "icon": "wb_sunny",
            "humidity": None,
            "wind_speed": None,
        }
        with (
            mock.patch.object(settings, "openweathermap_api_key", "test-key"),
            mock.patch(_OWM, side_effect=ConnectionError("boom")),
            mock.patch(_OPEN_METEO, return_value=[slot]) as open_meteo,
        ):
            result = execute("get_weather", {"pin_slug": self.pin.slug}, _context(self.profile))
        open_meteo.assert_called_once()
        self.assertEqual(result.data["source"], "open_meteo")

    def test_every_provider_failing_is_unavailable_not_an_error(self) -> None:
        with mock.patch.object(settings, "openweathermap_api_key", None), mock.patch(_OPEN_METEO, return_value=None):
            result = execute("get_weather", {"pin_slug": self.pin.slug}, _context(self.profile))
        self.assertNotIn("error", result.data)
        self.assertEqual(result.data["source"], "unavailable")

    def test_explicit_coordinates(self) -> None:
        with mock.patch.object(settings, "openweathermap_api_key", None), mock.patch(_OPEN_METEO, return_value=None):
            result = execute("get_weather", {"lat": 42.5, "lng": -73.5}, _context(self.profile))
        self.assertNotIn("error", result.data)

    def test_no_endpoint_is_an_error_block_not_a_raise(self) -> None:
        result = execute("get_weather", {}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_another_profiles_pin_slug_never_resolves(self) -> None:
        theirs = _pin(self.other, "44.0", "-75.0", "Theirs")
        result = execute("get_weather", {"pin_slug": theirs.slug}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_never_calls_a_redata_gateway(self) -> None:
        """The bypass rationale in the module docstring, verified: no REData weather gateway is touched."""
        with (
            mock.patch.object(settings, "openweathermap_api_key", None),
            mock.patch(_OPEN_METEO, return_value=None),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                side_effect=AssertionError("must not be called"),
            ),
        ):
            result = execute("get_weather", {"pin_slug": self.pin.slug}, _context(self.profile))
        self.assertNotIn("error", result.data)
