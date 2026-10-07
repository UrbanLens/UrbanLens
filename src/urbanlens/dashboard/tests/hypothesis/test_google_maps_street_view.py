"""Tests for GoogleMapsGateway.get_street_view_single - Street View "no imagery" placeholder leak."""

from __future__ import annotations

from unittest.mock import MagicMock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.tests.hypothesis.test_proxied_media_is_capped import _streamed

_REAL_IMAGE_BYTES = b"x" * 5000
_PLACEHOLDER_IMAGE_BYTES = b"x" * 500


def _response(json_data=None, content=None):
    """A metadata answer (JSON), or an image answer, which is read through ``read_capped`` and so must be streamed."""
    if content is not None:
        return _streamed(content)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    if json_data is not None:
        response.json.return_value = json_data
    return response


class _FakeStreetView:
    """Google's Street View answers: a pano *pano_at* metres away is found by any search reaching that far."""

    def __init__(
        self,
        *,
        pano_at: float | None,
        status: str | None = None,
        image_for=lambda radius: _REAL_IMAGE_BYTES,
        pano_id: str | None = None,
    ) -> None:
        self.pano_at = pano_at
        self.status = status
        self.pano_id = pano_id
        self.image_for = image_for
        self.searched: list[int] = []
        self.images: list[dict] = []

    def get(self, url: str, params: dict, **_kwargs):
        if url.endswith("/metadata"):
            self.searched.append(params["radius"])
            if self.status is not None:
                return _response(json_data={"status": self.status})
            if self.pano_at is None or params["radius"] < self.pano_at:
                return _response(json_data={"status": "ZERO_RESULTS"})
            found = {"status": "OK", "location": {"lat": 1.0, "lng": 2.0}, "date": "2024-01"}
            return _response(json_data={**found, "pano_id": self.pano_id} if self.pano_id else found)
        self.images.append(params)
        return _response(content=self.image_for(params["radius"]))


class GetStreetViewSingleTests(SimpleTestCase):
    def _gateway(self, session) -> GoogleMapsGateway:
        return GoogleMapsGateway(api_key="test-key", session=session)

    def test_the_image_request_keeps_the_radius_metadata_found_the_pano_at(self) -> None:
        """Regression guard: dropping `radius` from the image request lets it re-search with Google's own smaller default and miss a pano that metadata only found by expanding the search radius - silently returning the "Sorry, we have no imagery here" placeholder instead of the real photo."""
        google = _FakeStreetView(pano_at=400)

        content, date, pano_lat, pano_lng = self._gateway(google).get_street_view_single(1.0, 2.0, radius=400)

        self.assertEqual((content, date, pano_lat, pano_lng), (_REAL_IMAGE_BYTES, "2024-01", 1.0, 2.0))
        self.assertEqual([image["radius"] for image in google.images], [400])

    def test_the_nearest_radius_is_found_and_kept(self) -> None:
        google = _FakeStreetView(pano_at=480)

        self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual([image["radius"] for image in google.images], [500])

    def test_a_pano_beside_the_point_costs_one_search(self) -> None:
        google = _FakeStreetView(pano_at=20)

        self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual(google.searched, [50])

    def test_no_pano_within_reach_costs_two_searches(self) -> None:
        """A 50 m sweep to 1 km asked twenty times, a whole minute of the Google Maps budget, for one rural pin."""
        google = _FakeStreetView(pano_at=None)

        with self.assertRaises(ValueError):
            self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual(google.searched, [50, 1000])

    def test_a_distant_pano_is_found_in_a_few_searches(self) -> None:
        google = _FakeStreetView(pano_at=730)

        self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual([image["radius"] for image in google.images], [750])
        self.assertLessEqual(len(google.searched), 7)

    def test_a_suspiciously_small_response_is_treated_as_no_imagery_and_retried(self) -> None:
        google = _FakeStreetView(
            pano_at=50, image_for=lambda radius: _PLACEHOLDER_IMAGE_BYTES if radius < 100 else _REAL_IMAGE_BYTES
        )

        content, _date, _pano_lat, _pano_lng = self._gateway(google).get_street_view_single(
            1.0, 2.0, radius=50, radius_increment=50, max_radius=200
        )

        self.assertEqual(content, _REAL_IMAGE_BYTES)
        self.assertEqual([image["radius"] for image in google.images], [50, 100])

    def test_a_placeholder_costs_one_more_search_not_a_sweep(self) -> None:
        """Restarting the bisection after each placeholder asked 25 times and fetched 19 images for one pano at 100 m."""
        google = _FakeStreetView(pano_at=100, image_for=lambda radius: _PLACEHOLDER_IMAGE_BYTES)

        with self.assertRaises(ValueError):
            self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertLessEqual(len(google.searched), 8)
        self.assertEqual(len(google.images), 2)

    def test_a_wider_search_naming_the_same_pano_is_not_fetched_again(self) -> None:
        """Google answers the pano closest to the point, so a wider search names the one whose image was the placeholder."""
        google = _FakeStreetView(pano_at=100, image_for=lambda radius: _PLACEHOLDER_IMAGE_BYTES, pano_id="CAoSLEFGMVFp")

        with self.assertRaises(ValueError):
            self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual(len(google.images), 1)

    def test_exhausting_the_radius_on_only_small_responses_raises(self) -> None:
        google = _FakeStreetView(pano_at=50, image_for=lambda radius: _PLACEHOLDER_IMAGE_BYTES)

        with self.assertRaises(ValueError):
            self._gateway(google).get_street_view_single(1.0, 2.0, radius=200, radius_increment=50, max_radius=200)

    def test_over_query_limit_raises_immediately_instead_of_sweeping_the_radius(self) -> None:
        """Regression guard for the SpotGuessr /start/ 504s: OVER_QUERY_LIMIT (and any other account/request-level failure) is not "no coverage here yet" - retrying at a wider radius just repeats the identical failure up to (max_radius - radius) / radius_increment times, turning one bad API key/quota into ~20 wasted calls per candidate location."""
        google = _FakeStreetView(pano_at=None, status="OVER_QUERY_LIMIT")

        with self.assertRaises(ValueError):
            self._gateway(google).get_street_view_single(1.0, 2.0, radius=50, radius_increment=50, max_radius=1000)

        self.assertEqual(len(google.searched), 1)

    def test_zero_results_keeps_sweeping_the_radius(self) -> None:
        """ZERO_RESULTS is a genuine "no pano here" answer, unlike OVER_QUERY_LIMIT -
        it must still expand the search radius rather than failing immediately."""
        google = _FakeStreetView(pano_at=100)

        content, _date, _pano_lat, _pano_lng = self._gateway(google).get_street_view_single(
            1.0, 2.0, radius=50, radius_increment=50, max_radius=200
        )

        self.assertEqual(content, _REAL_IMAGE_BYTES)
