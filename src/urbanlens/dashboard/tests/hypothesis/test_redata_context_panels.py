"""Tests for the six REData location-context panels added 2026-08-15.

Underground structures, permits & violations, reported incidents, hydrology,
site conditions (land cover + walkability + soil), and air quality. Each
panel's render_context encodes a contract point from REData's endpoint docs
(traffic exclusion, capped-result flagging, enterable-first ordering,
null-distance preservation, no cross-source averaging) - these tests pin
those behaviours, not the cosmetics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.redata_air_quality import AirQualityPanelSource
from urbanlens.dashboard.plugins.builtin.redata_hydrology import HydrologyPanelSource
from urbanlens.dashboard.plugins.builtin.redata_incidents import PoliceIncidentsPanelSource
from urbanlens.dashboard.plugins.builtin.redata_permits import BuildingPermitsPanelSource
from urbanlens.dashboard.plugins.builtin.redata_site_conditions import SiteConditionsPanelSource
from urbanlens.dashboard.plugins.builtin.redata_underground import UndergroundPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, LocationContextUnavailableError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


def _us_pin() -> Pin:
    location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
    return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)


class UndergroundPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = UndergroundPanelSource()
        self.pin = _us_pin()

    def test_empty_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"structures": []}))

    def test_enterable_structures_render_first(self) -> None:
        data = {
            "structures": [
                {"kind": "utility_line", "name": "", "is_enterable": False},
                {"kind": "rail_tunnel", "name": "Old freight tunnel", "is_enterable": True, "layer": -1},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["label"], "Rail tunnel")
        self.assertIn("Old freight tunnel", ctx["meta"][0]["value"])
        self.assertIn("2 mapped within 250 m", ctx["chips"])
        self.assertIn("1 enterable", ctx["chips"])

    def test_disused_provenance_survives_to_the_row(self) -> None:
        data = {"structures": [{"kind": "station_level", "name": "Lower platform", "is_enterable": True, "attributes": {"abandoned:railway": "station"}}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("disused/abandoned", ctx["meta"][0]["value"])

    def test_fetch_caches_results_without_geometry(self) -> None:
        """The panel renders no geometry, so a LineString per tunnel segment must not bloat the cache row."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=1,
            complete=True,
            results=[{"kind": "chamber", "is_enterable": True, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}],
        )
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_underground_gateway.RedataUndergroundGateway") as gateway_cls:
            gateway_cls.return_value.get_underground_structures.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "redata_underground")
        assert cached is not None
        self.assertEqual(cached.data["structures"][0]["kind"], "chamber")
        self.assertNotIn("geometry", cached.data["structures"][0])


class BuildingPermitsPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = BuildingPermitsPanelSource()
        self.pin = _us_pin()

    def test_capped_results_are_flagged_as_a_floor(self) -> None:
        """A capped count is a floor, not a total - the chip must say so."""
        data = {
            "filings": [
                {"kind": "permit", "issued_at": "2025-06-01", "work_type": "Renovation", "status": "issued", "attributes": {"result_capped": True}},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("more than shown", ctx["chips"])

    def test_deep_link_and_declared_cost_render(self) -> None:
        data = {"filings": [{"kind": "permit", "issued_at": "2025-06-01", "work_type": "Demolition", "status": "final", "estimated_cost": 250000, "url": "https://city.example/p/1"}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["href"], "https://city.example/p/1")
        self.assertIn("declared $250,000", ctx["meta"][0]["value"])

    def test_gate_requires_us_coordinates(self) -> None:
        abroad = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=baker.make("dashboard.Location", latitude=48.86, longitude=2.35))
        # Patched at its definition site: the REData half of these gates now
        # lives on RedataInfoPanelSource, which imports it inside gate().
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=True):
            self.assertFalse(self.source.gate(abroad))
            self.assertTrue(self.source.gate(self.pin))


class PoliceIncidentsPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = PoliceIncidentsPanelSource()
        self.pin = _us_pin()

    def test_traffic_collisions_are_excluded(self) -> None:
        """Only some feeds publish traffic rows; keeping them would compare publishing scope, not safety."""
        data = {
            "incidents": [
                {"category": "traffic", "occurred_at": "2026-01-05"},
                {"category": "burglary", "occurred_at": "2026-01-02", "offense_description": "forced entry"},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("1 on this block in 3 years", ctx["chips"][0])
        self.assertTrue(any("Burglary" in entry["value"] for entry in ctx["meta"]))

    def test_only_traffic_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"incidents": [{"category": "traffic"}]}))

    def test_the_precision_caveat_is_always_present(self) -> None:
        data = {"incidents": [{"category": "theft", "occurred_at": "2026-01-02"}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][-1]["label"], "Precision")


class HydrologyPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = HydrologyPanelSource()
        self.pin = _us_pin()

    def test_watershed_becomes_the_heading_not_a_row(self) -> None:
        data = {
            "features": [
                {"kind": "watershed", "name": "Green Brook-Bound Brook", "distance_meters": None},
                {"kind": "stream", "name": "", "distance_meters": 120.0},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Green Brook-Bound Brook watershed")
        self.assertTrue(all("watershed" not in entry["label"].lower() for entry in ctx["meta"]))

    def test_unnamed_features_and_null_distances_are_kept(self) -> None:
        """Blank names and null distances are the sources' own answers, not gaps to drop."""
        data = {
            "features": [
                {"kind": "wetland", "name": "", "distance_meters": None, "attributes": {"water_regime": "Seasonally Flooded"}},
                {"kind": "stream", "name": "", "distance_meters": 45.0},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(len(ctx["meta"]), 2)
        self.assertIn("Unnamed stream", ctx["meta"][0]["value"])
        self.assertIn("seasonally flooded", ctx["meta"][1]["value"])

    def test_waterbody_pluralizes_as_waterbodies(self) -> None:
        data = {
            "features": [
                {"kind": "waterbody", "name": "Pond A", "distance_meters": 10.0},
                {"kind": "waterbody", "name": "Pond B", "distance_meters": 20.0},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("2 waterbodies within 1 km", ctx["chips"])


class SiteConditionsPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = SiteConditionsPanelSource()
        self.pin = _us_pin()

    def test_one_failing_domain_does_not_blank_the_others(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        land_cover = LocationContextEnvelope(count=1, complete=True, results=[{"class_name": "Developed, High Intensity", "class_code": 24}])
        walkability = LocationContextEnvelope(count=1, complete=True, results=[{"index": 17.0, "band": "Most walkable", "transit_distance_meters": None}])
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.redata_land_cover_gateway.RedataLandCoverGateway") as land_cls,
            mock.patch("urbanlens.dashboard.services.apis.locations.redata_walkability_gateway.RedataWalkabilityGateway") as walk_cls,
            mock.patch("urbanlens.dashboard.services.apis.locations.redata_soil_gateway.RedataSoilGateway") as soil_cls,
        ):
            land_cls.return_value.get_land_cover.return_value = land_cover
            walk_cls.return_value.get_walkability.return_value = walkability
            soil_cls.return_value.get_soil_components.side_effect = LocationContextUnavailableError("source_error", "boom")
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "redata_site_conditions")
        assert cached is not None
        self.assertIn("land_cover", cached.data)
        self.assertIn("walkability", cached.data)
        self.assertNotIn("soil", cached.data)

    def test_null_transit_distance_is_a_fact_not_a_gap(self) -> None:
        data = {"walkability": [{"index": 4.0, "band": "Least walkable", "transit_distance_meters": None}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertTrue(any(fact["text"] == "No transit stop nearby" for fact in ctx["facts"]))

    def test_soil_composition_renders_dominant_first_with_no_average(self) -> None:
        data = {
            "soil": [
                {"component_name": "Clarion", "component_percent": 85.0, "drainage_class": "Well drained", "hydrologic_group": "B", "map_unit_name": "Clarion loam"},
                {"component_name": "Nicollet", "component_percent": 5.0, "drainage_class": "Somewhat poorly drained", "hydrologic_group": "B/D", "map_unit_name": "Clarion loam"},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = [entry["label"] for entry in ctx["meta"]]
        self.assertIn("Soil map unit", labels[0])
        self.assertIn("Clarion (85%)", labels[1])
        self.assertIn("Nicollet (5%)", labels[2])

    def test_unrated_urban_component_is_labelled_unrated(self) -> None:
        """A blank drainage class is the survey's silence - shown as Unrated, never invented."""
        data = {"soil": [{"component_name": "Urban land", "component_percent": 90.0, "drainage_class": "", "hydrologic_group": "", "map_unit_name": ""}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["value"], "Unrated")


class AirQualityPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = AirQualityPanelSource()
        self.pin = _us_pin()

    def test_modelled_row_supplies_facts_and_sensors_are_only_counted(self) -> None:
        """Sensor readings must never be averaged into the modelled answer."""
        data = {
            "readings": [
                {"source_kind": "modelled", "us_aqi": 42.0, "pm2_5": 9.1},
                {"source_kind": "sensor", "pm2_5": 83.2},
                {"source_kind": "sensor", "pm2_5": 2.4},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertTrue(any("US AQI 42" in fact["text"] for fact in ctx["facts"]))
        self.assertIn("2 community sensors within 5 km", ctx["chips"])
        self.assertFalse(any("83" in fact["text"] or "2.4" in fact["text"] for fact in ctx["facts"]))

    def test_no_readings_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"readings": []}))

    def test_a_modelled_row_with_no_numbers_hides_the_panel(self) -> None:
        """A lone "modelled (CAMS)" chip with no facts would be an empty shell of a panel."""
        self.assertIsNone(self.source.render_context(self.pin, {"readings": [{"source_kind": "modelled"}]}))
