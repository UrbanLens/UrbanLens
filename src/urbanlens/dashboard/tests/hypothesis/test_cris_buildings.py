"""Tests for the CRIS Building USN Points plugin."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin import cris_buildings as cris_buildings_module
from urbanlens.dashboard.plugins.builtin.cris_buildings import (
    CrisBuildingEnrichmentSource,
    CrisBuildingPanelSource,
    CrisBuildingsPlugin,
    cris_only,
    nearest_resource,
    site_resource,
    site_resource_attributes,
)
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsUnavailableError,
    RedataGateway,
)
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
    "attributes": {
        "USNNum": "12345",
        "USNName": "Old Mill",
        "HouseNum": "10",
        "StreetName": "Main St",
        "City": "Albany",
        "Zip": "12207",
        "EligibilityDesc": "Listed",
    },
}
_BUILDING_DETAIL = {
    **_BUILDING_RESOURCE,
    "attachments": [
        {"id": 1, "kind": "photo", "name": "Front elevation", "content_type": "image/jpeg"},
        {
            "id": 2,
            "kind": "document",
            "attachment_type": "Building-Structure Inventory Form",
            "content_type": "application/pdf",
        },
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

    def test_fetch_lists_documents_without_waiting_on_extraction(self) -> None:
        """REData's extraction is synchronous AI work that can outlast the request; listing must not wait on it."""
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=_BUILDING_DETAIL),
            patch.object(RedataGateway, "extract_cultural_resource_attachment") as mock_extract,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)

        mock_extract.assert_not_called()
        data = mock_set.call_args[0][2]
        attachments_by_id = {a["id"]: a for a in data["attachments"]}
        self.assertEqual(attachments_by_id[2]["extracted_images"], [])
        self.assertNotIn("extracted_images", attachments_by_id[1])
        task, location_id, resource_uuid, attachment_ids = enqueue.call_args.args
        self.assertEqual(task.__name__, "extract_cris_attachments")
        self.assertEqual((location_id, resource_uuid, attachment_ids), (self.location.pk, "res-1", [2]))

    def test_images_redata_already_extracted_are_kept_and_not_requested_again(self) -> None:
        extracted = {
            **_BUILDING_DETAIL,
            "attachments": [
                {
                    **_BUILDING_DETAIL["attachments"][1],
                    "extracted_at": "2026-09-01T00:00:00Z",
                    "extracted_images": [{"id": 9}],
                }
            ],
        }
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE]),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=extracted),
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)

        self.assertEqual(mock_set.call_args[0][2]["attachments"][0]["extracted_images"], [{"id": 9}])
        enqueue.assert_not_called()

    def test_no_building_resource_found_persists_empty(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "lookup_cultural_resources",
                return_value=[{"uuid": "r2", "resource_type": "archaeological_buffer_area"}],
            ),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "cris_building_usn", {}, query_key="42.65,-73.75")

    def test_a_settled_no_data_answer_persists_empty(self) -> None:
        """Only REData's settled answers are cached - transient ones are ``TransientLookupFailureTests``."""
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "lookup_cultural_resources",
                side_effect=PropertyRecordsUnavailableError("no_data_found", "nothing here"),
            ),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "cris_building_usn", {}, query_key="42.65,-73.75")

    def test_unconfigured_gateway_gracefully_persists_empty(self) -> None:
        """RedataGateway() raises ValueError (not PropertyRecordsUnavailableError) when unconfigured.

        The unconfigured state is simulated rather than left to the ambient environment: an install that *does*
        configure REData would otherwise reach the real API here instead of exercising this branch."""
        with (
            patch.object(
                RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")
            ),
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


class ExtractCrisAttachmentsTaskTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        attachments = [
            {"id": 2, "kind": "document", "resource_uuid": "res-1", "extracted_images": []},
            {"id": 3, "kind": "document", "resource_uuid": "res-1", "extracted_images": []},
            {"id": 2, "kind": "document", "resource_uuid": "res-other", "extracted_images": []},
        ]
        LocationCache.set(self.location, "cris_building_usn", {"attachments": attachments}, query_key="q")

    def test_extracted_images_are_merged_into_the_cached_payload_and_a_failure_skips_only_its_own(self) -> None:
        from urbanlens.dashboard.tasks import extract_cris_attachments

        def extract(_self, resource_uuid, attachment_id, **_kwargs):
            if attachment_id == 3:
                raise PropertyRecordsUnavailableError("extraction_unavailable", "nothing found")
            return {"extracted_images": [{"id": 9}]}

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "extract_cultural_resource_attachment", extract),
        ):
            extract_cris_attachments(self.location.pk, "res-1", [2, 3])

        attachments = LocationCache.objects.get(location=self.location, source="cris_building_usn").data["attachments"]
        self.assertEqual(attachments[0]["extracted_images"], [{"id": 9}])
        self.assertEqual(attachments[1]["extracted_images"], [])
        self.assertEqual(
            attachments[2]["extracted_images"], [], "the same attachment id on another resource was overwritten"
        )


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
            {
                "uuid": "district",
                "resource_type": "building_district",
                "source_latitude": 41.733150,
                "source_longitude": -73.930370,
            },
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
        data = {
            "resource_uuid": "res-1",
            "attachments": [
                {"id": 2, "kind": "document", "attachment_type": "Inventory Form", "content_type": "application/pdf"}
            ],
        }
        items = self.source.media_items(data)
        self.assertIn("preview=1", items[0].thumb_url)
        self.assertEqual(items[0].content_type, "application/pdf")

    def test_every_attachment_thumbnails_through_the_proxys_preview_mode(self) -> None:
        """REData reports content_type as blank until a file has been downloaded
        once, so the format can't be decided here - the proxy, which holds the
        bytes, passes an already-displayable file straight through."""
        data = {
            "resource_uuid": "res-1",
            "attachments": [{"id": 1, "kind": "photo", "name": "Front", "content_type": ""}],
        }
        items = self.source.media_items(data)
        self.assertEqual(items[0].thumb_url, f"{items[0].url}?preview=1")

    def test_extracted_images_thumbnail_through_preview_mode_too(self) -> None:
        data = {
            "resource_uuid": "res-1",
            "attachments": [{"id": 2, "kind": "document", "extracted_images": [{"id": 9}]}],
        }
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
                {
                    "id": 2,
                    "kind": "document",
                    "attachment_type": "Inventory Form",
                    "extracted_images": [{"id": 9}, {"id": 10}],
                },
            ],
        }
        items = self.source.media_items(data)
        self.assertEqual(len(items), 3)  # the document attachment itself + 2 extracted images
        self.assertEqual(items[1].caption, "Inventory Form")
        self.assertTrue(items[1].thumb_url)
        self.assertEqual(items[2].caption, "Inventory Form")
        self.assertTrue(items[2].thumb_url)

    def test_attachment_with_no_extracted_images_yields_no_extra_items(self) -> None:
        data = {
            "resource_uuid": "res-1",
            "attachments": [{"id": 1, "kind": "photo", "name": "Front", "extracted_images": []}],
        }
        self.assertEqual(len(self.source.media_items(data)), 1)


