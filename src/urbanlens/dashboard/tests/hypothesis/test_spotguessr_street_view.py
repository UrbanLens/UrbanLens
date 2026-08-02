"""Tests for services.spotguessr.street_view - Street View mode's imagery selection.

The Google Maps gateway itself is mocked throughout - these tests are about
this module's own contract (graceful None on any failure), not about
exercising the real Street View Static API.
"""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.apis.locations.base import StreetViewSlide
from urbanlens.dashboard.services.spotguessr.street_view import candidate_street_view_for_location

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


class CandidateStreetViewForLocationTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()

    @patch("urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway.get_street_view_slides")
    def test_returns_the_slides_image_data_uri_on_success(self, mock_slides) -> None:
        mock_slides.return_value = ([StreetViewSlide(img_src="data:image/jpeg;base64,abc123", source="Google Street View", date="2024-01")], False)
        result = candidate_street_view_for_location(self.location)
        self.assertEqual(result, "data:image/jpeg;base64,abc123")

    @patch("urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway.get_street_view_slides")
    def test_no_coverage_returns_none(self, mock_slides) -> None:
        mock_slides.return_value = ([], False)
        self.assertIsNone(candidate_street_view_for_location(self.location))

    @patch("urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway.get_street_view_slides")
    def test_api_error_is_swallowed_not_raised(self, mock_slides) -> None:
        mock_slides.side_effect = ValueError("No Street View imagery found within the maximum search radius.")
        self.assertIsNone(candidate_street_view_for_location(self.location))

    @patch("urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway.get_street_view_slides")
    def test_network_error_is_swallowed_not_raised(self, mock_slides) -> None:
        mock_slides.side_effect = ConnectionError("boom")
        self.assertIsNone(candidate_street_view_for_location(self.location))

    @patch("urbanlens.dashboard.services.spotguessr.street_view.call_with_deadline")
    def test_the_lookup_is_bounded_by_the_shared_external_call_deadline(self, mock_deadline) -> None:
        """Regression guard for the SpotGuessr /start/ 504s: this is called up to
        _MAX_LOCATION_ATTEMPTS times synchronously inside the request handler (see
        services.spotguessr.session.get_or_create_round), same as pin.py's Street View
        carousel fetch - it must go through the same call_with_deadline bound that
        fetch already uses, or one slow/degraded provider call can hold the whole
        request open well past nginx's timeout."""
        from urbanlens.dashboard.services.core.timeout_utils import EXTERNAL_CALL_DEADLINE

        mock_deadline.return_value = ([StreetViewSlide(img_src="data:image/jpeg;base64,abc123", source="Google Street View", date="2024-01")], False)

        result = candidate_street_view_for_location(self.location)

        self.assertEqual(result, "data:image/jpeg;base64,abc123")
        mock_deadline.assert_called_once()
        self.assertEqual(mock_deadline.call_args.kwargs["timeout"], EXTERNAL_CALL_DEADLINE)

    @patch("urbanlens.dashboard.services.spotguessr.street_view.call_with_deadline")
    def test_a_timed_out_lookup_degrades_to_no_coverage(self, mock_deadline) -> None:
        # call_with_deadline itself never raises on timeout - it returns `default`.
        mock_deadline.return_value = ([], False)
        self.assertIsNone(candidate_street_view_for_location(self.location))
