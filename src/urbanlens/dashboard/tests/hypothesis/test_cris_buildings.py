"""Tests for the CRIS Building USN Points plugin.

Retrieval calls REData's cultural-resources endpoints (see the module
docstring in plugins.builtin.cris_buildings) - RedataGateway itself is
mocked, so no real network access occurs. Covers NY-only geo-gating,
fetch()'s lookup -> fetch-detail -> flatten pipeline (and its graceful
degradation when REData is unconfigured/unavailable), render_context against
the flattened payload shape, and media_items() building proxy URLs for
attachments.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.cris_buildings import (
    CrisBuildingEnrichmentSource,
    CrisBuildingPanelSource,
    CrisBuildingsPlugin,
)
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway
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
        # Location.latitude/longitude are non-nullable at the DB level (pre-existing,
        # unrelated to this plugin) - gate() only reads effective_latitude/longitude
        # (Pin's own passthrough property), so a duck-typed stand-in exercises the
        # same branch without needing a real, impossible-to-persist Location.
        stub_pin = SimpleNamespace(effective_latitude=None, effective_longitude=None)
        with patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH):
            self.assertFalse(self.source.gate(stub_pin))


_BUILDING_RESOURCE = {
    "uuid": "res-1",
    "resource_type": "building",
    "attributes": {"USNNum": "12345", "USNName": "Old Mill", "HouseNum": "10", "StreetName": "Main St", "City": "Albany", "Zip": "12207", "EligibilityDesc": "Listed"},
}
_BUILDING_DETAIL = {
    **_BUILDING_RESOURCE,
    "attachments": [{"id": 1, "kind": "PHOTO", "name": "Front elevation"}, {"id": 2, "kind": "DOCUMENT", "attachment_type": "Building-Structure Inventory Form"}],
}


class PanelFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)

    def test_fetch_flattens_attributes_and_stores_attachments(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL) as mock_detail,
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": []}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)

        mock_detail.assert_called_once_with("res-1")
        data = mock_set.call_args[0][2]
        self.assertEqual(data["USNName"], "Old Mill")
        self.assertEqual(data["resource_uuid"], "res-1")
        self.assertEqual(len(data["attachments"]), 2)

    def test_fetch_extracts_images_from_document_attachments_only(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": [{"id": 9}]}) as mock_extract,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)

        mock_extract.assert_called_once_with("res-1", 2)  # only the DOCUMENT-kind attachment (id=2)
        data = mock_set.call_args[0][2]
        attachments_by_id = {a["id"]: a for a in data["attachments"]}
        self.assertEqual(attachments_by_id[2]["extracted_images"], [{"id": 9}])
        self.assertNotIn("extracted_images", attachments_by_id[1])

    def test_fetch_tolerates_extraction_failure_for_one_attachment(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", side_effect=PropertyRecordsUnavailableError("not_extractable", "boom")),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)

        data = mock_set.call_args[0][2]
        attachments_by_id = {a["id"]: a for a in data["attachments"]}
        self.assertEqual(attachments_by_id[2]["extracted_images"], [])
        self.assertEqual(len(data["attachments"]), 2)  # the PHOTO attachment survives too

    def test_no_building_resource_found_persists_empty(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[{"uuid": "r2", "resource_type": "archaeological_buffer_area"}]),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "cris_building_usn", {}, query_key="42.65,-73.75")

    def test_unavailable_gracefully_persists_empty(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", side_effect=PropertyRecordsUnavailableError("source_error", "boom")),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "cris_building_usn", {}, query_key="42.65,-73.75")

    def test_unconfigured_gateway_gracefully_persists_empty(self) -> None:
        """RedataGateway() raises ValueError (not PropertyRecordsUnavailableError) when unconfigured.

        The unconfigured state is simulated rather than left to the ambient
        environment: an install that *does* configure REData would otherwise
        reach the real API here instead of exercising this branch.
        ``__post_init__`` is what raises that ValueError, and it's the only
        patchable seam - RedataGateway is a slotted dataclass, so ``base_url``
        itself is read-only on the class.
        """
        with (
            patch.object(RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "cris_building_usn", {}, query_key="42.65,-73.75")

    def test_no_coordinates_persists_empty_without_calling_redata(self) -> None:
        # Location.latitude/longitude are non-nullable at the DB level, so this
        # (admittedly defensive-only, given the schema) branch is exercised
        # with a duck-typed stand-in rather than a real, impossible-to-persist Location.
        stub_location = SimpleNamespace(latitude=None, longitude=None)
        pin = MagicMock(location=stub_location)
        with (
            patch.object(RedataGateway, "lookup_cultural_resources") as mock_lookup,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(pin)
        mock_lookup.assert_not_called()
        mock_set.assert_called_once_with(stub_location, "cris_building_usn", {}, query_key="")


class MediaItemsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()

    def test_builds_one_item_per_attachment(self) -> None:
        data = {"resource_uuid": "res-1", "attachments": [{"id": 1, "kind": "PHOTO", "name": "Front elevation"}, {"id": 2, "kind": "DOCUMENT", "attachment_type": "Inventory Form"}]}
        items = self.source.media_items(data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].caption, "Front elevation")
        self.assertTrue(items[0].thumb_url)
        self.assertEqual(items[1].caption, "Inventory Form")
        self.assertEqual(items[1].thumb_url, "")  # documents get no thumbnail

    def test_no_resource_uuid_yields_no_items(self) -> None:
        self.assertEqual(self.source.media_items({"attachments": [{"id": 1, "kind": "PHOTO"}]}), [])

    def test_no_attachments_yields_no_items(self) -> None:
        self.assertEqual(self.source.media_items({"resource_uuid": "res-1"}), [])

    def test_extracted_images_yield_additional_items(self) -> None:
        data = {
            "resource_uuid": "res-1",
            "attachments": [
                {"id": 2, "kind": "DOCUMENT", "attachment_type": "Inventory Form", "extracted_images": [{"id": 9}, {"id": 10}]},
            ],
        }
        items = self.source.media_items(data)
        self.assertEqual(len(items), 3)  # the document attachment itself + 2 extracted images
        self.assertEqual(items[1].caption, "Inventory Form")
        self.assertTrue(items[1].thumb_url)
        self.assertEqual(items[2].caption, "Inventory Form")
        self.assertTrue(items[2].thumb_url)

    def test_attachment_with_no_extracted_images_yields_no_extra_items(self) -> None:
        data = {"resource_uuid": "res-1", "attachments": [{"id": 1, "kind": "PHOTO", "name": "Front", "extracted_images": []}]}
        self.assertEqual(len(self.source.media_items(data)), 1)


def _stub_pin(*, site_scope: bool = False):
    """A duck-typed pin for render_context, which now consults parcel-vs-building scope.

    ``is_site_scope`` short-circuits on the instance memo, so setting it
    directly decides the answer without needing a database (these are
    SimpleTestCases). The real scope rules are covered in test_site_scope.py.
    """
    return SimpleNamespace(_site_scope_cache=site_scope)


class RenderContextTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()
        self.pin = _stub_pin()

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


class SiteScopeRenderTests(SimpleTestCase):
    """A parcel-scope pin shows the district record, never a single building's.

    "TOOL SHED (1937), Building Number 154" is a true statement about one
    structure on a campus and a false one about the campus itself.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()
        self.building_data = {"USNName": "Tool Shed", "USNNum": "154", "EligibilityDesc": "Non-Contributing"}

    def test_a_building_scope_pin_still_sees_the_building(self) -> None:
        ctx = self.source.render_context(_stub_pin(site_scope=False), self.building_data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Tool Shed")

    def test_a_parcel_scope_pin_never_sees_the_building(self) -> None:
        self.assertIsNone(self.source.render_context(_stub_pin(site_scope=True), self.building_data))

    def test_a_parcel_scope_pin_sees_the_district_instead(self) -> None:
        data = {**self.building_data, "district": {"USNName": "Hudson River State Hospital Historic District", "EligibilityDesc": "Listed"}}
        ctx = self.source.render_context(_stub_pin(site_scope=True), data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Hudson River State Hospital Historic District")

    def test_media_items_are_unaffected_by_scope(self) -> None:
        """Attachment photos are additive and source-labelled - a campus keeps them."""
        data = {"resource_uuid": "res-1", "attachments": [{"id": 1, "kind": "PHOTO", "name": "Front"}]}
        self.assertEqual(len(self.source.media_items(data)), 1)


_DISTRICT_RESOURCE = {
    "uuid": "res-9",
    "resource_type": "district",
    "attributes": {"USNName": "Hudson River State Hospital Historic District", "EligibilityDesc": "Listed"},
}


class DistrictPayloadTests(TestCase):
    """fetch() caches any site-level resource alongside the building one."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.733150", longitude="-73.930370", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)

    def test_a_district_is_cached_beside_the_building(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE, _DISTRICT_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": []}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data["USNName"], "Old Mill", "the building record must stay at the top level")
        self.assertEqual(data["district"]["USNName"], "Hudson River State Hospital Historic District")

    def test_a_district_alone_is_still_cached(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_DISTRICT_RESOURCE]),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        self.assertEqual(mock_set.call_args[0][2]["district"]["USNName"], "Hudson River State Hospital Historic District")

    def test_no_district_leaves_the_payload_shape_unchanged(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": []}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        self.assertNotIn("district", mock_set.call_args[0][2])

    def test_an_archaeological_buffer_is_not_treated_as_a_district(self) -> None:
        """It marks a sensitivity zone, not a description of the property."""
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[{"uuid": "r2", "resource_type": "archaeological_buffer_area", "attributes": {"USNName": "Buffer"}}]),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "cris_building_usn", {}, query_key="41.73315,-73.93037")


class EnrichmentSourceTests(TestCase):
    def test_fetch_returns_flattened_payload_when_a_building_is_found(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
        ):
            payload, query_key = CrisBuildingEnrichmentSource().fetch(location)

        assert payload is not None
        self.assertEqual(payload["USNName"], "Old Mill")
        self.assertEqual(payload["resource_uuid"], "res-1")
        self.assertEqual(query_key, "42.650000,-73.750000")

    def test_fetch_returns_none_payload_when_unavailable(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)

        with patch.object(RedataGateway, "lookup_cultural_resources", side_effect=PropertyRecordsUnavailableError("source_error", "boom")):
            payload, query_key = CrisBuildingEnrichmentSource().fetch(location)

        self.assertIsNone(payload)
        self.assertEqual(query_key, "42.650000,-73.750000")

    def test_fetch_returns_none_payload_when_unconfigured(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)

        with patch.object(RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")):
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
