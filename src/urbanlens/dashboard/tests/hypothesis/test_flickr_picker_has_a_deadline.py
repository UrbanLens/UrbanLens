"""The Flickr picker can hold a worker for seven and a half minutes.

N21 H36. The "visits" mode issues one Flickr search per visit date, because
Flickr's `min_taken_date`/`max_taken_date` take a single range rather than a set
of days. `MAX_VISIT_DATES` is 15 and the per-call `_REQUEST_TIMEOUT` is 30
seconds, so the *inner* timeouts bound each call and nothing bounds their sum:
450 seconds of wall clock inside one GET, with the request thread held the whole
time. Every per-call timeout can be respected and the request still outlast any
patience the user or the proxy in front of it has.

`call_with_deadline` already exists for exactly this and `controllers/pin.py`
already uses it for its own provider fan-out, degrading to an error card rather
than holding the request open. The picker is the same shape and did not.

Bounding the wall clock does not stop the abandoned call - Python cannot kill a
blocked thread, which is why the helper keeps a small dedicated pool - but it
does stop the *request* waiting on it, which is what the worker is for.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit

_GATEWAY = "urbanlens.dashboard.controllers.flickr.FlickrGateway"


class _FlickrPickerCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        FlickrAccount.objects.create(
            profile=self.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00"
        )
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=40.0, longitude=-74.0))
        baker.make(PinVisit, pin=self.pin)

    def _search(self, mode: str = "visits"):
        return self.client.get(reverse("pin.flickr.search", kwargs={"pin_slug": self.pin.slug}), {"mode": mode})


class TheSearchHasAWallClockDeadlineTests(_FlickrPickerCase):
    """A slow provider must cost the request a deadline, not the sum of its calls."""

    def test_a_hanging_search_returns_rather_than_holding_the_request(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def _hang(*_args, **_kwargs):
            release.wait(timeout=30)
            return []

        with (
            mock.patch("urbanlens.dashboard.services.core.timeout_utils.EXTERNAL_CALL_DEADLINE", 0.2),
            mock.patch(_GATEWAY) as gateway,
        ):
            gateway.return_value.search_by_dates.side_effect = _hang
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertIn("load your Flickr library right now.", response.content.decode())


class OrdinarySearchesStillWorkTests(_FlickrPickerCase):
    """The half that stops the deadline passing against a view that always errors."""

    def test_a_prompt_search_still_returns_its_photos(self) -> None:
        photo = mock.Mock(id="p1", thumbnail_url="https://example.invalid/t.jpg")

        with mock.patch(_GATEWAY) as gateway:
            gateway.return_value.search_by_dates.return_value = [photo]
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("load your Flickr library right now.", response.content.decode())

    def test_a_gateway_error_still_degrades_the_same_way(self) -> None:
        """The pre-existing failure path must keep working, not be replaced by the deadline."""
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        with mock.patch(_GATEWAY) as gateway:
            gateway.return_value.search_by_dates.side_effect = GatewayRequestError("nope")
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertIn("load your Flickr library right now.", response.content.decode())


class TheImmichPickerHasTheSameDoorTests(TestCase):
    """The same shape, one provider over - and the finding named only Flickr.

    `ImmichGateway.search_by_dates` issues one metadata search per date for the
    same reason Flickr's does, and its "nearby" mode measures against a whole
    library download. Capping one picker would have left the other holding a
    worker for as long as somebody's server takes to answer.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        from urbanlens.dashboard.models.immich.model import ImmichAccount

        ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=40.0, longitude=-74.0))
        baker.make(PinVisit, pin=self.pin)

    def _search(self, mode: str = "visits"):
        return self.client.get(reverse("pin.immich.search", kwargs={"pin_slug": self.pin.slug}), {"mode": mode})

    def test_a_hanging_search_returns_rather_than_holding_the_request(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def _hang(*_args, **_kwargs):
            release.wait(timeout=30)
            return []

        with (
            mock.patch("urbanlens.dashboard.services.core.timeout_utils.EXTERNAL_CALL_DEADLINE", 0.2),
            mock.patch("urbanlens.dashboard.controllers.immich.ImmichGateway") as gateway,
        ):
            gateway.return_value.search_by_dates.side_effect = _hang
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertIn("load your Immich library right now.", response.content.decode())

    def test_a_prompt_search_still_returns_its_assets(self) -> None:
        """The anti-vacuity half."""
        with mock.patch("urbanlens.dashboard.controllers.immich.ImmichGateway") as gateway:
            gateway.return_value.search_by_dates.return_value = [mock.Mock(id="a1")]
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("load your Immich library right now.", response.content.decode())
