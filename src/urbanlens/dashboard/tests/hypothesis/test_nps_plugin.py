"""Tests for the NPS plugin's REData-backed panel and enrichment source.

``NpsPanelSource``/``NpsEnrichmentSource`` now call ``RedataNationalParksGateway``
instead of the direct NPS Developer API - these tests mock that gateway and
check the LocationCache row it produces, rather than any HTTP call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.nps import NpsEnrichmentSource, NpsPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway.RedataNationalParksGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.nps.redata_configured"


class NpsPanelSourceGateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = NpsPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_requires_redata_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate(self.pin))
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate(self.pin))


class NpsPanelSourceFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = NpsPanelSource()
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def _cached(self) -> dict | None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        row = LocationCache.objects.filter(location=self.location, source="nps").first()
        return row.data if row else None

    def test_caches_the_nearest_park(self) -> None:
        park = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_nearest_park.return_value = park
            self.source.fetch(self.pin)

        self.assertEqual(self._cached(), park)
        mock_gateway_cls.return_value.find_nearest_park.assert_called_once_with(44.6, -110.5)

    def test_caches_an_empty_dict_when_nothing_is_within_range(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_nearest_park.return_value = None
            self.source.fetch(self.pin)

        self.assertEqual(self._cached(), {})


class NpsEnrichmentSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = NpsEnrichmentSource()
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)

    def test_gate_requires_redata_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate())
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate())

    def test_fetch_returns_the_nearest_park_and_query_key(self) -> None:
        park = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_nearest_park.return_value = park
            data, query_key = self.source.fetch(self.location)

        self.assertEqual(data, park)
        self.assertEqual(query_key, "44.60000,-110.50000")

    def test_fetch_returns_none_when_nothing_is_within_range(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_nearest_park.return_value = None
            data, _query_key = self.source.fetch(self.location)

        self.assertIsNone(data)
