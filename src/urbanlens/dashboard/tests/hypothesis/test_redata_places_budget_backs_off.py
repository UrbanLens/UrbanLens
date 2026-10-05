"""A spent REData Places budget is left alone for as long as REData said (P315).

REData's whole Google Places budget is 160 uncached calls a UTC day, shared by every key and its own CID resolution,
and it asks each client to honour ``Retry-After`` on ``503 rate_limited``. The gateway raised that answer without
the wait, so a panel that met it came back after the five-minute failure window whatever REData had said.
"""

from __future__ import annotations

import json
import time
from unittest import mock

from django.core.cache import cache
from model_bakery import baker
import pytest
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.plugins.builtin.google_places import GoogleMapsPhotosPanelSource
from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError
from urbanlens.dashboard.services.core.rate_limiter import UpstreamThrottledError
from urbanlens.dashboard.services.pins.external_data import get_panel_source, run_panel_fetch
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_RATE_LIMITED = {"error": "rate_limited", "message": "Places API (New) request budget is exhausted right now."}
_WAIT = 600


def _response(status_code: int, body: dict, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.headers.update({"Content-Type": "application/json", **(headers or {})})
    response._content = json.dumps(body).encode()
    return response


class PlacesBudgetBacksOffTests(RedataConfiguredMixin, TestCase):
    """REData is answered below the rate-limited session, so its breaker sees every response."""

    def test_the_next_search_waits_out_the_wait_redata_named(self) -> None:
        gateway = RedataPlacesGateway()
        answers = [
            _response(503, _RATE_LIMITED, {"Retry-After": str(_WAIT)}),
            _response(200, {"count": 0, "results": []}),
        ]
        with mock.patch.object(requests.Session, "request", side_effect=answers) as sent:
            with pytest.raises(UpstreamBusyError) as first:
                gateway.search_nearby(41.73, -73.92)
            with pytest.raises(UpstreamThrottledError) as held:
                gateway.search_nearby(42.10, -74.20)
            self.assertEqual(sent.call_count, 1)
            with mock.patch(
                "urbanlens.dashboard.services.core.upstream_breaker.time.time", return_value=time.time() + _WAIT + 1
            ):
                self.assertEqual(gateway.search_nearby(42.10, -74.20), [])

        self.assertEqual(first.value.retry_after, _WAIT)
        self.assertGreaterEqual(held.value.retry_after, _WAIT - 1)
        self.assertEqual(sent.call_count, 2)

    def test_the_photos_panel_waits_as_long_and_keeps_nothing(self) -> None:
        pin = baker.make(
            Pin,
            profile=Profile.objects.get(user=baker.make("auth.User")),
            location=baker.make(Location, latitude=41.73, longitude=-73.92),
        )
        source = get_panel_source("google_maps")
        assert isinstance(source, GoogleMapsPhotosPanelSource)
        busy = _response(503, _RATE_LIMITED, {"Retry-After": str(_WAIT)})
        with (
            mock.patch.object(requests.Session, "request", return_value=busy),
            mock.patch("urbanlens.dashboard.services.pins.external_data.cache.set", wraps=cache.set) as setter,
        ):
            run_panel_fetch(source.key, pin, None)

        setter.assert_any_call(source.skip_key(pin), 1, _WAIT)
        self.assertFalse(LocationCache.objects.filter(location=pin.location, source=source.cache_source).exists())
