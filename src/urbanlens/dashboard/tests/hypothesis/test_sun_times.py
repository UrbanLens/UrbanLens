"""Tests for UL-345: sunrise/sunset and golden-hour times on the pin weather panel.

get_sun_times() always goes through Open-Meteo (timezone=auto resolves local
time server-side, so no separate timezone lookup is needed here) regardless
of which provider serves the temperature/condition forecast, since
OpenWeatherMap's 5-day/3-hour endpoint doesn't carry sunrise/sunset.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway


def _mock_response(daily: dict) -> Mock:
    resp = Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"daily": daily}
    return resp


class GetSunTimesTests(SimpleTestCase):
    def setUp(self) -> None:
        self.gateway = OpenMeteoGateway()

    def test_parses_sunrise_and_sunset(self) -> None:
        daily = {"sunrise": ["2026-06-15T05:32"], "sunset": ["2026-06-15T20:47"]}
        with patch.object(self.gateway.session, "get", return_value=_mock_response(daily)):
            sun_times = self.gateway.get_sun_times(40.0, -74.0)

        assert sun_times is not None
        self.assertEqual(sun_times["sunrise"], datetime.fromisoformat("2026-06-15T05:32"))
        self.assertEqual(sun_times["sunset"], datetime.fromisoformat("2026-06-15T20:47"))

    def test_golden_hour_is_one_hour_from_sunrise_and_sunset(self) -> None:
        daily = {"sunrise": ["2026-06-15T05:32"], "sunset": ["2026-06-15T20:47"]}
        with patch.object(self.gateway.session, "get", return_value=_mock_response(daily)):
            sun_times = self.gateway.get_sun_times(40.0, -74.0)

        assert sun_times is not None
        self.assertEqual(sun_times["golden_hour_morning_end"], sun_times["sunrise"] + timedelta(hours=1))
        self.assertEqual(sun_times["golden_hour_evening_start"], sun_times["sunset"] - timedelta(hours=1))

    def test_returns_none_on_empty_daily_data(self) -> None:
        with patch.object(self.gateway.session, "get", return_value=_mock_response({})):
            self.assertIsNone(self.gateway.get_sun_times(40.0, -74.0))

    def test_returns_none_on_request_failure(self) -> None:
        with patch.object(self.gateway.session, "get", side_effect=OSError("boom")):
            self.assertIsNone(self.gateway.get_sun_times(40.0, -74.0))


class WeatherPanelSunTimesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_weather_panel_shows_sun_times(self) -> None:
        sun_times = {
            "sunrise": datetime.fromisoformat("2026-06-15T05:32"),
            "sunset": datetime.fromisoformat("2026-06-15T20:47"),
            "golden_hour_morning_end": datetime.fromisoformat("2026-06-15T06:32"),
            "golden_hour_evening_start": datetime.fromisoformat("2026-06-15T19:47"),
        }
        with (
            patch("urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast", return_value=[]),
            patch("urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_sun_times", return_value=sun_times),
        ):
            response = self.client.get(reverse("pin.weather_forecast", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sun_times"], sun_times)
        self.assertContains(response, "5:32 AM")
        self.assertContains(response, "8:47 PM")

    def test_weather_panel_omits_sun_times_section_when_unavailable(self) -> None:
        with (
            patch("urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_weather_forecast", return_value=[]),
            patch("urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_sun_times", return_value=None),
        ):
            response = self.client.get(reverse("pin.weather_forecast", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "wx-sun-times")

    def test_sun_times_fetched_even_when_openweathermap_serves_the_forecast(self) -> None:
        """OpenWeatherMap's 5-day/3-hour forecast has no sunrise/sunset field,
        so sun times must still come from Open-Meteo even on that path."""
        sun_times = {
            "sunrise": datetime.fromisoformat("2026-06-15T05:32"),
            "sunset": datetime.fromisoformat("2026-06-15T20:47"),
            "golden_hour_morning_end": datetime.fromisoformat("2026-06-15T06:32"),
            "golden_hour_evening_start": datetime.fromisoformat("2026-06-15T19:47"),
        }
        with (
            patch("urbanlens.UrbanLens.settings.app.settings.openweathermap_api_key", "test-key"),
            patch("urbanlens.dashboard.services.apis.weather.gateway.OpenWeatherMapGateway.get_weather_forecast", return_value=[{"date": sun_times["sunrise"], "main": {"temp": 70}, "weather": [{"main": "Clear"}]}]),
            patch("urbanlens.dashboard.services.apis.weather.open_meteo.OpenMeteoGateway.get_sun_times", return_value=sun_times) as get_sun_times,
        ):
            response = self.client.get(reverse("pin.weather_forecast", args=[self.pin.slug]))

        get_sun_times.assert_called_once()
        self.assertEqual(response.context["sun_times"], sun_times)
