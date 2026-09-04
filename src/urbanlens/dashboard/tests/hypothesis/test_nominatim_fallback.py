"""The direct-Nominatim geocode fallbacks go through NominatimGateway, not raw geopy.

Two call sites fell back to constructing geopy's ``Nominatim`` client
directly - ``geocode_resolution.geocode_address`` with
``user_agent="geoapiExercises"`` (the copy-pasted tutorial string Nominatim's
operators block outright) and ``controllers/settings.geocode_address`` with
its own ad-hoc agent. Both bypassed the project's rate limiter, call logging
and timeout injection while ``NominatimGateway`` (rate-limited, logged,
properly identified, 1-call/minute budget enforced app-wide) sat in the same
package. See the 2026-08-15 STATUS entry in
``docs/reports/2026-08-11-codebase-audit.md``.

The error contract improves as a side effect: geopy's ``GeocoderTimedOut``/
``GeocoderUnavailable`` used to escape to callers that only handle
``(None, None)``; the gateway returns ``[]`` on failure, which resolves to
the callers' own clean "couldn't convert address" paths.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations import geocode_resolution

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway"


class GeocodeResolutionFallbackTests(SimpleTestCase):
    def _fallback(self, results: list[dict]) -> tuple:
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.geocode_resolution.redata_configured", return_value=False
            ),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.search.return_value = results
            coords = geocode_resolution.geocode_address("323 Beaver St, Ansonia CT")
        return coords, gateway_cls

    def test_the_fallback_goes_through_the_rate_limited_gateway(self) -> None:
        (latitude, longitude), gateway_cls = self._fallback([{"lat": "41.35014", "lon": "-73.06822"}])
        self.assertEqual((latitude, longitude), (41.35014, -73.06822))
        gateway_cls.return_value.search.assert_called_once_with("323 Beaver St, Ansonia CT", limit=1)

    def test_no_results_resolves_to_none_none(self) -> None:
        coords, _ = self._fallback([])
        self.assertEqual(coords, (None, None))

    def test_a_result_missing_coordinates_resolves_to_none_none(self) -> None:
        coords, _ = self._fallback([{"lat": None, "lon": None}])
        self.assertEqual(coords, (None, None))

    def test_neither_fallback_constructs_geopy_nominatim(self) -> None:
        """The banned tutorial user agent (and raw geopy geocoding) must not return.

        A source-text assertion rather than a behavioural one, deliberately:
        the failure mode being guarded is someone re-adding the "simple"
        direct client in either module, which no mock-based test would see.
        """
        import urbanlens.dashboard.controllers.settings as settings_controller

        for module in (geocode_resolution, settings_controller):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn(
                "geoapiExercises", source, f"{module.__name__} reintroduced the blocked tutorial user agent"
            )
            self.assertNotIn(
                "from geopy.geocoders", source, f"{module.__name__} bypasses NominatimGateway with a raw geopy client"
            )


class SettingsGeocodeFallbackTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(baker.make(User))

    def test_the_nominatim_fallback_goes_through_the_gateway(self) -> None:
        """With Google yielding nothing, the view's fallback answers via NominatimGateway."""
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway"
            ) as google_cls,
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            google_cls.return_value.geocode_place_name.return_value = None
            gateway_cls.return_value.search.return_value = [{"lat": "41.35014", "lon": "-73.06822"}]
            response = self.client.get(reverse("settings.geocode"), {"address": "323 Beaver St, Ansonia CT"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"lat": 41.35014, "lng": -73.06822})

    def test_coordinate_fast_path_needs_no_gateway(self) -> None:
        with mock.patch(_GATEWAY_PATH) as gateway_cls:
            response = self.client.get(reverse("settings.geocode"), {"address": "41.35, -73.06"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"lat": 41.35, "lng": -73.06})
        gateway_cls.return_value.search.assert_not_called()
