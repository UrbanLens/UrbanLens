"""Location.place_name must never block a request on a live Google API call.

Covers the full lazy-loading chain: the model property is cache-only,
tasks.resolve_location_place_name is what actually populates the cache in the
background, and PinOverviewView (the actual renderer of
pin_overview_partial.html/deduplicated_identity_fields, loaded as an HTMX
fragment by the main pin detail page) is what dispatches it - see each
piece's own docstring for the reasoning. Regression coverage for the reported
bug: "we're still contacting external APIs (like google places) immediately
on the import of each pin... even when place data should be cached."
"""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class ResolveLocationPlaceNameTaskTests(TestCase):
    """tasks.resolve_location_place_name - the background half of the lazy fetch."""

    def test_populates_the_cache(self) -> None:
        from urbanlens.dashboard.tasks import resolve_location_place_name

        location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=None)
        with patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value="Old Factory"):
            result = resolve_location_place_name(location.pk)
        self.assertEqual(result, "Old Factory")
        location.refresh_from_db()
        self.assertEqual(location.cached_place_name, "Old Factory")

    def test_missing_location_is_a_no_op(self) -> None:
        from urbanlens.dashboard.tasks import resolve_location_place_name

        self.assertIsNone(resolve_location_place_name(999999))


class PinViewDispatchesPlaceNameResolutionTests(TestCase):
    """PinOverviewView's background dispatch, gated on external_apis_enabled
    and only when nothing is cached yet."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill")
        # Gives the location a route so PinOverviewView's separate address-backfill
        # dispatch (tasks.backfill_location_address) is skipped - unrelated to this
        # fix, and _dispatched_location_ids filters by task identity anyway; this
        # just keeps each test's dispatch surface down to the one being tested.
        self.pin.location.route = "Test St"
        self.pin.location.save(update_fields=["route"])
        self.client.force_login(self.user)

    def _get_pin_page(self):
        return self.client.get(reverse("pin.overview", args=[self.pin.slug]))

    def _dispatched_location_ids(self, mock_enqueue) -> list[int]:
        from urbanlens.dashboard.tasks import resolve_location_place_name

        return [call.args[1] for call in mock_enqueue.call_args_list if call.args and call.args[0] is resolve_location_place_name]

    def test_dispatches_when_uncached_and_apis_enabled(self) -> None:
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue:
            self._get_pin_page()
        self.assertIn(self.pin.location_id, self._dispatched_location_ids(mock_enqueue))

    def test_does_not_dispatch_when_apis_disabled(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue:
            self._get_pin_page()
        self.assertEqual(self._dispatched_location_ids(mock_enqueue), [])

    def test_does_not_dispatch_when_already_cached(self) -> None:
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        self.pin.location.cached_place_name = "Old Mill (Google Maps)"
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue:
            self._get_pin_page()
        self.assertEqual(self._dispatched_location_ids(mock_enqueue), [])

    def test_page_render_never_calls_the_live_resolver(self) -> None:
        """The actual reported bug: rendering the pin page must never itself
        make a live Google Places/Geocoding call, cached or not."""
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        with patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name") as mock_resolve:
            response = self._get_pin_page()
        self.assertEqual(response.status_code, 200)
        mock_resolve.assert_not_called()
