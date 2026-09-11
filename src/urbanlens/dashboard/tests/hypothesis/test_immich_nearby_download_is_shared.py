"""The Immich picker must download a library once, not once per pin.

Immich exposes no coordinate-radius filter, so "photos near this pin" can only
be answered by fetching every geolocated asset and measuring in Python - tens
of thousands of assets over the network, inside the request. The existing cache
is keyed on the *point*, which makes the six radius options on one pin share a
download but makes every pin pay for its own (N21 H09).

The download is a property of the account, not of the point. Only the measuring
and sorting depend on where the pin is, so those stay keyed per point and the
fetch moves behind a key that every pin in the library shares.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.immich.gateway import MapMarker
from urbanlens.dashboard.services.apis.immich.nearby import nearby_assets, within_radius

CEILING_SETTING = "IMMICH_MARKER_CACHE_MAX_ASSETS"

#: Two points about 1.4km apart, so a marker near one is far from the other.
HERE = (40.0, -75.0)
THERE = (40.01, -75.0)


class _NearbyCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.account = baker.make(ImmichAccount, profile=self.profile)

    def _gateway(self, markers: list[MapMarker]) -> mock.Mock:
        gateway = mock.Mock()
        gateway.get_map_markers.return_value = markers
        return gateway

    def _library(self, count: int) -> list[MapMarker]:
        return [MapMarker(id=f"asset-{index}", lat=40.0 + index * 0.00001, lon=-75.0) for index in range(count)]


class TheDownloadIsSharedAcrossPinsTests(_NearbyCase):
    def test_a_second_pin_reuses_the_first_pins_download(self) -> None:
        gateway = self._gateway(self._library(5))

        nearby_assets(gateway, self.account, HERE)
        nearby_assets(gateway, self.account, THERE)

        self.assertEqual(
            gateway.get_map_markers.call_count,
            1,
            "opening the picker on a second pin downloaded the whole library again",
        )

    def test_each_pin_is_still_measured_from_its_own_position(self) -> None:
        """The half that stops the test above passing against a cache that serves one answer everywhere."""
        gateway = self._gateway([MapMarker(id="close-to-here", lat=40.0, lon=-75.0)])

        here = within_radius(nearby_assets(gateway, self.account, HERE), radius_m=200)
        there = within_radius(nearby_assets(gateway, self.account, THERE), radius_m=200)

        self.assertEqual([marker.id for marker in here], ["close-to-here"])
        self.assertEqual(there, [], "a marker 1.4km away was reported as within 200m")

    def test_another_account_does_not_read_this_ones_library(self) -> None:
        other = baker.make(ImmichAccount, profile=Profile.objects.get(user=baker.make("auth.User")))
        mine = self._gateway([MapMarker(id="mine", lat=40.0, lon=-75.0)])
        theirs = self._gateway([MapMarker(id="theirs", lat=40.0, lon=-75.0)])

        nearby_assets(mine, self.account, HERE)
        result = nearby_assets(theirs, other, HERE)

        self.assertEqual([marker.id for _, marker in result.nearest], ["theirs"])


class TheSharedCacheIsBoundedTests(_NearbyCase):
    """It lands in the single Valkey that also holds everyone's sessions."""

    def test_the_ceiling_is_a_real_setting(self) -> None:
        from django.conf import settings

        self.assertTrue(hasattr(settings, CEILING_SETTING), f"nothing reads {CEILING_SETTING}")

    @override_settings(**{CEILING_SETTING: 3})
    def test_a_library_over_the_ceiling_is_served_but_not_stored(self) -> None:
        gateway = self._gateway(self._library(10))

        first = nearby_assets(gateway, self.account, HERE)
        nearby_assets(gateway, self.account, THERE)

        self.assertEqual(len(first.nearest), 10, "an oversized library must still be answered")
        self.assertEqual(gateway.get_map_markers.call_count, 2, "an oversized library was stored anyway")

    @override_settings(**{CEILING_SETTING: 100})
    def test_a_library_under_the_ceiling_is_stored(self) -> None:
        """The anti-vacuity half: a ceiling that stores nothing would pass above."""
        gateway = self._gateway(self._library(10))

        nearby_assets(gateway, self.account, HERE)
        nearby_assets(gateway, self.account, THERE)

        self.assertEqual(gateway.get_map_markers.call_count, 1)
