"""Tests for the CRIS Building USN Points plugin scaffold.

The plugin's actual data retrieval is deferred to REData (see the module
docstring in plugins.builtin.cris_buildings) - these tests cover the
infrastructure that's already wired: NY-only geo-gating, the empty-result
stub fetch, and render_context against the intended final payload shape.
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.cris_buildings import (
    CrisBuildingEnrichmentSource,
    CrisBuildingPanelSource,
    CrisBuildingsPlugin,
)
from urbanlens.dashboard.services.geo_boundary import GeoBoundary

# A stand-in boundary covering roughly upstate NY, so tests don't hit TIGERweb.
_NY_ISH = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -73.0)])


def _make_profile():
    from urbanlens.dashboard.models.profile.model import Profile

    user = baker.make("auth.User")
    return Profile.objects.get(user=user)


class PanelGateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()

    def test_gate_true_for_pin_inside_boundary(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        pin = baker.make(Pin, profile=_make_profile(), location=location)
        with patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH):
            self.assertTrue(self.source.gate(pin))

    def test_gate_false_for_pin_outside_boundary(self) -> None:
        location = baker.make(Location, latitude="48.850000", longitude="2.350000", google_place=None)
        pin = baker.make(Pin, profile=_make_profile(), location=location)
        with patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH):
            self.assertFalse(self.source.gate(pin))

    def test_gate_false_without_coordinates(self) -> None:
        location = baker.make(Location, latitude=None, longitude=None, google_place=None)
        pin = baker.make(Pin, profile=_make_profile(), location=location)
        with patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH):
            self.assertFalse(self.source.gate(pin))


class PanelFetchTests(TestCase):
    def test_fetch_persists_an_empty_marker(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        pin = baker.make(Pin, profile=_make_profile(), location=location)

        with patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set:
            CrisBuildingPanelSource().fetch(pin)

        mock_set.assert_called_once_with(location, "cris_building_usn", {}, query_key="")


class RenderContextTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()
        self.pin = None  # render_context doesn't use pin for this source.

    def test_empty_data_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {}))

    def test_missing_usn_name_yields_none(self) -> None:
        data = {"USNNum": "12345", "EligibilityDesc": "Listed"}
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_full_payload_renders_expected_meta(self) -> None:
        data = {
            "USNNum": "12345",
            "USNName": "Old Mill",
            "HouseNum": "10",
            "StreetName": "Main St",
            "City": "Albany",
            "Zip": "12207",
            "EligibilityDesc": "Listed",
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Old Mill")
        labels = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels["Address"], "10 Main St")
        self.assertEqual(labels["City"], "Albany")
        self.assertEqual(labels["ZIP Code"], "12207")
        self.assertEqual(labels["NYSHPO USN Number"], "12345")
        self.assertEqual(labels["Eligibility Status"], "Listed")


class EnrichmentSourceTests(TestCase):
    def test_fetch_returns_none_payload_and_a_query_key(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)

        payload, query_key = CrisBuildingEnrichmentSource().fetch(location)

        self.assertIsNone(payload)
        self.assertEqual(query_key, "42.650000,-73.750000")


class PluginContributionsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plugin = CrisBuildingsPlugin()

    def test_contributes_one_panel_source(self) -> None:
        sources = self.plugin.get_panel_sources()
        self.assertEqual([type(source) for source in sources], [CrisBuildingPanelSource])

    def test_contributes_one_enrichment_source(self) -> None:
        sources = self.plugin.get_enrichment_sources()
        self.assertEqual([type(source) for source in sources], [CrisBuildingEnrichmentSource])

    def test_contributes_a_name_provider_reading_usn_name(self) -> None:
        providers = self.plugin.get_name_providers()
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].source, "cris")
        self.assertEqual(providers[0].cache_source, "cris_building_usn")
        self.assertEqual(providers[0].keys, ("USNName",))
