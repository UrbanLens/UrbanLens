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


def _response(status_code: int, *, json_body: dict | list | None = None, text: str = "", raise_on_json: bool = False, content: bytes = b"", headers: dict | None = None) -> MagicMock:
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
        self.assertEqual(kwargs["params"], {"lat": 42.65, "lng": -73.75, "situs_address": "123 Main St", "apn": "1-2-3"})


class LookupParcelSuccessTests(SimpleTestCase):
    def test_returns_the_record_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body={"record_payload": {"owner_name": ["Jane Smith"], "apn": "1-2-3"}})
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
                "record_payload": {"owner_name": ["Jane Smith"], "parcel_geometry": {"format": "esri_rings", "rings": [[[0.0, 0.0]]]}},
                "parcel_geometry": {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]},
                "building_geometry": {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]},
            },
        )
        gateway = _gateway(session)
        payload = gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(payload["owner_name"], ["Jane Smith"])
        self.assertEqual(payload["parcel_geometry"], {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]})
        self.assertEqual(payload["building_geometry"], {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]})

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
        session.get.return_value = _response(404, json_body={"error": REASON_MANUAL_ONLY, "message": "Call the assessor.", "links": {"assessor_url": "https://example.gov/assessor"}})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_parcel(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_MANUAL_ONLY)
        self.assertEqual(str(ctx.exception), "Call the assessor.")
        self.assertEqual(ctx.exception.links, {"assessor_url": "https://example.gov/assessor"})

    def test_503_raises_with_redatas_own_reason(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(503, json_body={"error": REASON_SOURCE_ERROR, "message": "county server unreachable"})
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
        session.get.return_value = _response(200, json_body={"uuid": "3fae2b1c-0000-0000-0000-000000000000", "record_payload": {}})
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
        session.get.return_value = _response(200, json_body={"results": [{"uuid": "l1", "title": "Retail Building"}], "refresh_queued": True})
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
    def test_returns_the_detail_body(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"uuid": "r1", "attachments": [{"id": 1, "kind": "PHOTO"}]})
        gateway = _gateway(session)
        detail = gateway.fetch_cultural_resource_detail("r1")
        self.assertEqual(detail["attachments"][0]["kind"], "PHOTO")

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
        session.get.return_value = _response(200, json_body=[{"source": "cris", "name": "Reality House", "building_number": "72", "year_built": 1937}])
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
        session.post.return_value = _response(200, json_body={"id": 12, "extracted_data": {"building_number": "166"}, "extracted_images": [{"id": 3}]})
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
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/r1/attachments/12/extracted-images/3/download/")

    def test_404_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "image_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.download_extracted_image("r1", 12, 3)