def _stub_pin(*, site_scope: bool = False):
    """A duck-typed pin for render_context, which now consults parcel-vs-building scope.

    ``is_site_scope`` short-circuits on the instance memo, so setting it directly decides the answer without
    needing a database (these are SimpleTestCases)."""
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
        data = {
            **self.building_data,
            "district": {"USNName": "Hudson River State Hospital Historic District", "EligibilityDesc": "Listed"},
        }
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
    "attachments": [
        {
            "id": 5,
            "kind": "document",
            "attachment_type": "National Register Nomination",
            "content_type": "application/pdf",
        }
    ],
}


class SiteResourceTypeTests(SimpleTestCase):
    def test_a_building_district_is_recognized_as_the_site_record(self) -> None:
        attributes = site_resource_attributes([_BUILDING_RESOURCE, _DISTRICT_RESOURCE])
        self.assertEqual(attributes["USNName"], "Hudson River State Hospital Historic District")
        self.assertEqual(attributes["resource_type"], "building_district")

    def test_a_national_register_listing_is_recognized_too(self) -> None:
        listing = {
            "uuid": "nr-1",
            "resource_type": "national_register_listing",
            "attributes": {"USNName": "Main Building"},
        }
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
            patch.object(
                RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE, _DISTRICT_RESOURCE]
            ),
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
            patch.object(
                RedataGateway, "lookup_cultural_resources", return_value=[_BUILDING_RESOURCE, _DISTRICT_RESOURCE]
            ),
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
        self.assertEqual(
            len(data["attachments"]),
            1,
            "a location with no surveyed building of its own still has the district's media",
        )

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
            patch.object(
                RedataGateway,
                "lookup_cultural_resources",
                return_value=[
                    {"uuid": "r2", "resource_type": "archaeological_buffer_area", "attributes": {"USNName": "Buffer"}}
                ],
            ),
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

        with patch.object(
            RedataGateway,
            "lookup_cultural_resources",
            side_effect=PropertyRecordsUnavailableError("source_error", "boom"),
        ):
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

        with patch.object(
            RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")
        ):
            payload, query_key = CrisBuildingEnrichmentSource().fetch(location)

        self.assertIsNone(payload)
        self.assertEqual(query_key, "42.650000,-73.750000")


