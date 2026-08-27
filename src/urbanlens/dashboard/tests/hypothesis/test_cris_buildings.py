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
    cris_only,
    nearest_resource,
    site_resource,
    site_resource_attributes,
)
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary

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


# Field values here mirror REData's own serializers exactly - `resource_type`
# from CulturalResourceType and `kind` from CulturalResourceAttachmentKind,
# both lowercase TextChoices values. Earlier fixtures used invented uppercase
# kinds and a "district" resource type that REData never emits, which is what
# let the plugin's mismatched comparisons pass tests while matching nothing live.
_BUILDING_RESOURCE = {
    "uuid": "res-1",
    "resource_type": "building",
    "source_latitude": 42.650000,
    "source_longitude": -73.750000,
    "attributes": {"USNNum": "12345", "USNName": "Old Mill", "HouseNum": "10", "StreetName": "Main St", "City": "Albany", "Zip": "12207", "EligibilityDesc": "Listed"},
}
_BUILDING_DETAIL = {
    **_BUILDING_RESOURCE,
    "attachments": [
        {"id": 1, "kind": "photo", "name": "Front elevation", "content_type": "image/jpeg"},
        {"id": 2, "kind": "document", "attachment_type": "Building-Structure Inventory Form", "content_type": "application/pdf"},
    ],
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
        self.assertTrue(data["attachments_fetched"])

    def test_fetch_extracts_images_from_document_attachments_only(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": [{"id": 9}]}) as mock_extract,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)

        mock_extract.assert_called_once_with("res-1", 2)  # only the document-kind attachment (id=2)
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
        self.assertEqual(len(data["attachments"]), 2)  # the photo attachment survives too

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


class NearestResourceTests(SimpleTestCase):
    """A CRIS lookup over a campus returns dozens of buildings in no useful order.

    Taking the first match handed every pin on a site the same arbitrary
    outbuilding; each resource's own published position is what ranks them.
    """

    def _building(self, uuid: str, lat: float | None, lng: float | None) -> dict:
        return {"uuid": uuid, "resource_type": "building", "source_latitude": lat, "source_longitude": lng}

    def test_picks_the_closest_building_not_the_first(self) -> None:
        resources = [
            self._building("far", 41.740000, -73.930000),
            self._building("near", 41.733200, -73.930400),
            self._building("middling", 41.736000, -73.930000),
        ]
        chosen = nearest_resource(resources, "building", 41.733150, -73.930370)
        assert chosen is not None
        self.assertEqual(chosen["uuid"], "near")

    def test_ignores_resources_of_another_type(self) -> None:
        resources = [
            {"uuid": "district", "resource_type": "building_district", "source_latitude": 41.733150, "source_longitude": -73.930370},
            self._building("far", 41.740000, -73.930000),
        ]
        chosen = nearest_resource(resources, "building", 41.733150, -73.930370)
        assert chosen is not None
        self.assertEqual(chosen["uuid"], "far")

    def test_falls_back_to_the_first_match_when_none_publishes_a_position(self) -> None:
        """REData leaves source_* null for USN stubs - still better than nothing."""
        resources = [self._building("a", None, None), self._building("b", None, None)]
        chosen = nearest_resource(resources, "building", 41.7, -73.9)
        assert chosen is not None
        self.assertEqual(chosen["uuid"], "a")

    def test_no_match_yields_none(self) -> None:
        self.assertIsNone(nearest_resource([{"uuid": "x", "resource_type": "project"}], "building", 41.7, -73.9))


class MediaItemsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()

    def test_builds_one_item_per_attachment(self) -> None:
        data = {
            "resource_uuid": "res-1",
            "attachments": [
                {"id": 1, "kind": "photo", "name": "Front elevation", "content_type": "image/jpeg"},
                {"id": 2, "kind": "document", "attachment_type": "Inventory Form", "content_type": "application/pdf"},
            ],
        }
        items = self.source.media_items(data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].caption, "Front elevation")
        self.assertTrue(items[0].thumb_url)
        self.assertEqual(items[1].caption, "Inventory Form")

    def test_a_document_attachment_gets_a_rendered_thumbnail(self) -> None:
        """A scanned inventory form is a photograph of the building - it belongs
        in the gallery as an image, not as an anonymous grey document icon."""
        data = {"resource_uuid": "res-1", "attachments": [{"id": 2, "kind": "document", "attachment_type": "Inventory Form", "content_type": "application/pdf"}]}
        items = self.source.media_items(data)
        self.assertIn("preview=1", items[0].thumb_url)
        self.assertEqual(items[0].content_type, "application/pdf")

    def test_every_attachment_thumbnails_through_the_proxys_preview_mode(self) -> None:
        """REData reports content_type as blank until a file has been downloaded
        once, so the format can't be decided here - the proxy, which holds the
        bytes, passes an already-displayable file straight through."""
        data = {"resource_uuid": "res-1", "attachments": [{"id": 1, "kind": "photo", "name": "Front", "content_type": ""}]}
        items = self.source.media_items(data)
        self.assertEqual(items[0].thumb_url, f"{items[0].url}?preview=1")

    def test_extracted_images_thumbnail_through_preview_mode_too(self) -> None:
        data = {"resource_uuid": "res-1", "attachments": [{"id": 2, "kind": "document", "extracted_images": [{"id": 9}]}]}
        items = self.source.media_items(data)
        self.assertIn("preview=1", items[1].thumb_url)

    def test_attachments_carry_their_own_resource_uuid(self) -> None:
        """One payload aggregates the nearest building's attachments and the
        site record's, so each must proxy through its own resource."""
        data = {
            "resource_uuid": "res-1",
            "attachments": [
                {"id": 1, "kind": "photo", "name": "Building", "resource_uuid": "res-1"},
                {"id": 5, "kind": "photo", "name": "District", "resource_uuid": "res-9"},
            ],
        }
        items = self.source.media_items(data)
        self.assertIn("res-1", items[0].url)
        self.assertIn("res-9", items[1].url)

    def test_no_resource_uuid_yields_no_items(self) -> None:
        self.assertEqual(self.source.media_items({"attachments": [{"id": 1, "kind": "photo"}]}), [])

    def test_no_attachments_yields_no_items(self) -> None:
        self.assertEqual(self.source.media_items({"resource_uuid": "res-1"}), [])

    def test_extracted_images_yield_additional_items(self) -> None:
        data = {
            "resource_uuid": "res-1",
            "attachments": [
                {"id": 2, "kind": "document", "attachment_type": "Inventory Form", "extracted_images": [{"id": 9}, {"id": 10}]},
            ],
        }
        items = self.source.media_items(data)
        self.assertEqual(len(items), 3)  # the document attachment itself + 2 extracted images
        self.assertEqual(items[1].caption, "Inventory Form")
        self.assertTrue(items[1].thumb_url)
        self.assertEqual(items[2].caption, "Inventory Form")
        self.assertTrue(items[2].thumb_url)

    def test_attachment_with_no_extracted_images_yields_no_extra_items(self) -> None:
        data = {"resource_uuid": "res-1", "attachments": [{"id": 1, "kind": "photo", "name": "Front", "extracted_images": []}]}
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


# REData's CulturalResourceType value is `building_district`; a plain
# "district" matches nothing it ever returns.
_DISTRICT_RESOURCE = {
    "uuid": "res-9",
    "resource_type": "building_district",
    "attributes": {"USNName": "Hudson River State Hospital Historic District", "EligibilityDesc": "Listed"},
}
_DISTRICT_DETAIL = {
    **_DISTRICT_RESOURCE,
    "attachments": [{"id": 5, "kind": "document", "attachment_type": "National Register Nomination", "content_type": "application/pdf"}],
}


class SiteResourceTypeTests(SimpleTestCase):
    def test_a_building_district_is_recognized_as_the_site_record(self) -> None:
        attributes = site_resource_attributes([_BUILDING_RESOURCE, _DISTRICT_RESOURCE])
        self.assertEqual(attributes["USNName"], "Hudson River State Hospital Historic District")
        self.assertEqual(attributes["resource_type"], "building_district")

    def test_a_national_register_listing_is_recognized_too(self) -> None:
        listing = {"uuid": "nr-1", "resource_type": "national_register_listing", "attributes": {"USNName": "Main Building"}}
        self.assertEqual(site_resource_attributes([listing])["USNName"], "Main Building")

    def test_a_building_alone_yields_no_site_record(self) -> None:
        self.assertEqual(site_resource_attributes([_BUILDING_RESOURCE]), {})


class DistrictPayloadTests(TestCase):
    """fetch() caches any site-level resource alongside the building one."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.733150", longitude="-73.930370", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)

    @staticmethod
    def _detail_by_uuid(resource_uuid: str) -> dict:
        return {"res-1": _BUILDING_DETAIL, "res-9": _DISTRICT_DETAIL}[resource_uuid]

    def test_a_district_is_cached_beside_the_building(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE, _DISTRICT_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", side_effect=self._detail_by_uuid),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": []}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data["USNName"], "Old Mill", "the building record must stay at the top level")
        self.assertEqual(data["district"]["USNName"], "Hudson River State Hospital Historic District")

    def test_the_site_records_own_attachments_are_fetched_too(self) -> None:
        """A parcel-scope pin's CRIS media is the district's nomination forms and
        survey photos, not whichever single building happened to be nearest."""
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE, _DISTRICT_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", side_effect=self._detail_by_uuid),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": []}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        attachments = mock_set.call_args[0][2]["attachments"]
        by_resource = {a["resource_uuid"] for a in attachments}
        self.assertEqual(by_resource, {"res-1", "res-9"})

    def test_a_district_alone_is_still_cached(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_DISTRICT_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", side_effect=self._detail_by_uuid),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", return_value={"extracted_images": []}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data["district"]["USNName"], "Hudson River State Hospital Historic District")
        self.assertEqual(len(data["attachments"]), 1, "a location with no surveyed building of its own still has the district's media")

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

    def test_enrichment_does_not_claim_the_media_half(self) -> None:
        """Enrichment fills the info card only - attachments need a per-resource
        detail fetch it deliberately skips."""
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
        ):
            payload, _ = CrisBuildingEnrichmentSource().fetch(location)

        assert payload is not None
        self.assertNotIn("attachments_fetched", payload)

    def test_fetch_returns_none_payload_when_unconfigured(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)

        with patch.object(RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")):
            payload, query_key = CrisBuildingEnrichmentSource().fetch(location)

        self.assertIsNone(payload)
        self.assertEqual(query_key, "42.650000,-73.750000")


class MediaReadinessTests(SimpleTestCase):
    """The panel and the background enrichment source share one cache row.

    Enrichment writes the info-card half only. Before this was accounted for,
    a location enriched in the background rendered as an authoritative "CRIS
    found nothing" in the gallery for the whole cache window, even though CRIS
    had photos and inventory forms for it and nothing had ever asked.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()

    def test_an_enrichment_written_row_is_not_media_ready(self) -> None:
        self.assertFalse(self.source.media_is_ready({"USNName": "Old Mill", "resource_uuid": "res-1", "attachments": []}))

    def test_a_panel_written_row_is_media_ready(self) -> None:
        self.assertTrue(self.source.media_is_ready({"USNName": "Old Mill", "resource_uuid": "res-1", "attachments": [], "attachments_fetched": True}))

    def test_an_empty_row_is_media_ready(self) -> None:
        """"CRIS has nothing here" is a real answer - re-polling it forever isn't."""
        self.assertTrue(self.source.media_is_ready({}))

    def test_other_sources_are_media_ready_by_default(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import get_panel_source

        panel = get_panel_source("smithsonian")
        assert panel is not None
        self.assertTrue(panel.media_is_ready({}))


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


#: A National Register row of the kind REData's nationwide `nps_nrhp` provider
#: returns for any US coordinate, including every New York one. It shares the
#: `resource_type` vocabulary with CRIS and publishes its own position, so it
#: competes on distance - but carries none of CRIS's raw attribute names.
_NRHP_BUILDING = {
    "uuid": "nrhp-1",
    "provider": "nps_nrhp",
    "resource_type": "building",
    # Deliberately nearer the query point than _BUILDING_RESOURCE, so it wins
    # any distance ranking that does not exclude it first.
    "source_latitude": 42.650001,
    "source_longitude": -73.750001,
    "attributes": {"RESNAME": "Some Listed House", "PROPERTY_ID": "77000123"},
}
_NRHP_DISTRICT = {
    "uuid": "nrhp-2",
    "provider": "nps_nrhp",
    "resource_type": "building_district",
    "attributes": {"RESNAME": "Some Historic District"},
}
_CRIS_BUILDING = {**_BUILDING_RESOURCE, "provider": "ny_cris"}
_CRIS_DISTRICT = {**_DISTRICT_RESOURCE, "provider": "ny_cris"}


class ProviderScopingTests(SimpleTestCase):
    """This panel reads CRIS's own attribute names, so it must read CRIS's rows.

    REData answers `/cultural-resources/lookup/` from a registry of state and
    municipal inventories plus the nationwide National Register. Inside New
    York both `ny_cris` and `nps_nrhp` answer, and selecting purely on
    `resource_type` let an NRHP row win - after which `USNName` is absent, the
    card renders nothing, and a nomination PDF from the wrong source lands in
    the CRIS-labelled media tab.
    """

    def test_an_nrhp_building_does_not_win_on_distance(self) -> None:
        resources = [_NRHP_BUILDING, _CRIS_BUILDING]

        chosen = nearest_resource(resources, "building", 42.650000, -73.750000)

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["provider"], "ny_cris", "the nearer NRHP row must not be picked for a CRIS-only card")
        self.assertIn("USNName", chosen["attributes"])

    def test_an_nrhp_district_is_not_taken_as_the_site_record(self) -> None:
        """List order across providers is arbitrary, so first-match is not safe."""
        chosen = site_resource([_NRHP_DISTRICT, _CRIS_DISTRICT])

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["provider"], "ny_cris")

    def test_a_response_with_only_foreign_rows_selects_nothing(self) -> None:
        """Better an absent card than one headed "CRIS" showing another register."""
        self.assertIsNone(nearest_resource([_NRHP_BUILDING], "building", 42.65, -73.75))
        self.assertIsNone(site_resource([_NRHP_DISTRICT]))

    def test_untagged_rows_survive(self) -> None:
        """Cached responses predating REData's provider registry carry no tag."""
        self.assertEqual(cris_only([_BUILDING_RESOURCE]), [_BUILDING_RESOURCE])

    def test_the_lookup_names_its_provider(self) -> None:
        """Filtering client-side still pays for the other providers' queries."""
        location = SimpleNamespace(latitude="42.650000", longitude="-73.750000")
        with patch.object(RedataGateway, "lookup_cultural_resources", return_value=[]) as mock_lookup:
            CrisBuildingEnrichmentSource().fetch(location)

        self.assertEqual(mock_lookup.call_args.kwargs.get("provider"), "ny_cris")
