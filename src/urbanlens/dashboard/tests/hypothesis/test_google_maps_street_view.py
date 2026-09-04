"""Tests for GoogleMapsGateway.get_street_view_single - Street View "no imagery" placeholder leak.

See docs/designs/drafts/spotguessr.md ("Street View mode") and
services.spotguessr.street_view for how this feeds SpotGuessr's Street View
mode. A custom (mock) `session` is passed directly to the gateway - Gateway
.__post_init__ preserves any non-plain-requests.Session as-is, which is this
codebase's established test-injection pattern (see services/gateway.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

_REAL_IMAGE_BYTES = b"x" * 5000
_PLACEHOLDER_IMAGE_BYTES = b"x" * 500


def _response(json_data=None, content=b""):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    if json_data is not None:
        response.json.return_value = json_data
    response.content = content
    return response


class GetStreetViewSingleTests(SimpleTestCase):
    def _gateway(self, session) -> GoogleMapsGateway:
        return GoogleMapsGateway(api_key="test-key", session=session)

    def test_the_image_request_keeps_the_radius_metadata_found_the_pano_at(self) -> None:
        """Regression guard: dropping `radius` from the image request lets it
        re-search with Google's own smaller default and miss a pano that
        metadata only found by expanding the search radius - silently
        returning the "Sorry, we have no imagery here" placeholder instead
        of the real photo."""
        session = MagicMock()
        session.get.side_effect = [
            _response(json_data={"status": "OK", "location": {"lat": 1.0, "lng": 2.0}, "date": "2024-01"}),
            _response(content=_REAL_IMAGE_BYTES),
        ]
        gateway = self._gateway(session)

        content, date, pano_lat, pano_lng = gateway.get_street_view_single(1.0, 2.0, radius=400)

        self.assertEqual(content, _REAL_IMAGE_BYTES)
        self.assertEqual(date, "2024-01")
        self.assertEqual(pano_lat, 1.0)
        self.assertEqual(pano_lng, 2.0)
        image_call_params = session.get.call_args_list[1].kwargs["params"]
        self.assertEqual(image_call_params["radius"], 400)

    def test_a_suspiciously_small_response_is_treated_as_no_imagery_and_retried(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            _response(json_data={"status": "OK", "location": {"lat": 1.0, "lng": 2.0}, "date": "2024-01"}),
            _response(content=_PLACEHOLDER_IMAGE_BYTES),  # too small - the placeholder graphic
            _response(json_data={"status": "OK", "location": {"lat": 1.0, "lng": 2.0}, "date": "2024-01"}),
            _response(content=_REAL_IMAGE_BYTES),
        ]
        gateway = self._gateway(session)

        content, _date, _pano_lat, _pano_lng = gateway.get_street_view_single(
            1.0, 2.0, radius=50, radius_increment=50, max_radius=200
        )

        self.assertEqual(content, _REAL_IMAGE_BYTES)
        self.assertEqual(session.get.call_count, 4)

    def test_exhausting_the_radius_on_only_small_responses_raises(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            _response(json_data={"status": "OK", "location": {"lat": 1.0, "lng": 2.0}, "date": "2024-01"}),
            _response(content=_PLACEHOLDER_IMAGE_BYTES),
        ]
        gateway = self._gateway(session)

        with self.assertRaises(ValueError):
            gateway.get_street_view_single(1.0, 2.0, radius=200, radius_increment=50, max_radius=200)

    def test_over_query_limit_raises_immediately_instead_of_sweeping_the_radius(self) -> None:
        """Regression guard for the SpotGuessr /start/ 504s: OVER_QUERY_LIMIT (and any
        other account/request-level failure) is not "no coverage here yet" - retrying
        at a wider radius just repeats the identical failure up to
        (max_radius - radius) / radius_increment times, turning one bad API key/quota
        into ~20 wasted calls per candidate location."""
        session = MagicMock()
        session.get.side_effect = [
            _response(json_data={"status": "OVER_QUERY_LIMIT"}),
        ]
        gateway = self._gateway(session)

        with self.assertRaises(ValueError):
            gateway.get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual(session.get.call_count, 1)

    def test_zero_results_keeps_sweeping_the_radius(self) -> None:
        """ZERO_RESULTS is a genuine "no pano here" answer, unlike OVER_QUERY_LIMIT -
        it must still expand the search radius rather than failing immediately."""
        session = MagicMock()
        session.get.side_effect = [
            _response(json_data={"status": "ZERO_RESULTS"}),
            _response(json_data={"status": "OK", "location": {"lat": 1.0, "lng": 2.0}, "date": "2024-01"}),
            _response(content=_REAL_IMAGE_BYTES),
        ]
        gateway = self._gateway(session)

        content, _date, _pano_lat, _pano_lng = gateway.get_street_view_single(
            1.0, 2.0, radius=50, radius_increment=50, max_radius=200
        )

        self.assertEqual(content, _REAL_IMAGE_BYTES)