class MediaReadinessTests(SimpleTestCase):
    """The panel and the background enrichment source share one cache row.

    Enrichment writes the info-card half only."""

    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()

    def test_an_enrichment_written_row_is_not_media_ready(self) -> None:
        self.assertFalse(
            self.source.media_is_ready({"USNName": "Old Mill", "resource_uuid": "res-1", "attachments": []})
        )

    def test_a_panel_written_row_is_media_ready(self) -> None:
        self.assertTrue(
            self.source.media_is_ready(
                {"USNName": "Old Mill", "resource_uuid": "res-1", "attachments": [], "attachments_fetched": True}
            )
        )

    def test_an_empty_row_is_media_ready(self) -> None:
        """ "CRIS has nothing here" is a real answer - re-polling it forever isn't."""
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

    REData answers `/cultural-resources/lookup/` from a registry of state and municipal inventories plus the
    nationwide National Register."""

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


# -- Campus (site-scope) aggregation: P24 ----------------------------------------------------------

#: A district polygon around the campus centre; buildings inside it are the site's.
_CAMPUS_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-73.935, 41.730], [-73.920, 41.730], [-73.920, 41.737], [-73.935, 41.737], [-73.935, 41.730]]],
}
_CAMPUS_DISTRICT = {
    "uuid": "dist-1",
    "provider": "ny_cris",
    "resource_type": "building_district",
    "name": "Hudson River State Hospital",
    "geometry": _CAMPUS_POLYGON,
    "attributes": {"USNName": "Hudson River State Hospital"},
}
_CAMPUS_DISTRICT_DETAIL = {
    **_CAMPUS_DISTRICT,
    "attachments": [{"id": 50, "kind": "document", "name": "NRHP Nomination", "content_type": "application/pdf"}],
    "linked_resources": [],
}


def _campus_building(uuid: str, lat: float, lng: float, name: str, **extra) -> dict:
    return {
        "uuid": uuid,
        "provider": "ny_cris",
        "resource_type": "building",
        "name": name,
        "source_latitude": lat,
        "source_longitude": lng,
        "attributes": {"USNName": name},
        **extra,
    }


def _inventory_form(attachment_id: int) -> dict:
    return {
        "id": attachment_id,
        "kind": "document",
        "name": "Building Inventory Form",
        "attachment_type": "Building-Structure Inventory Form",
        "content_type": "application/pdf",
    }


class CampusAggregationTests(TestCase):
    """A parcel-scope pin gathers every campus building's CRIS documents, each tagged with its building."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.733280", longitude="-73.928120", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)
        self.pin._site_scope_cache = True
        self.main = _campus_building("b-main", 41.733300, -73.928100, "BLDG 51/MAIN/ADMIN")
        # REData already fetched this one's detail, so the lookup row carries its attachments.
        self.chapel = _campus_building(
            "b-chapel",
            41.734000,
            -73.929000,
            "BLDG 28/CATHOLIC CHAPEL",
            detail_retrieved_at="2026-09-01T00:00:00Z",
            attachments=[_inventory_form(21)],
        )
        self.mortuary = _campus_building("b-mortuary", 41.732000, -73.926000, "BLDG 45/MORTUARY & LAB")
        self.shed = _campus_building("b-shed", 41.735500, -73.922000, "BLDG 154/TOOL SHED")
        self.offsite = _campus_building("b-offsite", 41.745000, -73.930000, "UNRELATED FARMHOUSE")
        self.details = {
            "b-main": {**self.main, "attachments": [_inventory_form(11), {"id": 12, "kind": "photo", "name": "Front"}]},
            "b-mortuary": {**self.mortuary, "attachments": [_inventory_form(31)]},
            "b-shed": {**self.shed, "attachments": [{"id": 41, "kind": "photo", "name": "Shed"}]},
            "b-offsite": {**self.offsite, "attachments": [_inventory_form(99)]},
            "dist-1": _CAMPUS_DISTRICT_DETAIL,
        }
        self.resources = [self.offsite, self.shed, self.chapel, self.mortuary, self.main, _CAMPUS_DISTRICT]

    def _detail(self, resource_uuid: str) -> dict:
        return self.details[resource_uuid]

    def _fetch(self, *, bulk_side_effect=None, detail_side_effect=None, lookup_side_effect=None):
        self.lookup_calls = []

        def lookup(_lat, _lng, *, radius_meters, provider=None):
            self.lookup_calls.append(radius_meters)
            return lookup_side_effect(radius_meters) if lookup_side_effect else self.resources

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", side_effect=lookup),
            patch.object(
                RedataGateway, "fetch_cultural_resource_detail", side_effect=detail_side_effect or self._detail
            ) as mock_detail,
            patch.object(
                RedataGateway,
                "queue_cultural_resource_details",
                side_effect=bulk_side_effect,
                return_value={"queued": 3},
            ) as mock_bulk,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as self.mock_enqueue,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        return mock_set.call_args[0][2], mock_detail, mock_bulk

    def test_documents_from_several_buildings_are_cached_each_naming_its_building(self) -> None:
        data, _detail, _bulk = self._fetch()
        subjects = {a["subject"] for a in data["attachments"] if a.get("subject_kind") == "building"}
        self.assertTrue(
            {"BLDG 51/MAIN/ADMIN", "BLDG 28/CATHOLIC CHAPEL", "BLDG 45/MORTUARY & LAB"} <= subjects,
            f"a campus pin must aggregate every building's records, got {subjects}",
        )

    def test_a_building_outside_the_site_polygon_is_left_out(self) -> None:
        data, mock_detail, _bulk = self._fetch()
        self.assertNotIn("b-offsite", {a["resource_uuid"] for a in data["attachments"]})
        self.assertNotIn("b-offsite", [call.args[0] for call in mock_detail.call_args_list])

    def test_attachments_already_on_the_lookup_row_cost_no_detail_call(self) -> None:
        data, mock_detail, _bulk = self._fetch()
        self.assertNotIn("b-chapel", [call.args[0] for call in mock_detail.call_args_list])
        self.assertIn(21, [a["id"] for a in data["attachments"] if a["resource_uuid"] == "b-chapel"])

    def test_the_site_records_documents_are_tagged_as_the_site(self) -> None:
        data, _detail, _bulk = self._fetch()
        district = [a for a in data["attachments"] if a["resource_uuid"] == "dist-1"]
        self.assertEqual([a["subject_kind"] for a in district], ["site"])
        self.assertEqual(district[0]["subject"], "Hudson River State Hospital")

    def test_the_payload_records_that_it_was_fetched_at_site_scope(self) -> None:
        data, _detail, _bulk = self._fetch()
        self.assertIs(data["site_scope"], True)

    def test_redata_is_asked_to_warm_the_whole_site(self) -> None:
        _data, _detail, mock_bulk = self._fetch()
        mock_bulk.assert_called_once()
        self.assertGreaterEqual(mock_bulk.call_args.kwargs["radius_meters"], 200)

    def test_a_read_only_key_refusing_the_bulk_queue_does_not_stop_aggregation(self) -> None:
        data, _detail, _bulk = self._fetch(bulk_side_effect=PropertyRecordsUnavailableError("source_error", "403"))
        self.assertIn("b-mortuary", {a["resource_uuid"] for a in data["attachments"]})

    def test_one_buildings_failed_detail_skips_only_that_building(self) -> None:
        def detail(resource_uuid: str) -> dict:
            if resource_uuid == "b-mortuary":
                raise PropertyRecordsUnavailableError("cultural_resource_provider_unavailable", "down")
            return self._detail(resource_uuid)

        data, _detail, _bulk = self._fetch(detail_side_effect=detail)
        resources = {a["resource_uuid"] for a in data["attachments"]}
        self.assertNotIn("b-mortuary", resources)
        self.assertIn("b-chapel", resources)

    def test_buildings_linked_from_the_site_record_are_included(self) -> None:
        """CRIS's own roster: a stub with no published position, reachable only by its link."""
        self.details["dist-1"] = {
            **_CAMPUS_DISTRICT_DETAIL,
            "linked_resources": [
                {
                    "uuid": "b-stub",
                    "resource_type": "building",
                    "external_id": "02714.000140",
                    "name": "BLDG 140/LAUNDRY",
                },
                {"uuid": "p-1", "resource_type": "project", "external_id": "P1", "name": "Some Project"},
            ],
        }
        self.details["b-stub"] = {
            "uuid": "b-stub",
            "resource_type": "building",
            "name": "BLDG 140/LAUNDRY",
            "attachments": [_inventory_form(141)],
        }
        data, mock_detail, _bulk = self._fetch()
        self.assertIn("BLDG 140/LAUNDRY", {a.get("subject") for a in data["attachments"]})
        self.assertNotIn("p-1", [call.args[0] for call in mock_detail.call_args_list])

    def test_live_detail_fetches_are_capped_per_pass(self) -> None:
        many = [
            _campus_building(f"b-{index}", 41.7305 + index * 0.0002, -73.9300, f"BLDG {index}") for index in range(30)
        ]
        self.resources = [*many, _CAMPUS_DISTRICT]
        for resource in many:
            self.details[resource["uuid"]] = {**resource, "attachments": [_inventory_form(1000 + len(self.details))]}
        _data, mock_detail, _bulk = self._fetch()
        # The primary building and the site record are fetched on top of the capped campus buildings.
        self.assertLessEqual(mock_detail.call_count, cris_buildings_module._MAX_SITE_DETAIL_FETCHES + 2)

    def test_a_site_wider_than_the_first_search_widens_it_to_its_footprint(self) -> None:
        wide = {
            "type": "Polygon",
            "coordinates": [
                [[-73.945, 41.725], [-73.910, 41.725], [-73.910, 41.742], [-73.945, 41.742], [-73.945, 41.725]]
            ],
        }
        district = {**_CAMPUS_DISTRICT, "geometry": wide}
        far = _campus_building("b-far", 41.741000, -73.944000, "BLDG 99/FAR WARD")
        self.details["b-far"] = {**far, "attachments": [_inventory_form(91)]}

        neighbour = {**_CAMPUS_DISTRICT, "uuid": "dist-neighbour", "name": "Neighbouring District"}

        def lookup(radius_meters: float) -> list[dict]:
            if radius_meters > 1000:
                return [neighbour, self.main, district, far]
            return [self.main, district]

        data, _detail, mock_bulk = self._fetch(lookup_side_effect=lookup)
        self.assertEqual(data["district"]["resource_uuid"], "dist-1", "the wider search must not swap the site")
        self.assertEqual(self.lookup_calls[0], cris_buildings_module._SITE_RADIUS_METERS)
        self.assertEqual(
            self.lookup_calls[-1], cris_buildings_module._MAX_SITE_RADIUS_METERS, "clamped, not county-wide"
        )
        self.assertIn("b-far", {a["resource_uuid"] for a in data["attachments"]})
        self.assertEqual(mock_bulk.call_args.kwargs["radius_meters"], cris_buildings_module._MAX_SITE_RADIUS_METERS)

    def test_a_site_within_the_first_search_costs_one_lookup(self) -> None:
        compact = {
            "type": "Polygon",
            "coordinates": [
                [[-73.931, 41.731], [-73.925, 41.731], [-73.925, 41.735], [-73.931, 41.735], [-73.931, 41.731]]
            ],
        }
        self.resources = [*self.resources[:-1], {**_CAMPUS_DISTRICT, "geometry": compact}]
        self._fetch()
        self.assertEqual(self.lookup_calls, [cris_buildings_module._SITE_RADIUS_METERS])

    def test_a_point_only_site_does_not_filter_the_campus(self) -> None:
        self.resources = [
            *self.resources[:-1],
            {**_CAMPUS_DISTRICT, "geometry": {"type": "Point", "coordinates": [-73.9, 41.7]}},
        ]
        data, _detail, _bulk = self._fetch()
        self.assertIn("b-offsite", {a["resource_uuid"] for a in data["attachments"]})

    def test_campus_documents_skip_ai_extraction(self) -> None:
        self._fetch()
        self.assertEqual({call.args[2] for call in self.mock_enqueue.call_args_list}, {"b-main", "dist-1"})

    def test_campus_buildings_stay_out_of_the_media_gallery(self) -> None:
        data, _detail, _bulk = self._fetch()
        urls = [item.url for item in CrisBuildingPanelSource().media_items(data)]
        self.assertFalse([url for url in urls if "/b-chapel/" in url or "/b-mortuary/" in url], urls)
        self.assertTrue([url for url in urls if "/b-main/" in url])

    def test_a_building_scope_pin_keeps_to_its_own_building_and_site(self) -> None:
        self.pin._site_scope_cache = False
        data, _detail, mock_bulk = self._fetch()
        self.assertEqual({a["resource_uuid"] for a in data["attachments"]}, {"b-main", "dist-1"})
        mock_bulk.assert_not_called()
        self.assertIs(data["site_scope"], False)

    def test_the_building_scope_record_names_its_own_building(self) -> None:
        self.pin._site_scope_cache = False
        data, _detail, _bulk = self._fetch()
        main = [a for a in data["attachments"] if a["resource_uuid"] == "b-main"]
        self.assertEqual({a["subject"] for a in main}, {"BLDG 51/MAIN/ADMIN"})
        self.assertEqual({a["subject_kind"] for a in main}, {"building"})


