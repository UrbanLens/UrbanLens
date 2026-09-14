"""A private pin's coordinates never reach a log line (P112, `py/clear-text-logging-sensitive-data`)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.handlers.pin_mutation import PinMutationUndoHandler
from urbanlens.dashboard.services.undo.service import UndoExpiredError


class UndoMoveRejectionLogTests(TestCase):
    def test_a_rejected_replay_logs_the_pin_but_not_where_it_was_going(self) -> None:
        profile = baker.make(User).profile
        moving = baker.make(Pin, profile=profile, location=Location.objects.create(latitude=10.0, longitude=20.0))
        baker.make(Pin, profile=profile, location=Location.objects.create(latitude=51.123457, longitude=-73.654321))

        with (
            self.assertLogs("urbanlens.dashboard.services.undo.handlers.pin_mutation", "INFO") as logs,
            self.assertRaises(UndoExpiredError),
        ):
            PinMutationUndoHandler.undo_mutation(
                {"op": "move", "pin_id": moving.pk, "before_lat": 51.123457, "before_lng": -73.654321}
            )

        output = "\n".join(logs.output)
        self.assertIn(str(moving.pk), output)
        self.assertNotIn("51.12", output)
        self.assertNotIn("73.65", output)


class WikipediaPanelLogTests(TestCase):
    def test_an_empty_article_cache_logs_the_pin_but_not_its_coordinates(self) -> None:
        user = baker.make(User)
        location = Location.objects.create(latitude=51.123457, longitude=-73.654321)
        pin = baker.make(Pin, profile=user.profile, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data={})
        self.client.force_login(user)

        with self.assertLogs("urbanlens.dashboard.controllers.pin", "DEBUG") as logs:
            response = self.client.get(reverse("pin.wikipedia", args=[pin.slug]))

        self.assertEqual(response.status_code, 204)
        output = "\n".join(logs.output)
        self.assertIn(pin.slug, output)
        self.assertNotIn("51.12", output)
        self.assertNotIn("73.65", output)


class NearbyPlacesLogTests(TestCase):
    def test_a_places_search_logs_the_result_count_but_not_the_viewport(self) -> None:
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        user = baker.make(User)
        user.profile.places_google_enabled = True
        user.profile.places_nps_enabled = False
        user.profile.places_wikipedia_enabled = False
        user.profile.save()
        self.client.force_login(user)

        with (
            mock.patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=True),
            mock.patch.object(app_settings, "google_unrestricted_api_key", "test-key"),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.places_resolution.search_nearby_landmarks",
                return_value=[],
            ),
            self.assertLogs("urbanlens.dashboard.controllers.maps", "INFO") as logs,
        ):
            response = self.client.get(
                reverse("map.places.nearby"), {"lat": "51.123457", "lng": "-73.654321", "zoom": "15"}
            )

        self.assertEqual(response.status_code, 200)
        output = "\n".join(logs.output)
        self.assertIn("found 0 results", output)
        self.assertNotIn("51.12", output)
        self.assertNotIn("73.65", output)


class TripWeatherLogTests(TestCase):
    def test_a_failed_forecast_does_not_log_the_activitys_coordinates(self) -> None:
        from urbanlens.dashboard.controllers.trip import _build_activity_forecasts

        act = mock.MagicMock()
        act.lat_override = 51.123457
        act.lng_override = -73.654321
        act.pin = None
        act.title = None
        act.scheduled_at = timezone.now()
        act.status = "proposed"

        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.weather_resolution.get_raw_forecast_slots",
                side_effect=requests.ConnectionError("down"),
            ),
            self.assertLogs("urbanlens.dashboard.controllers.trip", "WARNING") as logs,
        ):
            _build_activity_forecasts([act])

        output = "\n".join(logs.output)
        self.assertNotIn("51.12", output)
        self.assertNotIn("73.65", output)


class TakeoutImportLogTests(TestCase):
    """A Takeout Maps URL embeds the place's coordinates, and a row carries the user's own name and notes for it."""

    _URL = "https://www.google.com/maps/place/Secret+Mill/@51.123457,-73.654321,17z"

    def _warnings(self, csv_text: str, **extract) -> str:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        profile = baker.make(User).profile
        with (
            mock.patch.object(GoogleGeocodingGateway, "extract_coordinates_from_url", **extract),
            self.assertLogs("urbanlens.dashboard.services.apis.locations.google.maps", "WARNING") as logs,
        ):
            rows = list(GoogleMapsGateway(api_key="test-key")._csv_row_iter(csv_text, profile, offline=True))
        self.assertEqual(rows, [None])
        return "\n".join(logs.output)

    def test_an_unparseable_or_unresolved_url_is_not_logged(self) -> None:
        for extract in ({"side_effect": ValueError("unparseable")}, {"return_value": (None, None)}):
            with self.subTest(extract=extract):
                output = self._warnings(f'Title,Note,URL\nSecret Mill,,"{self._URL}"\n', **extract)

                self.assertNotIn("51.12", output)
                self.assertNotIn("Secret", output)

    def test_a_row_without_coordinates_is_not_logged(self) -> None:
        output = self._warnings("Title,Note\nSecret Mill,behind the river gate\n", return_value=(None, None))

        self.assertNotIn("Secret", output)
        self.assertNotIn("river gate", output)
