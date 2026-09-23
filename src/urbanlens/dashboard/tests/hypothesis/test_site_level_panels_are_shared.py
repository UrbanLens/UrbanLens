"""A building pin nested under a site reuses the site's answer for panels that describe the site.

Every HRSH building child has its own Location, so opening one ran every panel again at a point 50-300 m from its
parent: the nearest park unit within 100 km, earthquakes within 100 km, the parcel the building stands on. The
run's hour made 1,439 REData calls for one campus pin and 69 building pins.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.external_data import get_panel_source, run_panel_fetch

_SITE_LEVEL = "nps"
_BUILDING_LEVEL = "photon"


class SiteLevelPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        profile = Profile.objects.get(user=baker.make("auth.User"))
        self.site = baker.make(
            Pin, profile=profile, location=baker.make(Location, latitude=41.7321, longitude=-73.9262)
        )
        self.building = baker.make(
            Pin,
            profile=profile,
            parent_pin=self.site,
            location=baker.make(Location, latitude=41.7326, longitude=-73.9258),
        )

    def _cached(self, pin: Pin, source_key: str) -> dict | None:
        row = LocationCache.get_fresh(pin.location, get_panel_source(source_key).cache_source)
        return None if row is None else row.data

    def test_a_building_reuses_the_sites_answer(self) -> None:
        source = get_panel_source(_SITE_LEVEL)
        LocationCache.set(self.site.location, source.cache_source, {"park_code": "vama"}, query_key="site")

        with mock.patch.object(type(source), "fetch") as fetch:
            run_panel_fetch(_SITE_LEVEL, self.building, None)

        fetch.assert_not_called()
        self.assertEqual(self._cached(self.building, _SITE_LEVEL), {"park_code": "vama"})

    def test_a_building_asks_for_the_site_when_the_site_has_no_answer_yet(self) -> None:
        """So the next building, and the site's own page, find it answered."""
        source = get_panel_source(_SITE_LEVEL)

        def land(pin: Pin) -> None:
            LocationCache.set(pin.location, source.cache_source, {"park_code": "vama"}, query_key=str(pin.pk))

        with mock.patch.object(type(source), "fetch", autospec=True, side_effect=lambda _self, pin: land(pin)) as fetch:
            run_panel_fetch(_SITE_LEVEL, self.building, None)

        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1].pk, self.site.pk)
        self.assertEqual(self._cached(self.site, _SITE_LEVEL), {"park_code": "vama"})
        self.assertEqual(self._cached(self.building, _SITE_LEVEL), {"park_code": "vama"})

    def test_a_building_level_panel_still_asks_about_the_building(self) -> None:
        source = get_panel_source(_BUILDING_LEVEL)
        LocationCache.set(self.site.location, source.cache_source, {"locality": "Poughkeepsie"}, query_key="site")

        with mock.patch.object(type(source), "fetch") as fetch:
            run_panel_fetch(_BUILDING_LEVEL, self.building, None)

        fetch.assert_called_once()

    def test_a_site_pin_asks_for_itself(self) -> None:
        source = get_panel_source(_SITE_LEVEL)

        with mock.patch.object(type(source), "fetch") as fetch:
            run_panel_fetch(_SITE_LEVEL, self.site, None)

        fetch.assert_called_once()
