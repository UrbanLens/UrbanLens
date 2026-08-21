"""Tests for RedataPhotosGateway, the REST client for REData's photo-relevance endpoints.

All HTTP calls are mocked so no real network access occurs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError


def _response(status_code: int, *, json_body: dict | None = None, text: str = "", raise_on_json: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if raise_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _gateway(session: MagicMock | None = None) -> RedataPhotosGateway:
    return RedataPhotosGateway(base_url="https://redata.example.test", api_key="test-key", session=session or MagicMock())


class ConstructionTests(SimpleTestCase):
    def test_missing_base_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            RedataPhotosGateway(base_url=None, api_key="test-key", session=MagicMock())

    def test_missing_api_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            RedataPhotosGateway(base_url="https://redata.example.test", api_key=None, session=MagicMock())

    def test_scheme_is_added_when_missing(self) -> None:
        gateway = RedataPhotosGateway(base_url="redata.example.test", api_key="test-key", session=MagicMock())
        self.assertEqual(gateway.base_url, "https://redata.example.test")


# These exercise the production send path. Off production the write is skipped by design
# (see services.core.environment), and the test environment is not production - so the
# send path has to be opted into explicitly here.
@override_settings(IS_PRODUCTION=True)
class SubmitPhotosTests(SimpleTestCase):
    def test_empty_list_short_circuits_without_a_request(self) -> None:
        session = MagicMock()
        gateway = _gateway(session)
        result = gateway.submit_photos([])
        session.post.assert_not_called()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], {})

    def test_posts_to_the_photos_endpoint_with_bearer_auth(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"count": 1, "results": {}, "unknown": []})
        gateway = _gateway(session)
        gateway.submit_photos([{"photo_id": "abc", "location_latitude": 1.0, "location_longitude": 2.0}])
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/photos/")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["photos"][0]["photo_id"], "abc")

    def test_parses_confidence_results(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"count": 1, "results": {"abc": {"confidence": 0.87, "scorer": "model", "model_version": 7}}, "unknown": []})
        gateway = _gateway(session)
        result = gateway.submit_photos([{"photo_id": "abc", "location_latitude": 1.0, "location_longitude": 2.0}])
        self.assertEqual(result["results"]["abc"]["confidence"], 0.87)

    def test_non_2xx_status_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(500, text="server error")
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.submit_photos([{"photo_id": "abc"}])

    def test_unparseable_json_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, raise_on_json=True)
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.submit_photos([{"photo_id": "abc"}])

    def test_network_failure_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.side_effect = ConnectionError("unreachable")
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.submit_photos([{"photo_id": "abc"}])


# These exercise the production send path. Off production the write is skipped by design
# (see services.core.environment), and the test environment is not production - so the
# send path has to be opted into explicitly here.
@override_settings(IS_PRODUCTION=True)
class SubmitVotesTests(SimpleTestCase):
    def test_empty_list_short_circuits_without_a_request(self) -> None:
        session = MagicMock()
        gateway = _gateway(session)
        result = gateway.submit_votes([])
        session.post.assert_not_called()
        self.assertEqual(result["recorded"], 0)

    def test_posts_to_the_votes_endpoint(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"recorded": 1, "unknown_photo_ids": [], "updated_photos": 1})
        gateway = _gateway(session)
        result = gateway.submit_votes([{"photo_id": "abc", "is_relevant": True, "voter_id": "5"}])
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/photos/votes/")
        self.assertEqual(kwargs["json"]["votes"][0]["photo_id"], "abc")
        self.assertEqual(result["recorded"], 1)

    def test_reports_unknown_photo_ids(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"recorded": 0, "unknown_photo_ids": ["abc"], "updated_photos": 0})
        gateway = _gateway(session)
        result = gateway.submit_votes([{"photo_id": "abc", "is_relevant": True}])
        self.assertEqual(result["unknown_photo_ids"], ["abc"])


class GetConfidenceBatchTests(SimpleTestCase):
    def test_empty_list_short_circuits_without_a_request(self) -> None:
        session = MagicMock()
        gateway = _gateway(session)
        result = gateway.get_confidence_batch([])
        session.post.assert_not_called()
        self.assertEqual(result["results"], {})

    def test_posts_to_the_confidence_endpoint(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"count": 1, "results": {"abc": {"confidence": 0.5}}, "unknown": []})
        gateway = _gateway(session)
        result = gateway.get_confidence_batch(["abc"])
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/photos/confidence/")
        self.assertEqual(kwargs["json"]["photo_ids"], ["abc"])
        self.assertEqual(result["results"]["abc"]["confidence"], 0.5)
