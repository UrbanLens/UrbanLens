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


def _response(status_code: int, *, json_body: dict | None = None, text: str = "", raise_on_json: bool = False) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if raise_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_body or {}
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
