"""A refused external call costs a picker its results, not the page.

A limiter that cannot read its own table refuses the call like any other refusal: under R29 an executor thread's
connection is refused whenever ``ul_web`` is at its limit.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db import OperationalError
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.core.rate_limiter import (
    RateLimitExceededError,
    RequestCancelledError,
    ServiceDisabledError,
)

_REFUSED = OperationalError('FATAL:  too many connections for role "ul_web"')


class AnUnreadableLimiterRefusesTheCallTests(TestCase):
    def test_a_refused_connection_refuses_the_call(self) -> None:
        with (
            mock.patch.object(rate_limiter, "get_limit_config", side_effect=_REFUSED),
            self.assertRaises(RequestCancelledError),
        ):
            rate_limiter._reserve_call("immich")

    def test_a_failed_locked_read_refuses_the_call(self) -> None:
        rate_limiter.get_limit_config("immich")
        with (
            mock.patch.object(ApiRateLimit.objects, "select_for_update", side_effect=_REFUSED),
            self.assertRaises(RequestCancelledError),
        ):
            rate_limiter._reserve_call("immich")


class _PickerCase(TestCase):
    provider: str

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make(
            Pin, profile=self.user.profile, location=baker.make(Location, latitude=40.0, longitude=-74.0)
        )
        baker.make(PinVisit, pin=self.pin)

    def _search(self):
        url = reverse(f"pin.{self.provider}.search", kwargs={"pin_slug": self.pin.slug})
        return self.client.get(url, {"mode": "visits"})


class TheImmichPickerDegradesTests(_PickerCase):
    provider = "immich"

    def setUp(self) -> None:
        super().setUp()
        ImmichAccount.objects.create(profile=self.user.profile, server_url="https://photos.example.com", api_key="k")

    def test_an_unreadable_limiter_shows_the_error_card_without_calling_out(self) -> None:
        with (
            mock.patch.object(rate_limiter, "get_limit_config", side_effect=_REFUSED),
            mock.patch("requests.Session.request") as wire,
        ):
            response = self._search()

        wire.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn("load your Immich library right now.", response.content.decode())

    def test_a_disabled_service_shows_the_error_card(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.immich.ImmichGateway") as gateway:
            gateway.return_value.search_by_dates.side_effect = ServiceDisabledError("immich")
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertIn("load your Immich library right now.", response.content.decode())


class TheFlickrPickerDegradesTests(_PickerCase):
    provider = "flickr"

    def setUp(self) -> None:
        super().setUp()
        FlickrAccount.objects.create(
            profile=self.user.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00"
        )

    def test_a_rate_limited_search_shows_the_error_card(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.flickr.FlickrGateway") as gateway:
            gateway.return_value.search_by_dates.side_effect = RateLimitExceededError("flickr")
            response = self._search()

        self.assertEqual(response.status_code, 200)
        self.assertIn("load your Flickr library right now.", response.content.decode())
