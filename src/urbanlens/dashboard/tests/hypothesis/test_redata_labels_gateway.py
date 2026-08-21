"""Tests for RedataLabelsGateway, the REST client for REData's label-suggestion endpoints.

All HTTP calls are mocked so no real network access occurs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import RedataLabelsGateway
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


def _gateway(session: MagicMock | None = None) -> RedataLabelsGateway:
    return RedataLabelsGateway(base_url="https://redata.example.test", api_key="test-key", session=session or MagicMock())


class ConstructionTests(SimpleTestCase):
    def test_missing_base_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            RedataLabelsGateway(base_url=None, api_key="test-key", session=MagicMock())

    def test_missing_api_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            RedataLabelsGateway(base_url="https://redata.example.test", api_key=None, session=MagicMock())

    def test_scheme_is_added_when_missing(self) -> None:
        gateway = RedataLabelsGateway(base_url="redata.example.test", api_key="test-key", session=MagicMock())
        self.assertEqual(gateway.base_url, "https://redata.example.test")


# These exercise the production send path. Off production the write is skipped by design
# (see services.core.environment), and the test environment is not production - so the
# send path has to be opted into explicitly here.
@override_settings(IS_PRODUCTION=True)
class DefineLabelsTests(SimpleTestCase):
    def test_empty_list_short_circuits_without_a_request(self) -> None:
        session = MagicMock()
        gateway = _gateway(session)
        result = gateway.define_labels("user-1", [])
        session.post.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["unknown_parents"], {})

    def test_posts_to_the_labels_endpoint_with_bearer_auth(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"created": 1, "updated": 0, "unknown_parents": {}, "rejected_edges": [], "implied_created": 0, "implied_removed": 0, "statistics_deferred": False})
        gateway = _gateway(session)
        gateway.define_labels("user-1", [{"external_id": "abc", "name": "Church"}])
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/labels/")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["user_id"], "user-1")
        self.assertEqual(kwargs["json"]["labels"][0]["external_id"], "abc")

    def test_non_2xx_status_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(500, text="server error")
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.define_labels("user-1", [{"external_id": "abc", "name": "Church"}])

    def test_network_failure_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.side_effect = ConnectionError("unreachable")
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.define_labels("user-1", [{"external_id": "abc", "name": "Church"}])

    def test_unparseable_json_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, raise_on_json=True)
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.define_labels("user-1", [{"external_id": "abc", "name": "Church"}])


# These exercise the production send path. Off production the write is skipped by design
# (see services.core.environment), and the test environment is not production - so the
# send path has to be opted into explicitly here.
@override_settings(IS_PRODUCTION=True)
class SyncAssignmentsTests(SimpleTestCase):
    def test_empty_list_short_circuits_without_a_request(self) -> None:
        session = MagicMock()
        gateway = _gateway(session)
        result = gateway.sync_assignments("user-1", [])
        session.post.assert_not_called()
        self.assertEqual(result["assignments_added"], 0)

    def test_posts_to_the_assignments_endpoint(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"locations_created": 1, "locations_updated": 0, "assignments_added": 2, "assignments_removed": 0, "implied_created": 0, "implied_removed": 0, "unknown_label_ids": [], "statistics_deferred": False})
        gateway = _gateway(session)
        result = gateway.sync_assignments("user-1", [{"external_id": "pin-1", "latitude": 1.0, "longitude": 2.0, "label_ids": ["abc"], "replace": True}])
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/labels/assignments/")
        self.assertEqual(kwargs["json"]["locations"][0]["external_id"], "pin-1")
        self.assertEqual(result["assignments_added"], 2)

    def test_reports_unknown_label_ids(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"locations_created": 0, "locations_updated": 1, "assignments_added": 0, "assignments_removed": 0, "implied_created": 0, "implied_removed": 0, "unknown_label_ids": ["ghost"], "statistics_deferred": False})
        gateway = _gateway(session)
        result = gateway.sync_assignments("user-1", [{"external_id": "pin-1", "latitude": 1.0, "longitude": 2.0, "label_ids": ["ghost"], "replace": True}])
        self.assertEqual(result["unknown_label_ids"], ["ghost"])


class SuggestLabelsTests(SimpleTestCase):
    def test_posts_to_the_suggest_endpoint_with_required_fields_only(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"count": 0, "results": [], "implied": [], "ranker": "heuristic", "model_version": None, "scored_candidates": 0, "total_candidates": 0, "statistics_stale": False})
        gateway = _gateway(session)
        gateway.suggest_labels("user-1", 1.0, 2.0)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://redata.example.test/api/v1/labels/suggest/")
        body = kwargs["json"]
        self.assertEqual(body, {"user_id": "user-1", "latitude": 1.0, "longitude": 2.0})

    def test_includes_optional_fields_when_given(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"count": 0, "results": [], "implied": [], "ranker": "heuristic", "model_version": None, "scored_candidates": 0, "total_candidates": 0, "statistics_stale": False})
        gateway = _gateway(session)
        gateway.suggest_labels("user-1", 1.0, 2.0, names=["Old Mill"], applied_label_ids=["abc"], limit=5, min_confidence=0.3)
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["names"], ["Old Mill"])
        self.assertEqual(body["applied_label_ids"], ["abc"])
        self.assertEqual(body["limit"], 5)
        self.assertEqual(body["min_confidence"], 0.3)

    def test_parses_results(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(200, json_body={"count": 1, "results": [{"label_id": "abc", "name": "Lighthouse", "confidence": 0.82, "canonical_key": "lighthouse"}], "implied": [], "ranker": "heuristic", "model_version": None, "scored_candidates": 1, "total_candidates": 1, "statistics_stale": False})
        gateway = _gateway(session)
        result = gateway.suggest_labels("user-1", 1.0, 2.0)
        self.assertEqual(result["results"][0]["confidence"], 0.82)

    def test_non_2xx_status_raises_gateway_error(self) -> None:
        session = MagicMock()
        session.post.return_value = _response(500, text="server error")
        gateway = _gateway(session)
        with self.assertRaises(GatewayRequestError):
            gateway.suggest_labels("user-1", 1.0, 2.0)
