"""Tests for RedataGateway, the REST client for the standalone REData property-records service.

All HTTP calls are mocked so no real network access occurs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
from unittest import mock
from unittest.mock import MagicMock

from urllib3.response import HTTPResponse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_DETAIL_UNRESOLVED,
    REASON_MANUAL_ONLY,
    REASON_SOURCE_ERROR,
    TRANSIENT_REASONS,
    PropertyRecordsBusyError,
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_MAX_SECONDS, UpstreamBusyError
from urbanlens.dashboard.tests.hypothesis.redata_helpers import BUDGET_REFUSALS, detail_unresolved_body


def _response(
    status_code: int,
    *,
    json_body: dict | list | None = None,
    text: str = "",
    raise_on_json: bool = False,
    content: bytes = b"",
    headers: dict | None = None,
) -> MagicMock:
    """Build a mock requests.Response, streamed the way ``stream=True`` leaves it for ``read_capped``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    resp.headers = headers or {}
    resp._content_consumed = False
    resp.raw = HTTPResponse(body=io.BytesIO(content), headers=headers or {}, status=status_code, preload_content=False)
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

    def test_top_level_geojson_parcel_geometry_overrides_record_payloads_own_copy(self) -> None:
        """The Parcel's top-level parcel_geometry is already-converted GeoJSON; record_payload's own
        parcel_geometry/building_geometry are the raw Esri-ring snapshot, and the building one is the only copy."""
        building_rings = {"format": "esri_rings", "rings": [[[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 2.0]]]}
        session = MagicMock()
        session.get.return_value = _response(
            200,
            json_body={
                "record_payload": {
                    "owner_name": ["Jane Smith"],
                    "parcel_geometry": {"format": "esri_rings", "rings": [[[0.0, 0.0]]]},
                    "building_geometry": building_rings,
                },
                "parcel_geometry": {
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
        self.assertEqual(payload["building_geometry"], building_rings)

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


class FetchCulturalResourceDetailUnresolvedTests(SimpleTestCase):
    """REData holds a resource its source cannot resolve, and answers 200 ``unresolved`` with the resource and no detail.

    Handed on as the resource, that answer reads as a fetched record, and the share cache keeps it for an hour.
    """

    def _gateway(self, *bodies: dict) -> tuple[RedataGateway, MagicMock]:
        session = MagicMock()
        session.post.side_effect = [_response(200, json_body=body) for body in bodies]
        return _gateway(session), session

    def test_it_raises_the_transient_reason_with_redatas_message(self) -> None:
        gateway, _session = self._gateway(detail_unresolved_body())

        with self.assertRaises(PropertyRecordsUnavailableError) as caught:
            gateway.fetch_cultural_resource_detail("r1")

        self.assertEqual(caught.exception.reason, REASON_DETAIL_UNRESOLVED)
        self.assertEqual(
            str(caught.exception),
            "The source could not resolve this resource to a record, so nothing more was fetched.",
        )

    def test_the_reason_is_one_a_caller_treats_as_ask_again_later(self) -> None:
        gateway, _session = self._gateway(detail_unresolved_body())

        with self.assertRaises(PropertyRecordsUnavailableError) as caught:
            gateway.fetch_cultural_resource_detail("r1")

        self.assertIn(REASON_DETAIL_UNRESOLVED, TRANSIENT_REASONS)
        self.assertTrue(
            caught.exception.is_outage, "a caller that stores a settled 'no detail' for it would never ask again"
        )

    def test_it_carries_the_wait_like_a_throttle_does(self) -> None:
        gateway, _session = self._gateway(detail_unresolved_body())

        with self.assertRaises(PropertyRecordsUnavailableError) as caught:
            gateway.fetch_cultural_resource_detail("r1")

        self.assertIsInstance(caught.exception, PropertyRecordsBusyError)
        self.assertIsInstance(caught.exception, UpstreamBusyError)

    def test_the_wait_is_how_long_until_redatas_hold_ends(self) -> None:
        in_five_minutes = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        for retry_after in (in_five_minutes, 300, "300", 300.0):
            with self.subTest(retry_after=retry_after):
                gateway, _session = self._gateway(detail_unresolved_body(retry_after=retry_after))

                with self.assertRaises(PropertyRecordsBusyError) as caught:
                    gateway.fetch_cultural_resource_detail(f"r-{retry_after}")

                self.assertTrue(295 <= caught.exception.retry_after <= 300, caught.exception.retry_after)

    def test_a_hold_longer_than_a_busy_wait_may_be_is_bounded_like_one(self) -> None:
        a_week = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        gateway, _session = self._gateway(detail_unresolved_body(retry_after=a_week))

        with self.assertRaises(PropertyRecordsBusyError) as caught:
            gateway.fetch_cultural_resource_detail("r1")

        self.assertEqual(caught.exception.retry_after, UPSTREAM_BUSY_MAX_SECONDS)

    def test_a_missing_or_unreadable_wait_still_raises_with_a_bounded_one(self) -> None:
        for retry_after in ("", "someday", None):
            with self.subTest(retry_after=retry_after):
                body = detail_unresolved_body()
                body["retry_after"] = retry_after
                gateway, _session = self._gateway(body)

                with self.assertRaises(PropertyRecordsBusyError) as caught:
                    gateway.fetch_cultural_resource_detail(f"r-{retry_after}")

                self.assertEqual(caught.exception.retry_after, UPSTREAM_BUSY_MAX_SECONDS)

    def test_a_hold_that_has_already_ended_is_asked_again_at_once(self) -> None:
        gateway, _session = self._gateway(
            detail_unresolved_body(retry_after=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
        )

        with self.assertRaises(PropertyRecordsBusyError) as caught:
            gateway.fetch_cultural_resource_detail("r1")

        self.assertEqual(caught.exception.retry_after, 1)

    def test_it_is_not_shared_so_the_next_ask_goes_back_to_redata(self) -> None:
        resolved = {"detail_status": "fetched", "resource": {"uuid": "r1", "attachments": [{"id": 1, "kind": "photo"}]}}
        gateway, session = self._gateway(detail_unresolved_body(), detail_unresolved_body(), resolved)

        for _ in range(2):
            with self.assertRaises(PropertyRecordsBusyError):
                gateway.fetch_cultural_resource_detail("r1")
        detail = gateway.fetch_cultural_resource_detail("r1")

        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(detail["attachments"][0]["id"], 1)

    def test_a_resource_with_nothing_deeper_published_is_still_a_settled_answer(self) -> None:
        """Only ``unresolved`` is held: REData's other envelopes still return the resource."""
        for detail_status in ("fetched", "no_detail_published", "already_fetched", "not_supported"):
            with self.subTest(detail_status=detail_status):
                gateway, _session = self._gateway(
                    {"detail_status": detail_status, "resource": {"uuid": f"u-{detail_status}"}}
                )

                self.assertEqual(
                    gateway.fetch_cultural_resource_detail(f"r-{detail_status}"), {"uuid": f"u-{detail_status}"}
                )


class QueueCulturalResourceDetailsTests(SimpleTestCase):
    """The bulk ``fetch-details/`` endpoint queues every nearby resource's detail fetch (P24)."""

    def test_posts_the_coordinate_and_radius_to_the_bulk_endpoint(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(202, json_body={"queued": 38, "considered": 44})
        gateway = _gateway(session)
        gateway.queue_cultural_resource_details(41.73328, -73.92812, radius_meters=600)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/fetch-details/")
        self.assertEqual(kwargs["params"], {"lat": 41.73328, "lng": -73.92812, "radius_meters": 600})

    def test_returns_the_queue_counts(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(202, json_body={"queued": 38, "already_fetched": 4, "considered": 44})
        counts = _gateway(session).queue_cultural_resource_details(41.7, -73.9, radius_meters=300)
        self.assertEqual(counts["queued"], 38)
        self.assertEqual(counts["already_fetched"], 4)

    def test_a_read_only_key_raises_unavailable(self) -> None:
        """``fetch-details/`` needs ``cultural_resources:write``; a read-only key gets 403."""
        session = MagicMock()
        session.post.return_value = _response(403, json_body={"detail": "forbidden"})
        with self.assertRaises(PropertyRecordsUnavailableError):
            _gateway(session).queue_cultural_resource_details(41.7, -73.9, radius_meters=300)

    def test_an_unreachable_redata_raises_unavailable(self) -> None:
        session = MagicMock()
        session.post.side_effect = OSError("connection refused")
        with self.assertRaises(PropertyRecordsUnavailableError):
            _gateway(session).queue_cultural_resource_details(41.7, -73.9, radius_meters=300)


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
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/r1/attachments/5/download/")
        self.assertTrue(kwargs.get("stream"), "an unstreamed download cannot be size-capped")

    def test_404_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, json_body={"error": "attachment_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError):
            gateway.download_cultural_resource_attachment("r1", 5)


# -- lookup_buildings ---------------------------------------------------------------


class LookupParcelBuildingsTests(SimpleTestCase):
    def test_sources_redata_names_as_unanswered_are_reported(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(
            200, json_body=[{"source": "cris"}], headers={"X-REData-Unanswered-Sources": "overture, overpass"}
        )

        answer = _gateway(session).lookup_parcel_buildings("parcel-uuid")

        self.assertEqual(answer.buildings, [{"source": "cris"}])
        self.assertEqual(answer.unanswered_sources, ("overture", "overpass"))

    def test_an_answer_without_the_header_is_complete(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=[], headers={"Content-Type": "application/json"})

        self.assertEqual(_gateway(session).lookup_parcel_buildings("parcel-uuid").unanswered_sources, ())


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

    def test_a_document_not_downloaded_yet_is_downloaded_then_extracted(self) -> None:
        """REData extracts only a document it holds the file for, and fetches the file only when it is downloaded."""
        session = MagicMock()
        session.post.side_effect = [
            _response(400, json_body={"error": "not_extractable", "message": "download it first"}),
            _response(200, json_body={"id": 21, "extracted_images": [{"id": 3}]}),
        ]
        session.get.return_value = _response(200, content=b"%PDF", headers={"Content-Type": "application/pdf"})
        gateway = _gateway(session)

        result = gateway.extract_cultural_resource_attachment("r1", 21)

        self.assertEqual(result["extracted_images"], [{"id": 3}])
        args, _kwargs = session.get.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/cultural-resources/r1/attachments/21/download/")
        self.assertEqual(session.post.call_count, 2)

    def test_a_document_still_refused_once_downloaded_is_not_asked_again(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(400, json_body={"error": "not_extractable"})
        session.get.return_value = _response(200, content=b"%PDF")
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.extract_cultural_resource_attachment("r1", 22)
        self.assertEqual(ctx.exception.reason, "not_extractable")
        self.assertEqual((session.post.call_count, session.get.call_count), (2, 1))

    def test_a_document_that_cannot_be_downloaded_is_not_extracted(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(400, json_body={"error": "not_extractable"})
        session.get.return_value = _response(404, json_body={"error": "attachment_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.extract_cultural_resource_attachment("r1", 23)
        self.assertEqual(ctx.exception.reason, "attachment_unavailable")
        self.assertEqual(session.post.call_count, 1)

    def test_a_spent_budget_is_an_outage_and_is_asked_again(self) -> None:
        """Unlike a settled refusal, a budget refusal says nothing about the document, so the next ask goes back to REData."""
        from django.core.cache import cache

        for error in BUDGET_REFUSALS:
            with self.subTest(error):
                cache.clear()
                session = MagicMock()
                session.post.return_value = _response(503, json_body={"error": error})
                gateway = _gateway(session)

                for _ in range(2):
                    with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
                        gateway.extract_cultural_resource_attachment("r1", 32)
                    self.assertEqual(ctx.exception.reason, error)
                    self.assertTrue(ctx.exception.is_outage)

                self.assertEqual(session.post.call_count, 2)

    def test_a_settled_refusal_is_not_asked_again_for_a_while(self) -> None:
        """REData re-runs OCR and an AI model on every ask; dev asked one empty form twelve times."""
        from django.core.cache import cache

        for status, error in ((503, "extraction_unavailable"), (400, "not_extractable")):
            with self.subTest(error=error):
                cache.clear()
                session = MagicMock()
                session.post.return_value = _response(status, json_body={"error": error})
                session.get.return_value = _response(200, content=b"%PDF")
                gateway = _gateway(session)

                def ask(gateway: RedataGateway = gateway) -> str:
                    with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
                        gateway.extract_cultural_resource_attachment("r1", 31)
                    return ctx.exception.reason

                self.assertEqual(ask(), error)
                first = session.post.call_count
                self.assertEqual(ask(), error)
                self.assertEqual(session.post.call_count, first, "asked again within the backoff")

                cache.clear()
                ask()
                self.assertEqual(session.post.call_count, 2 * first, "not asked again once the backoff is gone")

    def test_a_timeout_is_asked_again(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(504, text="<html>gateway timeout</html>")
        gateway = _gateway(session)
        for _ in range(2):
            with self.assertRaises(PropertyRecordsUnavailableError):
                gateway.extract_cultural_resource_attachment("r1", 32)

        self.assertEqual(session.post.call_count, 2)

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
        from django.core.cache import cache

        for error in BUDGET_REFUSALS:
            with self.subTest(error):
                cache.clear()
                session = MagicMock()
                session.get.return_value = _response(503, json_body={"error": error})
                gateway = _gateway(session)
                with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
                    gateway.lookup_demographics("parcel-uuid")
                self.assertEqual(ctx.exception.reason, error)
                self.assertTrue(
                    ctx.exception.is_outage, "a spent budget says nothing about the parcel, so nothing is cached"
                )

    def test_503_census_data_api_unavailable_raises_unavailable(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(503, json_body={"error": "census_data_api_unavailable"})
        gateway = _gateway(session)
        with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            gateway.lookup_demographics("parcel-uuid")
        self.assertEqual(ctx.exception.reason, "census_data_api_unavailable")


def _demographics_body(**fields: object) -> dict:
    return {"demographics": {"uuid": "d1", "population": 4210, "geography_level": "census_tract", **fields}}


class DemographicsSharedPerParcelTests(SimpleTestCase):
    """Every location on one parcel asks REData for the same parcel's demographics.

    In 0.8.0 each location made its own call: 24 locations on one parcel were 24 calls in a cycle, whatever REData
    answered. The answer is the parcel's, so one call answers all of them.
    """

    _PARCEL = "parcel-uuid"

    def _shared_for(self, body: dict) -> int:
        """Seconds the answer to ``body`` is shared for."""
        from django.core.cache import cache

        cache.clear()
        session = MagicMock()
        session.get.return_value = _response(200, json_body=body)
        with mock.patch.object(cache, "set", wraps=cache.set) as stored:
            _gateway(session).lookup_demographics(self._PARCEL)
        timeouts = [
            call.args[2]
            for call in stored.call_args_list
            if self._PARCEL in call.args[0] and "flight" not in call.args[0]
        ]
        self.assertEqual(len(timeouts), 1, stored.call_args_list)
        return timeouts[0]

    def test_one_parcel_is_asked_once_for_every_location_on_it(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=_demographics_body())

        answers = [_gateway(session).lookup_demographics(self._PARCEL) for _ in range(24)]

        self.assertEqual(session.get.call_count, 1)
        self.assertEqual({answer["population"] for answer in answers if answer}, {4210})

    def test_another_parcel_is_its_own_question(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, json_body=_demographics_body())

        _gateway(session).lookup_demographics("parcel-a")
        _gateway(session).lookup_demographics("parcel-b")

        self.assertEqual(session.get.call_count, 2)

    def test_an_answer_is_shared_as_long_as_the_parcel_lookup_is(self) -> None:
        from urbanlens.dashboard.services.apis.property_records import redata_gateway

        self.assertEqual(self._shared_for(_demographics_body()), redata_gateway._PARCEL_LOOKUP_SHARE_SECONDS)
        self.assertEqual(
            self._shared_for({"demographics": None}),
            redata_gateway._PARCEL_LOOKUP_SHARE_SECONDS,
            "no coordinate, or outside the USA, is settled",
        )

    def test_an_older_redata_answer_without_a_level_is_shared_as_long(self) -> None:
        from urbanlens.dashboard.services.apis.property_records import redata_gateway

        body = {"demographics": {"uuid": "d1", "population": 4210}}

        self.assertEqual(self._shared_for(body), redata_gateway._PARCEL_LOOKUP_SHARE_SECONDS)

    def test_a_partial_or_coarser_answer_is_shared_briefly(self) -> None:
        """REData answers a tract question at county level when it cannot resolve the tract, such as before its tract layer syncs."""
        from urbanlens.dashboard.services.apis.property_records import redata_gateway

        for body in (
            _demographics_body(geography_level="county"),
            {**_demographics_body(), "complete": False},
            {**_demographics_body(), "degraded": True},
        ):
            with self.subTest(body):
                self.assertEqual(self._shared_for(body), redata_gateway._PARTIAL_PARCEL_LOOKUP_SHARE_SECONDS)

    def test_a_failure_is_remembered_briefly_and_raised_again_alike(self) -> None:
        """The next location on the parcel learns what the first did, without asking: an outage stays an outage."""
        from django.core.cache import cache

        failures = (
            _response(503, json_body={"error": "census_data_api_unavailable"}),
            _response(503, json_body={"error": "census_data_api_not_configured"}),
            _response(503, json_body={"error": "rate_limited"}, headers={"Retry-After": "120"}),
            # REData older than the endpoint answers its route's 404.
            _response(404, text="<html>Not Found</html>", raise_on_json=True),
        )
        for response in failures:
            with self.subTest(response.status_code, body=response.json.return_value):
                cache.clear()
                session = MagicMock()
                session.get.return_value = response
                raised = []
                for _ in range(3):
                    with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
                        _gateway(session).lookup_demographics(self._PARCEL)
                    raised.append(ctx.exception)

                self.assertEqual(session.get.call_count, 1)
                first, *rest = raised
                for again in rest:
                    self.assertIs(type(again), type(first))
                    self.assertEqual(
                        (again.reason, again.is_outage, again.retry_later, again.status_code, str(again)),
                        (first.reason, first.is_outage, first.retry_later, first.status_code, str(first)),
                    )
                    self.assertEqual(getattr(again, "retry_after", None), getattr(first, "retry_after", None))

    def test_a_failure_is_asked_about_again_once_its_memory_lapses(self) -> None:
        from django.core.cache import cache

        from urbanlens.dashboard.services.apis.property_records import redata_gateway

        session = MagicMock()
        session.get.return_value = _response(503, json_body={"error": "census_data_api_unavailable"})
        with (
            mock.patch.object(cache, "set", wraps=cache.set) as stored,
            self.assertRaises(PropertyRecordsUnavailableError),
        ):
            _gateway(session).lookup_demographics(self._PARCEL)
        remembered = [
            call.args[2]
            for call in stored.call_args_list
            if self._PARCEL in call.args[0] and "flight" not in call.args[0]
        ]
        self.assertEqual(remembered, [redata_gateway._PARTIAL_PARCEL_LOOKUP_SHARE_SECONDS])

        cache.clear()
        session.get.return_value = _response(200, json_body=_demographics_body())
        self.assertEqual((_gateway(session).lookup_demographics(self._PARCEL) or {}).get("population"), 4210)
        self.assertEqual(session.get.call_count, 2)

    def test_a_longer_wait_redata_named_is_kept(self) -> None:
        from django.core.cache import cache

        session = MagicMock()
        session.get.return_value = _response(503, json_body={"error": "rate_limited"}, headers={"Retry-After": "720"})
        with (
            mock.patch.object(cache, "set", wraps=cache.set) as stored,
            self.assertRaises(PropertyRecordsUnavailableError),
        ):
            _gateway(session).lookup_demographics(self._PARCEL)
        remembered = [
            call.args[2]
            for call in stored.call_args_list
            if self._PARCEL in call.args[0] and "flight" not in call.args[0]
        ]
        self.assertEqual(remembered, [720])


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
