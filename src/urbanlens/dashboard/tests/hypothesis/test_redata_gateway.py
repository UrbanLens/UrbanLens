"""Tests for RedataGateway, the REST client for the standalone REData property-records service.

All HTTP calls are mocked so no real network access occurs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_MANUAL_ONLY,
    REASON_SOURCE_ERROR,
    PropertyRecordsUnavailableError,
    RedataGateway,
)


def _response(
    status_code: int,
    *,
    json_body: dict | list | None = None,
    text: str = "",
    raise_on_json: bool = False,
    content: bytes = b"",
    headers: dict | None = None,
) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    resp.headers = headers or {}
    if raise_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _gateway(session: MagicMock | None = None) -> RedataGateway:
    """Return a RedataGateway with fake config and a stub session (no real HTTP)."""
    return RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session or MagicMock())


class ConstructionTests(SimpleTestCase):
    def test_missing_base_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            RedataGateway(base_url=None, api_key="test-key", session=MagicMock())

    def test_missing_api_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            RedataGateway(base_url="https://redata.example.test", api_key=None, session=MagicMock())


class LookupParcelRequestTests(SimpleTestCase):
    """Verifies the request URL/params/headers RedataGateway builds."""

    def test_sends_bearer_auth_header(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {}})
        gateway = _gateway(session)
        gateway.lookup_parcel(42.65, -73.75)
        _args, kwargs = session.get.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_url_hits_the_lookup_endpoint_with_a_trailing_slash_normalized(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {}})
        gateway = RedataGateway(base_url="https://redata.example.test/", api_key="test-key", session=session)
        gateway.lookup_parcel(42.65, -73.75)
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/lookup/")

    def test_lat_lng_are_always_sent(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {}})
        gateway = _gateway(session)
        gateway.lookup_parcel(42.65, -73.75)
        _args, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"], {"lat": 42.65, "lng": -73.75})

    def test_situs_address_and_apn_are_included_only_when_given(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {}})
        gateway = _gateway(session)
        gateway.lookup_parcel(42.65, -73.75, situs_address="123 Main St", apn="1-2-3")
        _args, kwargs = session.get.call_args
        self.assertEqual(
            kwargs["params"], {"lat": 42.65, "lng": -73.75, "situs_address": "123 Main St", "apn": "1-2-3"}
        )


class LookupParcelSuccessTests(SimpleTestCase):
    def test_returns_the_record_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200, json_body={"record_payload": {"owner_name": ["Jane Smith"], "apn": "1-2-3"}}
        )
        gateway = _gateway(session)
        payload = gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(payload, {"owner_name": ["Jane Smith"], "apn": "1-2-3"})

    def test_missing_record_payload_returns_an_empty_dict(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={})
        gateway = _gateway(session)
        self.assertEqual(gateway.lookup_parcel(42.65, -73.75), {})

    def test_top_level_geojson_geometry_overrides_record_payloads_own_copy(self) -> None:
        """The top-level Parcel fields are already-converted GeoJSON; record_payload's own
        parcel_geometry/building_geometry are still the raw Esri-ring-shaped snapshot."""
        session = MagicMock()
        session.get.return_value = _response(
            200,
            json_body={
                "record_payload": {
                    "owner_name": ["Jane Smith"],
                    "parcel_geometry": {"format": "esri_rings", "rings": [[[0.0, 0.0]]]},
                },
                "parcel_geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
                "building_geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            },
        )
        gateway = _gateway(session)
        payload = gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(payload["owner_name"], ["Jane Smith"])
        self.assertEqual(
            payload["parcel_geometry"],
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]},
        )
        self.assertEqual(
            payload["building_geometry"],
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]},
        )

    def test_no_top_level_geometry_leaves_record_payload_untouched(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {"owner_name": ["Jane Smith"]}})
        gateway = _gateway(session)
        payload = gateway.lookup_parcel(42.65, -73.75)
        self.assertNotIn("parcel_geometry", payload)
        self.assertNotIn("building_geometry", payload)

    def test_unparseable_200_response_raises_source_error(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, raise_on_json=True)
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_SOURCE_ERROR)


class LookupParcelErrorResponseTests(SimpleTestCase):
    def test_404_raises_with_redatas_own_reason(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            404,
            json_body={
                "error": REASON_MANUAL_ONLY,
                "message": "Call the assessor.",
                "links": {"assessor_url": "https://example.gov/assessor"},
            },
        )
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_MANUAL_ONLY)
        self.assertEqual(str(ctx.exception), "Call the assessor.")
        self.assertEqual(ctx.exception.links, {"assessor_url": "https://example.gov/assessor"})

    def test_503_raises_with_redatas_own_reason(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            503, json_body={"error": REASON_SOURCE_ERROR, "message": "county server unreachable"}
        )
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_SOURCE_ERROR)

    def test_404_with_unparseable_body_falls_back_to_source_error(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, raise_on_json=True)
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_SOURCE_ERROR)

    def test_error_response_with_no_links_yields_an_empty_dict_not_none(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "no_data_found", "message": "nothing found"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.links, {})

    def test_top_level_uuid_is_included_in_the_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200,
            json_body={
                "uuid": "3fae2b1c-0000-0000-0000-000000000000",
                "record_payload": {"owner_name": ["Jane Smith"]},
            },
        )
        gateway = _gateway(session)
        payload = gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(payload["uuid"], "3fae2b1c-0000-0000-0000-000000000000")

    def test_missing_top_level_uuid_leaves_payload_without_one(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {"owner_name": ["Jane Smith"]}})
        gateway = _gateway(session)
        payload = gateway.lookup_parcel(42.65, -73.75)
        self.assertNotIn("uuid", payload)

    def test_unexpected_status_code_raises_source_error(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(500, text="internal server error")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_SOURCE_ERROR)

    def test_network_error_raises_source_error(self) -> None:
        session = MagicMock()
        session.get.side_effect = ConnectionError("connection refused")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_SOURCE_ERROR)


# -- lookup_parcel_uuid ------------------------------------------------------------


class LookupParcelUuidTests(SimpleTestCase):
    def test_returns_the_uuid(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200, json_body={"uuid": "3fae2b1c-0000-0000-0000-000000000000", "record_payload": {}}
        )
        gateway = _gateway(session)
        self.assertEqual(gateway.lookup_parcel_uuid(42.65, -73.75), "3fae2b1c-0000-0000-0000-000000000000")

    def test_missing_uuid_returns_none(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {}})
        gateway = _gateway(session)
        self.assertIsNone(gateway.lookup_parcel_uuid(42.65, -73.75))

    def test_hits_the_same_lookup_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"uuid": "x"})
        gateway = _gateway(session)
        gateway.lookup_parcel_uuid(42.65, -73.75, situs_address="123 Main St")
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/lookup/")
        self.assertEqual(kwargs["params"]["situs_address"], "123 Main St")


# -- lookup_listings / download_listing_photo ---------------------------------------


class LookupListingsTests(SimpleTestCase):
    def test_returns_the_full_body(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200, json_body={"results": [{"uuid": "l1", "title": "Retail Building"}], "refresh_queued": True}
        )
        gateway = _gateway(session)
        body = gateway.lookup_listings("parcel-uuid")
        self.assertEqual(body["refresh_queued"], True)
        self.assertEqual(body["results"][0]["title"], "Retail Building")

    def test_hits_the_parcel_scoped_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"results": []})
        gateway = _gateway(session)
        gateway.lookup_listings("parcel-uuid")
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/parcel-uuid/listings/")

    def test_404_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "no_situs_address"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.lookup_listings("parcel-uuid")


class DownloadListingPhotoTests(SimpleTestCase):
    def test_returns_bytes_and_content_type(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, content=b"jpeg-bytes", headers={"Content-Type": "image/jpeg"})
        gateway = _gateway(session)
        content, content_type = gateway.download_listing_photo("listing-uuid", 1)
        self.assertEqual(content, b"jpeg-bytes")
        self.assertEqual(content_type, "image/jpeg")

    def test_hits_the_listing_photo_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, content=b"x")
        gateway = _gateway(session)
        gateway.download_listing_photo("listing-uuid", 7)
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/listings/listing-uuid/photos/7/download/")

    def test_404_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "photo_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.download_listing_photo("listing-uuid", 1)


# -- Cultural resources (CRIS) -------------------------------------------------------


class LookupCulturalResourcesTests(SimpleTestCase):
    def test_bare_array_response_is_returned_as_is(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=[{"uuid": "r1", "resource_type": "building"}])
        gateway = _gateway(session)
        results = gateway.lookup_cultural_resources(42.65, -73.75)
        self.assertEqual(results, [{"uuid": "r1", "resource_type": "building"}])

    def test_results_wrapped_response_is_also_handled(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"results": [{"uuid": "r1"}]})
        gateway = _gateway(session)
        self.assertEqual(gateway.lookup_cultural_resources(42.65, -73.75), [{"uuid": "r1"}])

    def test_empty_array_outside_coverage(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=[])
        gateway = _gateway(session)
        self.assertEqual(gateway.lookup_cultural_resources(42.65, -73.75), [])

    def test_hits_the_lookup_endpoint_with_radius(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=[])
        gateway = _gateway(session)
        gateway.lookup_cultural_resources(42.65, -73.75, radius_meters=500)
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/lookup/")
        self.assertEqual(kwargs["params"], {"lat": 42.65, "lng": -73.75, "radius_meters": 500})


class FetchCulturalResourceDetailTests(SimpleTestCase):
    def test_unwraps_the_resource_from_redatas_envelope(self) -> None:
        """REData answers ``{"detail_status": ..., "resource": {...}}``.

        Handing the envelope on made every caller's ``attributes``/``attachments`` read come back empty, which
        is indistinguishable from a resource that genuinely has neither - so no CRIS record ever produced an
        info card or a single photo."""
        session = MagicMock()
        session.post.return_value = _response(
            200,
            json_body={
                "detail_status": "fetched",
                "resource": {
                    "uuid": "r1",
                    "attributes": {"USNName": "Old Mill"},
                    "attachments": [{"id": 1, "kind": "photo"}],
                },
            },
        )
        gateway = _gateway(session)
        detail = gateway.fetch_cultural_resource_detail("r1")
        self.assertEqual(detail["uuid"], "r1")
        self.assertEqual(detail["attributes"]["USNName"], "Old Mill")
        self.assertEqual(detail["attachments"][0]["kind"], "photo")

    def test_an_unenveloped_body_is_returned_as_is(self) -> None:
        """Defensive: a body with no ``resource`` key is already the resource."""
        session = MagicMock()
        session.post.return_value = _response(
            200, json_body={"uuid": "r1", "attachments": [{"id": 1, "kind": "photo"}]}
        )
        gateway = _gateway(session)
        detail = gateway.fetch_cultural_resource_detail("r1")
        self.assertEqual(detail["attachments"][0]["kind"], "photo")

    def test_hits_the_fetch_detail_endpoint(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={})
        gateway = _gateway(session)
        gateway.fetch_cultural_resource_detail("r1")
        args, _kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/r1/fetch-detail/")

    def test_400_no_detail_available_raises_unavailable(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(400, json_body={"error": "no_detail_available"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.fetch_cultural_resource_detail("r1")


class DownloadCulturalResourceAttachmentTests(SimpleTestCase):
    def test_returns_bytes_and_content_type(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, content=b"pdf-bytes", headers={"Content-Type": "application/pdf"})
        gateway = _gateway(session)
        content, content_type = gateway.download_cultural_resource_attachment("r1", 5)
        self.assertEqual(content, b"pdf-bytes")
        self.assertEqual(content_type, "application/pdf")

    def test_hits_the_attachment_download_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, content=b"x")
        gateway = _gateway(session)
        gateway.download_cultural_resource_attachment("r1", 5)
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/r1/attachments/5/download/")

    def test_404_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "attachment_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.download_cultural_resource_attachment("r1", 5)


# -- lookup_buildings ---------------------------------------------------------------


class LookupBuildingsTests(SimpleTestCase):
    def test_returns_the_building_list(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200, json_body=[{"source": "cris", "name": "Reality House", "building_number": "72", "year_built": 1937}]
        )
        gateway = _gateway(session)
        buildings = gateway.lookup_buildings("parcel-uuid")
        self.assertEqual(buildings[0]["name"], "Reality House")

    def test_hits_the_parcel_scoped_buildings_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=[])
        gateway = _gateway(session)
        gateway.lookup_buildings("parcel-uuid")
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/parcel-uuid/buildings/")

    def test_non_list_body_returns_empty_list(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"unexpected": "shape"})
        gateway = _gateway(session)
        self.assertEqual(gateway.lookup_buildings("parcel-uuid"), [])

    def test_network_error_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.side_effect = ConnectionError("connection refused")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.lookup_buildings("parcel-uuid")


# -- extract_cultural_resource_attachment / download_extracted_image ----------------


class ExtractCulturalResourceAttachmentTests(SimpleTestCase):
    def test_returns_the_extraction_body(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(
            200, json_body={"id": 12, "extracted_data": {"building_number": "166"}, "extracted_images": [{"id": 3}]}
        )
        gateway = _gateway(session)
        result = gateway.extract_cultural_resource_attachment("r1", 12)
        self.assertEqual(result["extracted_images"], [{"id": 3}])

    def test_hits_the_extract_endpoint(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={})
        gateway = _gateway(session)
        gateway.extract_cultural_resource_attachment("r1", 12)
        args, _kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/r1/attachments/12/extract/")

    def test_400_not_extractable_raises_unavailable(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(400, json_body={"error": "not_extractable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.extract_cultural_resource_attachment("r1", 12)
        self.assertEqual(ctx.exception.reason, "not_extractable")

    def test_503_extraction_unavailable_raises_unavailable(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(503, json_body={"error": "extraction_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.extract_cultural_resource_attachment("r1", 12)
        self.assertEqual(ctx.exception.reason, "extraction_unavailable")

    def test_network_error_raises_unavailable(self) -> None:
        session = MagicMock()
        session.post.side_effect = ConnectionError("connection refused")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.extract_cultural_resource_attachment("r1", 12)


# -- lookup_coverage ------------------------------------------------------------------


class LookupCoverageTests(SimpleTestCase):
    def test_returns_the_coverage_mapping(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200,
            json_body={
                "assessments": {"available": False, "reason": "outside every coverage area"},
                "sale_records": {"available": True, "reason": "covered by provider(s): cook_county"},
                "demographics": {"available": True, "reason": "coordinate is within the USA"},
            },
        )
        gateway = _gateway(session)
        coverage = gateway.lookup_coverage("parcel-uuid")
        self.assertEqual(coverage["assessments"], {"available": False, "reason": "outside every coverage area"})
        self.assertEqual(coverage["sale_records"]["available"], True)

    def test_hits_the_parcel_scoped_coverage_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={})
        gateway = _gateway(session)
        gateway.lookup_coverage("parcel-uuid")
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/parcel-uuid/coverage/")

    def test_non_dict_body_returns_empty_dict(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=[])
        gateway = _gateway(session)
        self.assertEqual(gateway.lookup_coverage("parcel-uuid"), {})

    def test_network_error_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.side_effect = ConnectionError("connection refused")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.lookup_coverage("parcel-uuid")


# -- lookup_demographics --------------------------------------------------------------


class LookupDemographicsTests(SimpleTestCase):
    def test_returns_the_demographics_dict(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200,
            json_body={
                "demographics": {
                    "uuid": "d1",
                    "population": 295911,
                    "median_household_income": "81234.00",
                    "median_home_value": "358900.00",
                    "median_gross_rent": "1345.00",
                    "percent_owner_occupied": "68.20",
                    "percent_renter_occupied": "31.80",
                }
            },
        )
        gateway = _gateway(session)
        demographics = gateway.lookup_demographics("parcel-uuid")
        assert demographics is not None
        self.assertEqual(demographics["population"], 295911)
        self.assertEqual(demographics["median_household_income"], "81234.00")

    def test_null_demographics_returns_none(self) -> None:
        """Null when the parcel has no known coordinate, or is outside the USA."""
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"demographics": None})
        gateway = _gateway(session)
        self.assertIsNone(gateway.lookup_demographics("parcel-uuid"))

    def test_hits_the_parcel_scoped_demographics_endpoint_with_no_level_param(self) -> None:
        """census_tract is REData's own default and the right one for a single parcel."""
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"demographics": None})
        gateway = _gateway(session)
        gateway.lookup_demographics("parcel-uuid")
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/parcel-uuid/demographics/")
        self.assertIsNone(kwargs.get("params"))

    def test_503_rate_limited_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(503, json_body={"error": "rate_limited"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_demographics("parcel-uuid")
        self.assertEqual(ctx.exception.reason, "rate_limited")

    def test_503_census_data_api_unavailable_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(503, json_body={"error": "census_data_api_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_demographics("parcel-uuid")
        self.assertEqual(ctx.exception.reason, "census_data_api_unavailable")


# -- lookup_national_parks --------------------------------------------------------------


class LookupNationalParksTests(SimpleTestCase):
    def test_returns_the_full_body(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200,
            json_body={
                "containing_park": {"park_code": "yell", "full_name": "Yellowstone National Park"},
                "nearby_parks": [{"park_code": "grte", "full_name": "Grand Teton National Park"}],
            },
        )
        gateway = _gateway(session)
        result = gateway.lookup_national_parks("parcel-uuid")
        self.assertEqual(result["containing_park"]["full_name"], "Yellowstone National Park")
        self.assertEqual(len(result["nearby_parks"]), 1)

    def test_hits_the_parcel_scoped_national_parks_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"containing_park": None, "nearby_parks": []})
        gateway = _gateway(session)
        gateway.lookup_national_parks("parcel-uuid")
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/parcels/parcel-uuid/national-parks/")

    def test_no_containing_park_is_null(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"containing_park": None, "nearby_parks": []})
        gateway = _gateway(session)
        result = gateway.lookup_national_parks("parcel-uuid")
        self.assertIsNone(result["containing_park"])

    def test_network_error_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.side_effect = ConnectionError("connection refused")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.lookup_national_parks("parcel-uuid")


class DownloadExtractedImageTests(SimpleTestCase):
    def test_returns_bytes_and_content_type(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, content=b"jpeg-bytes", headers={"Content-Type": "image/jpeg"})
        gateway = _gateway(session)
        content, content_type = gateway.download_extracted_image("r1", 12, 3)
        self.assertEqual(content, b"jpeg-bytes")
        self.assertEqual(content_type, "image/jpeg")

    def test_hits_the_extracted_image_download_endpoint(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, content=b"x")
        gateway = _gateway(session)
        gateway.download_extracted_image("r1", 12, 3)
        args, _kwargs = session.get.call_args
        self.assertEqual(
            args[0],
            "https://redata.example.test/api/v1/cultural-resources/r1/attachments/12/extracted-images/3/download/",
        )

    def test_404_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "image_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.download_extracted_image("r1", 12, 3)
