"""Tests for CensusTigerwebGateway.get_state_boundary - the state-polygon attribute query
backing services.geo_boundary.state_boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.census_tigerweb import CensusTigerwebGateway


def _json_response(payload: dict) -> MagicMock:
    """Build a mock requests.Response whose .json() returns *payload*."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _gateway() -> CensusTigerwebGateway:
    return CensusTigerwebGateway(session=MagicMock())


class GetStateBoundaryTests(SimpleTestCase):
    def test_returns_geometry_of_matching_feature(self) -> None:
        gw = _gateway()
        geometry = {"rings": [[[-80.0, 40.0], [-80.0, 45.0], [-70.0, 45.0], [-70.0, 40.0], [-80.0, 40.0]]]}
        gw.session.get.return_value = _json_response({"features": [{"geometry": geometry, "attributes": {"STUSPS": "NY"}}]})

        self.assertEqual(gw.get_state_boundary("ny"), geometry)

    def test_returns_none_when_no_features(self) -> None:
        gw = _gateway()
        gw.session.get.return_value = _json_response({"features": []})

        self.assertIsNone(gw.get_state_boundary("ZZ"))

    def test_returns_none_on_request_failure(self) -> None:
        gw = _gateway()
        gw.session.get.side_effect = requests.exceptions.ConnectionError("network down")

        self.assertIsNone(gw.get_state_boundary("NY"))

    def test_issues_an_uppercased_attribute_query(self) -> None:
        gw = _gateway()
        gw.session.get.return_value = _json_response({"features": []})

        gw.get_state_boundary("ny")

        _args, kwargs = gw.session.get.call_args
        params = kwargs["params"]
        self.assertEqual(params["where"], "STUSPS='NY'")
        self.assertEqual(params["returnGeometry"], "true")
        self.assertEqual(params["outSR"], 4326)

    def test_rejects_non_two_letter_abbreviations(self) -> None:
        gw = _gateway()
        with pytest.raises(ValueError, match="two-letter"):
            gw.get_state_boundary("New York")
        gw.session.get.assert_not_called()

    def test_rejects_abbreviations_with_non_letters(self) -> None:
        gw = _gateway()
        with pytest.raises(ValueError, match="two-letter"):
            gw.get_state_boundary("N1")
        gw.session.get.assert_not_called()
