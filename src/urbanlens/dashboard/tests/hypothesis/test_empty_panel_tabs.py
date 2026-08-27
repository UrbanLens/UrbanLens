"""A panel tab appears only when it has something to show.

Reported from staging: the Photon (address) and Elevation tabs rendered as
empty panels. Both `render_context` implementations already returned None for
those payloads - the tab appeared anyway, because `panel_readiness` answers
"does a fresh cache row exist", not "is there anything in it". A successful
lookup that legitimately finds nothing (a coordinate with no reverse-geocodable
address, or one outside every elevation model's coverage) still writes a row.

The check is opt-in per panel (`inspects_content`) rather than universal: the
batched readiness query fetches only source names, and pulling every payload -
boundary geometry, image lists - on each pin render to answer a question that
is "yes" for almost every panel would be a poor trade.

The distinction this must preserve, and the reason it is not simply "hide empty
tabs": an *outage* is not an empty result. A failed fetch writes no row at all
(see test_outage_not_cached_as_empty.py), so it is already absent here rather
than being silently hidden.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.open_elevation import ElevationPanelSource
from urbanlens.dashboard.plugins.builtin.photon import PhotonPanelSource
from urbanlens.dashboard.services.pins.external_data import panel_readiness


class EmptyPanelTabTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.73, longitude=-73.92)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def _readiness(self, source, payload) -> bool:
        LocationCache.set(self.location, source.cache_source, payload, query_key="k")
        return panel_readiness(self.pin, [source])[source.key]

    def test_an_address_with_nothing_in_it_gets_no_tab(self) -> None:
        self.assertFalse(self._readiness(PhotonPanelSource(), {"house_number": None}))

    def test_a_real_address_gets_a_tab(self) -> None:
        self.assertTrue(self._readiness(PhotonPanelSource(), {"locality": "Poughkeepsie", "region": "NY"}))

    def test_an_elevation_with_no_reading_gets_no_tab(self) -> None:
        self.assertFalse(self._readiness(ElevationPanelSource(), {"readings": []}))

    def test_a_real_elevation_gets_a_tab(self) -> None:
        self.assertTrue(self._readiness(ElevationPanelSource(), {"elevation_m": 47.0}))

    def test_an_elevation_of_exactly_zero_still_counts(self) -> None:
        """Sea level is a reading, and `if not elevation` would drop it."""
        self.assertTrue(self._readiness(ElevationPanelSource(), {"elevation_m": 0}))

    def test_a_panel_that_does_not_opt_in_is_unaffected(self) -> None:
        """The default stays "a fresh row means ready" - no extra cost, no behaviour change."""
        source = PhotonPanelSource()
        self.assertTrue(source.inspects_content)

        class _Plain(type(source)):
            inspects_content = False

        self.assertTrue(self._readiness(_Plain(), {"house_number": None}))


class ReadinessFormsAgreeTests(TestCase):
    """The single and bulk forms of "is this panel ready" must not disagree.

    `panel_readiness` is documented as the batched form of `is_ready`, and both
    are consulted in different places - the tab strip uses the batched one, the
    Location Data overview loops over the single one. A content check applied to
    only one of them makes a tab vanish while the overview still tries to
    summarise it (and gets nothing).
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.75, longitude=-73.95)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def _both(self, source, payload) -> tuple[bool, bool]:
        LocationCache.set(self.location, source.cache_source, payload, query_key="k")
        return source.is_ready(self.pin), panel_readiness(self.pin, [source])[source.key]

    def test_they_agree_on_an_empty_payload(self) -> None:
        single, bulk = self._both(PhotonPanelSource(), {"house_number": None})

        self.assertEqual(single, bulk)
        self.assertFalse(single)

    def test_they_agree_on_a_real_payload(self) -> None:
        single, bulk = self._both(PhotonPanelSource(), {"locality": "Beacon"})

        self.assertEqual(single, bulk)
        self.assertTrue(single)

    def test_they_agree_when_nothing_is_cached(self) -> None:
        source = ElevationPanelSource()

        self.assertEqual(source.is_ready(self.pin), panel_readiness(self.pin, [source])[source.key])
