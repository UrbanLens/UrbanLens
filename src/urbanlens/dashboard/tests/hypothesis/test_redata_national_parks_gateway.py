"""Tests for RedataNationalParksGateway - REData's ``/parks/nearby/`` local NPS catalog lookup.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mock ``session``
(``Gateway.__post_init__`` leaves a non-default session untouched, skipping
the DB-backed rate-limiting wrapper), no database access.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataNationalParksGateway:
    return RedataNationalParksGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class FindParksNearTests(SimpleTestCase):
    def test_hits_the_parks_nearby_path(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_parks_near(44.6, -110.5)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/parks/nearby/")

    def test_returns_the_envelopes_results_nearest_first(self) -> None:
        session = mock.Mock()
        units = [{"park_code": "yell", "full_name": "Yellowstone National Park"}]
        session.get.return_value = _response(200, {"count": 1, "complete": True, "results": units})

        result = _gateway(session).find_parks_near(44.6, -110.5)

        self.assertEqual(result, units)

    def test_no_providers_block_is_not_required(self) -> None:
        """This endpoint has no ``providers`` block (pure local-catalog read) -
        the envelope parser must not choke on its absence."""
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        result = _gateway(session).find_parks_near(44.6, -110.5)

        self.assertEqual(result, [])

    def test_forwards_radius_and_limit(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_parks_near(44.6, -110.5, radius_meters=250_000, limit=5)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["radius_meters"], 250_000)
        self.assertEqual(params["limit"], 5)
        self.assertNotIn("provider", params)

    def test_omitting_radius_lets_redata_use_its_own_default(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_parks_near(44.6, -110.5)

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("radius_meters", params)


class FindNearestParkTests(SimpleTestCase):
    def test_returns_the_first_result(self) -> None:
        session = mock.Mock()
        units = [
            {"park_code": "yell", "full_name": "Yellowstone National Park"},
            {"park_code": "grte", "full_name": "Grand Teton National Park"},
        ]
        session.get.return_value = _response(200, {"count": 2, "complete": True, "results": units})

        result = _gateway(session).find_nearest_park(44.6, -110.5)

        self.assertEqual(result, units[0])

    def test_returns_none_when_nothing_is_within_range(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        result = _gateway(session).find_nearest_park(44.6, -110.5)

        self.assertIsNone(result)

    def test_requests_a_limit_of_one(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).find_nearest_park(44.6, -110.5)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["limit"], 1)


class GetAlertsTests(SimpleTestCase):
    def test_hits_the_alerts_path_for_the_given_park_code(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).get_alerts("yell")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/parks/yell/alerts/")

    def test_returns_the_raw_list(self) -> None:
        session = mock.Mock()
        alerts = [
            {"id": 1, "title": "Bridge closed", "category": "Park Closure", "url": "https://nps.gov/yell/alert1"},
            {"id": 2, "title": "Bear activity", "category": "Caution", "url": "https://nps.gov/yell/alert2"},
        ]
        session.get.return_value = _response(200, alerts)

        result = _gateway(session).get_alerts("yell")

        self.assertEqual(result, alerts)

    def test_an_empty_array_is_a_clean_nothing_published_not_an_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        result = _gateway(session).get_alerts("yell")

        self.assertEqual(result, [])

    def test_an_unexpected_shape_degrades_to_an_empty_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"not": "a list"})

        result = _gateway(session).get_alerts("yell")

        self.assertEqual(result, [])

    def test_a_404_propagates_as_unavailable(self) -> None:
        """The park code was already resolved via ``/parks/nearby/`` - a 404 here means
        something unexpected happened, not "no alerts"."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        session = mock.Mock()
        response = _response(404, {})
        response.text = "not found"
        session.get.return_value = response

        with self.assertRaises(LocationContextUnavailableError):
            _gateway(session).get_alerts("bogus")

    def test_park_code_is_url_encoded(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).get_alerts("a/b c")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/parks/a%2Fb%20c/alerts/")


class GetVisitorCentersTests(SimpleTestCase):
    def test_hits_the_visitor_centers_path_for_the_given_park_code(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).get_visitor_centers("yell")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/parks/yell/visitor-centers/")

    def test_returns_the_raw_list(self) -> None:
        session = mock.Mock()
        centers = [{"id": 1, "name": "Old Faithful Visitor Center"}]
        session.get.return_value = _response(200, centers)

        result = _gateway(session).get_visitor_centers("yell")

        self.assertEqual(result, centers)

    def test_an_empty_array_is_a_clean_nothing_published_not_an_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        result = _gateway(session).get_visitor_centers("yell")

        self.assertEqual(result, [])


class GetCampgroundsTests(SimpleTestCase):
    def test_hits_the_campgrounds_path_for_the_given_park_code(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).get_campgrounds("yell")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/parks/yell/campgrounds/")

    def test_returns_the_raw_list(self) -> None:
        session = mock.Mock()
        campgrounds = [{"id": 1, "name": "Madison Campground", "number_of_sites_reservable": 278}]
        session.get.return_value = _response(200, campgrounds)

        result = _gateway(session).get_campgrounds("yell")

        self.assertEqual(result, campgrounds)

    def test_an_empty_array_is_a_clean_nothing_published_not_an_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        result = _gateway(session).get_campgrounds("yell")

        self.assertEqual(result, [])