class TransientLookupFailureTests(TestCase):
    """An outage must not be cached as "CRIS has nothing here" for the whole cache window."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)

    def test_a_transient_failure_is_raised_not_cached(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "lookup_cultural_resources",
                side_effect=PropertyRecordsUnavailableError("source_error", "timed out"),
            ),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
            self.assertRaises(PropertyRecordsUnavailableError),
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_not_called()

    def test_a_rate_limited_lookup_is_raised_not_cached(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "lookup_cultural_resources",
                side_effect=PropertyRecordsUnavailableError("rate_limited", "later"),
            ),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
            self.assertRaises(PropertyRecordsUnavailableError),
        ):
            CrisBuildingPanelSource().fetch(self.pin)
        mock_set.assert_not_called()


# -- Article > Sources documents --------------------------------------------------------------------


class SourceDocumentsTests(SimpleTestCase):
    """The PDF attachments this source lists under Article > Sources."""

    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()
        self.data = {
            "resource_uuid": "b-main",
            "site_scope": True,
            "attachments_fetched": True,
            "attachments": [
                {**_inventory_form(11), "resource_uuid": "b-main", "subject": "Main", "subject_kind": "building"},
                {"id": 12, "kind": "photo", "content_type": "image/jpeg", "resource_uuid": "b-main"},
                {"id": 13, "kind": "document", "content_type": "image/tiff", "resource_uuid": "b-main"},
                {"id": 14, "kind": "document", "content_type": "", "name": "Scan", "resource_uuid": "b-main"},
                {
                    **_inventory_form(50),
                    "resource_uuid": "dist-1",
                    "subject": "Hudson River State Hospital",
                    "subject_kind": "site",
                },
                {
                    **_inventory_form(21),
                    "resource_uuid": "b-chapel",
                    "subject": "Chapel",
                    "subject_kind": "building",
                    "site_building": True,
                },
            ],
        }

    def test_only_pdf_documents_are_listed(self) -> None:
        ids = [doc.document_id for doc in self.source.source_documents(self.data, site_scope=True)]
        self.assertEqual(ids, ["b-main.11", "b-main.14", "dist-1.50", "b-chapel.21"])

    def test_every_listed_document_is_a_pdf(self) -> None:
        types = {doc.content_type for doc in self.source.source_documents(self.data, site_scope=True)}
        self.assertEqual(types, {"application/pdf"})

    def test_documents_carry_what_they_describe(self) -> None:
        docs = {doc.document_id: doc for doc in self.source.source_documents(self.data, site_scope=True)}
        self.assertEqual((docs["b-chapel.21"].subject, docs["b-chapel.21"].subject_kind), ("Chapel", "building"))
        self.assertEqual(docs["dist-1.50"].subject_kind, "site")

    def test_a_building_scope_viewer_does_not_see_the_other_campus_buildings(self) -> None:
        ids = [doc.document_id for doc in self.source.source_documents(self.data, site_scope=False)]
        self.assertNotIn("b-chapel.21", ids)
        self.assertIn("b-main.11", ids)

    def test_duplicate_attachments_are_listed_once(self) -> None:
        self.data["attachments"].append({**_inventory_form(11), "resource_uuid": "b-main"})
        ids = [doc.document_id for doc in self.source.source_documents(self.data, site_scope=True)]
        self.assertEqual(ids.count("b-main.11"), 1)

    def test_find_document_only_answers_for_a_listed_document(self) -> None:
        self.assertIsNotNone(self.source.find_document(self.data, "dist-1.50", site_scope=True))
        self.assertIsNone(self.source.find_document(self.data, "b-main.12", site_scope=True), "a photo is not a source")
        self.assertIsNone(self.source.find_document(self.data, "someone-else.1", site_scope=True))
        self.assertIsNone(self.source.find_document(self.data, "b-chapel.21", site_scope=False))

    def test_download_asks_redata_for_that_attachment(self) -> None:
        document = self.source.find_document(self.data, "dist-1.50", site_scope=True)
        assert document is not None
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway, "download_cultural_resource_attachment", return_value=(b"%PDF-1.4", "application/pdf")
            ) as mock_download,
        ):
            content, _content_type = self.source.download_document(document)
        mock_download.assert_called_once_with("dist-1", 50)
        self.assertEqual(content, b"%PDF-1.4")

    def test_an_unavailable_attachment_raises_document_unavailable(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import DocumentUnavailableError

        document = self.source.find_document(self.data, "dist-1.50", site_scope=True)
        assert document is not None
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "download_cultural_resource_attachment",
                side_effect=PropertyRecordsUnavailableError("attachment_unavailable", "gone"),
            ),
            self.assertRaises(DocumentUnavailableError),
        ):
            self.source.download_document(document)

    def test_an_oversized_or_throttled_download_raises_document_unavailable(self) -> None:
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError
        from urbanlens.dashboard.services.pins.external_data import DocumentUnavailableError

        document = self.source.find_document(self.data, "dist-1.50", site_scope=True)
        assert document is not None
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "download_cultural_resource_attachment",
                side_effect=GatewayRequestError("CRIS attachment is larger than the 50MB limit for proxied media"),
            ),
            self.assertRaises(DocumentUnavailableError),
        ):
            self.source.download_document(document)

    def test_the_document_bytes_share_the_gallery_proxys_cache_entry(self) -> None:
        document = self.source.find_document(self.data, "dist-1.50", site_scope=True)
        assert document is not None
        self.assertEqual(self.source.document_cache_key(document), "ul_cris_attachment_dist-1_50")


class DocumentsReadyTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = CrisBuildingPanelSource()

    def test_an_enrichment_written_row_is_not_ready(self) -> None:
        self.assertFalse(self.source.documents_ready({"USNName": "Old Mill", "attachments": []}, site_scope=False))

    def test_a_building_scope_payload_is_not_ready_for_a_site_scope_viewer(self) -> None:
        data = {"attachments": [], "attachments_fetched": True, "site_scope": False}
        self.assertFalse(self.source.documents_ready(data, site_scope=True))
        self.assertTrue(self.source.documents_ready(data, site_scope=False))

    def test_a_site_scope_payload_is_ready_for_either(self) -> None:
        data = {"attachments": [], "attachments_fetched": True, "site_scope": True}
        self.assertTrue(self.source.documents_ready(data, site_scope=True))
        self.assertTrue(self.source.documents_ready(data, site_scope=False))

    def test_nothing_found_is_a_ready_answer(self) -> None:
        self.assertTrue(self.source.documents_ready({}, site_scope=True))
