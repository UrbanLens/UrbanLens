"""Tests for RedataPointsOfInterestGateway - REData's shared ``/points-of-interest/lookup/``
near-a-coordinate search backing both the Yelp and EPA ECHO plugins.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mock ``session``
(``Gateway.__post_init__`` leaves a non-default session untouched, skipping
the DB-backed rate-limiting wrapper), no database access.
"""

from __future__ import annotations

from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway import RedataPointsOfInterestGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataPointsOfInterestGateway:
    return RedataPointsOfInterestGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class FindNearTests(SimpleTestCase):
    def test_hits_the_points_of_interest_lookup_path(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).find_near(40.0, -74.0, provider="yelp")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/points-of-interest/lookup/")

    def test_forwards_the_provider_restriction(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).find_near(40.0, -74.0, provider="epa_echo")

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["provider"], "epa_echo")

    def test_returns_the_envelopes_results(self) -> None:
        session = mock.Mock()
        rows = [{"provider": "yelp", "name": "Joe's Diner", "attributes": {"rating": 4.5}}]
        session.get.return_value = _response(200, {"count": 1, "complete": True, "results": rows, "providers": [{"provider": "yelp", "status": "ok", "count": 1, "message": None, "radius_meters": 500.0}]})

        result = _gateway(session).find_near(40.0, -74.0, provider="yelp")

        self.assertEqual(result, rows)

    def test_force_refresh_is_forwarded(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).find_near(40.0, -74.0, provider="yelp", force_refresh=True)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["force_refresh"], "true")

    def test_a_provider_scoped_rate_limit_raises(self) -> None:
        """Restricting to one provider still surfaces that provider's own rate limit as a failure."""
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "rate_limited", "message": "back off"})

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).find_near(40.0, -74.0, provider="epa_echo")

    def test_empty_results_is_a_real_answer_not_an_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": [{"provider": "yelp", "status": "ok", "count": 0, "message": None, "radius_meters": 500.0}]})

        result = _gateway(session).find_near(40.0, -74.0, provider="yelp")

        self.assertEqual(result, [])
