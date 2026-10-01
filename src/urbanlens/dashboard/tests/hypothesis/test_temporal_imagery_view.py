"""Tests for TemporalImageryFeaturesView (GET .../temporal/<year>/)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.apis.locations.open_historical_map import OpenHistoricalMapGateway

_YEAR = 1950
_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "Old Rail Spur"},
            "geometry": {"type": "LineString", "coordinates": [[-74.0, 40.0], [-74.001, 40.001]]},
        },
    ],
}


def _grant_beta_features(user: User) -> None:
    role = baker.make(SubscriptionRole, features=SiteFeature.BETA_FEATURES)
    grant_subscription(user, role, user, None)


class TemporalImageryFeaturesPinScopedTests(TestCase):
    """``GET pin/<slug>/temporal/<year>/``"""

    def setUp(self) -> None:
        super().setUp()
        # Absorb the fresh-test-db bootstrap admin promotion (see
        # test_panel_feature_gate.py's setUp for why the first user isn't a
        # safe subject).
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)

    def _url(self, year: int = _YEAR) -> str:
        return reverse("pin.temporal_imagery", args=[self.pin.slug, year])

    def test_404_without_beta_feature(self) -> None:
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_404_for_someone_elses_pin(self) -> None:
        _grant_beta_features(self.user)
        other_pin = baker.make_recipe("dashboard.pin")
        response = self.client.get(reverse("pin.temporal_imagery", args=[other_pin.slug, _YEAR]))
        self.assertEqual(response.status_code, 404)

    def test_200_with_geojson_body(self) -> None:
        _grant_beta_features(self.user)
        with mock.patch.object(OpenHistoricalMapGateway, "get_features_at", return_value=_GEOJSON) as fetch:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"year": _YEAR, "geojson": _GEOJSON})
        fetch.assert_called_once()

    def test_second_request_for_same_year_does_not_recall_the_gateway(self) -> None:
        """The per-year cache must actually be hit on the second request."""
        _grant_beta_features(self.user)
        with mock.patch.object(OpenHistoricalMapGateway, "get_features_at", return_value=_GEOJSON) as fetch:
            first = self.client.get(self._url())
            second = self.client.get(self._url())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"year": _YEAR, "geojson": _GEOJSON})
        fetch.assert_called_once()

    def test_different_years_each_call_the_gateway(self) -> None:
        """Guards against a single fixed cache key silently sharing data across years."""
        _grant_beta_features(self.user)
        with mock.patch.object(OpenHistoricalMapGateway, "get_features_at", return_value=_GEOJSON) as fetch:
            self.client.get(self._url(1900))
            self.client.get(self._url(1950))
        self.assertEqual(fetch.call_count, 2)


class TemporalImageryFeaturesWikiScopedTests(TestCase):
    """``GET location/<slug>/wiki/temporal/<year>/``"""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.location)
        # A pin at this location is what earns wiki visibility - see
        # services.wiki.wiki_access.resolve_visible_wiki.
        baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)

    def _url(self, year: int = _YEAR) -> str:
        return reverse("location.wiki.temporal_imagery", args=[self.location.slug, year])

    def test_404_without_beta_feature(self) -> None:
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_404_for_a_location_the_viewer_has_no_pin_on(self) -> None:
        _grant_beta_features(self.user)
        other_location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=other_location)
        response = self.client.get(reverse("location.wiki.temporal_imagery", args=[other_location.slug, _YEAR]))
        self.assertEqual(response.status_code, 404)

    def test_200_with_geojson_body(self) -> None:
        _grant_beta_features(self.user)
        with mock.patch.object(OpenHistoricalMapGateway, "get_features_at", return_value=_GEOJSON) as fetch:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"year": _YEAR, "geojson": _GEOJSON})
        fetch.assert_called_once()

    def test_second_request_for_same_year_does_not_recall_the_gateway(self) -> None:
        _grant_beta_features(self.user)
        with mock.patch.object(OpenHistoricalMapGateway, "get_features_at", return_value=_GEOJSON) as fetch:
            first = self.client.get(self._url())
            second = self.client.get(self._url())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        fetch.assert_called_once()
