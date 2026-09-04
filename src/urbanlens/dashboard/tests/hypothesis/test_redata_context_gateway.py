"""Tests for RedataLocationContextGateway against REData's shared near-a-coordinate
contract (``../REData/docs/api-reference.md``, "Near-a-coordinate endpoints").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

from unittest import mock

from django.test import override_settings
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    REASON_ALL_PROVIDERS_UNAVAILABLE,
    REASON_RATE_LIMITED,
    REASON_SOURCE_ERROR,
    LocationContextUnavailableError,
    RedataLocationContextGateway,
    redata_configured,
)
from urbanlens.dashboard.services.core.gateway import GatewayRequestError


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataLocationContextGateway:
    return RedataLocationContextGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class ConstructionTests(SimpleTestCase):
    def test_missing_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="UL_REDATA_API_URL"):
            RedataLocationContextGateway(base_url=None, api_key="test-key", session=mock.Mock())

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ValueError, match="UL_REDATA_API_KEY"):
            RedataLocationContextGateway(base_url="https://redata.example.test", api_key=None, session=mock.Mock())

    def test_bare_host_gets_https_scheme(self) -> None:
        gateway = RedataLocationContextGateway(base_url="redata.example.test", api_key="test-key", session=mock.Mock())
        self.assertEqual(gateway.base_url, "https://redata.example.test")


class RedataConfiguredTests(SimpleTestCase):
    def test_false_when_either_setting_missing(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_context_gateway.settings") as mock_settings:
            mock_settings.redata_api_url = None
            mock_settings.redata_api_key = "key"
            self.assertFalse(redata_configured())

    def test_true_when_both_set(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_context_gateway.settings") as mock_settings:
            mock_settings.redata_api_url = "https://redata.example.test"
            mock_settings.redata_api_key = "key"
            self.assertTrue(redata_configured())

    @override_settings(UL_PROCESS_ROLE="ai")
    def test_false_under_the_ai_role_even_when_both_set(self) -> None:
        # The assistant's tool loop must never reach REData, regardless of
        # whether ai-worker's environment happens to carry the credentials.
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_context_gateway.settings") as mock_settings:
            mock_settings.redata_api_url = "https://redata.example.test"
            mock_settings.redata_api_key = "key"
            self.assertFalse(redata_configured())

    @override_settings(UL_PROCESS_ROLE="worker")
    def test_true_under_a_non_ai_role_when_both_set(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_context_gateway.settings") as mock_settings:
            mock_settings.redata_api_url = "https://redata.example.test"
            mock_settings.redata_api_key = "key"
            self.assertTrue(redata_configured())


class NearPointTests(SimpleTestCase):
    def test_200_parses_the_full_envelope(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [{"provider": "usgs_earthquakes", "magnitude": 3.2}],
                "providers": [
                    {
                        "provider": "usgs_earthquakes",
                        "status": "ok",
                        "count": 1,
                        "message": None,
                        "radius_meters": 100.0,
                    }
                ],
            },
        )

        envelope = _gateway(session).near_point(
            "/api/v1/hazards/", 38.456, -77.123, radius_meters=100_000, provider="usgs_earthquakes"
        )

        self.assertEqual(envelope.count, 1)
        self.assertTrue(envelope.complete)
        self.assertEqual(envelope.results, [{"provider": "usgs_earthquakes", "magnitude": 3.2}])
        self.assertEqual(envelope.providers[0]["provider"], "usgs_earthquakes")
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/hazards/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 38.456)
        self.assertEqual(params["lng"], -77.123)
        self.assertEqual(params["radius_meters"], 100_000)
        self.assertEqual(params["provider"], "usgs_earthquakes")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_defaults_omit_optional_params(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).near_point("/api/v1/elevation/", 1.0, 2.0)

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("radius_meters", params)
        self.assertNotIn("provider", params)
        self.assertNotIn("force_refresh", params)
        self.assertNotIn("limit", params)

    def test_force_refresh_and_extra_params_are_sent(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).near_point(
            "/api/v1/hazards/", 1.0, 2.0, force_refresh=True, limit=10, extra_params={"min_magnitude": 3.0, "years": 10}
        )

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["force_refresh"], "true")
        self.assertEqual(params["limit"], 10)
        self.assertEqual(params["min_magnitude"], 3.0)
        self.assertEqual(params["years"], 10)

    def test_missing_envelope_fields_default_empty(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {})

        envelope = _gateway(session).near_point("/api/v1/elevation/", 1.0, 2.0)

        self.assertEqual(envelope.count, 0)
        self.assertTrue(envelope.complete)
        self.assertEqual(envelope.results, [])
        self.assertEqual(envelope.providers, [])

    def test_non_dict_body_raises(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [1, 2, 3])

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).near_point("/api/v1/elevation/", 1.0, 2.0)

    def test_unparseable_200_body_raises(self) -> None:
        session = mock.Mock()
        resp = mock.Mock(status_code=200, text="")
        resp.json.side_effect = ValueError("bad json")
        session.get.return_value = resp

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).near_point("/api/v1/elevation/", 1.0, 2.0)

    def test_503_all_providers_unavailable_carries_reason(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            503, {"error": REASON_ALL_PROVIDERS_UNAVAILABLE, "message": "every source failed"}
        )

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).near_point("/api/v1/hazards/", 1.0, 2.0)
        self.assertEqual(ctx.value.reason, REASON_ALL_PROVIDERS_UNAVAILABLE)

    def test_503_rate_limited_carries_reason(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": REASON_RATE_LIMITED, "message": "back off"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).near_point("/api/v1/hazards/", 1.0, 2.0)
        self.assertEqual(ctx.value.reason, REASON_RATE_LIMITED)

    def test_400_carries_redata_error_code(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(400, {"error": "invalid_radius", "message": "radius_km is not a thing"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).near_point("/api/v1/hazards/", 1.0, 2.0)
        self.assertEqual(ctx.value.reason, "invalid_radius")

    def test_network_error_raises_source_error(self) -> None:
        session = mock.Mock()
        session.get.side_effect = OSError("connection refused")

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).near_point("/api/v1/hazards/", 1.0, 2.0)
        self.assertEqual(ctx.value.reason, REASON_SOURCE_ERROR)

    def test_unexpected_status_code_raises_source_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(500, {})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).near_point("/api/v1/hazards/", 1.0, 2.0)
        self.assertEqual(ctx.value.reason, REASON_SOURCE_ERROR)

    def test_is_a_gateway_request_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": REASON_RATE_LIMITED})

        with pytest.raises(GatewayRequestError):
            _gateway(session).near_point("/api/v1/hazards/", 1.0, 2.0)


class GetJsonTests(SimpleTestCase):
    def test_200_returns_raw_body(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 2, "results": [{"title": "a"}, {"title": "b"}]})

        body = _gateway(session).get_json("/api/v1/reference-documents/search/", params={"q": "lighthouse"})

        self.assertEqual(body, {"count": 2, "results": [{"title": "a"}, {"title": "b"}]})
        self.assertEqual(session.get.call_args.kwargs["params"], {"q": "lighthouse"})

    def test_omits_params_when_none(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).get_json("/api/v1/search/web/")

        self.assertEqual(session.get.call_args.kwargs["params"], {})

    def test_non_200_raises(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "all_providers_unavailable"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).get_json("/api/v1/search/web/")
        self.assertEqual(ctx.value.reason, REASON_ALL_PROVIDERS_UNAVAILABLE)


class PostJsonTests(SimpleTestCase):
    def test_200_returns_raw_body(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"route": {"distance_meters": 100.0}})

        body = _gateway(session).post_json("/api/v1/routes/", {"waypoints": [[1.0, 2.0], [3.0, 4.0]]})

        self.assertEqual(body, {"route": {"distance_meters": 100.0}})
        self.assertEqual(session.post.call_args.kwargs["json"], {"waypoints": [[1.0, 2.0], [3.0, 4.0]]})
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_non_200_raises(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(503, {"error": REASON_RATE_LIMITED})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).post_json("/api/v1/routes/", {"waypoints": []})
        self.assertEqual(ctx.value.reason, REASON_RATE_LIMITED)

    def test_network_error_raises_source_error(self) -> None:
        session = mock.Mock()
        session.post.side_effect = OSError("connection refused")

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).post_json("/api/v1/routes/", {"waypoints": []})
        self.assertEqual(ctx.value.reason, REASON_SOURCE_ERROR)
