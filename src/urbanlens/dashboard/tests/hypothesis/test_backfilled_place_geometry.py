"""A backfilled parcel must not keep its pre-chain geometry forever."""

from __future__ import annotations

import datetime
from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.places.provisioning import geometry_stale

#: Frozen so the exact-boundary tests below pin the comparison instead of racing wall-clock time.
_INSTANT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

_hyp = hyp_settings(max_examples=30, deadline=None)


def _square(size: float) -> MultiPolygon:
    """A square parcel of *size* degrees anchored near the reported pin."""
    west, south = -73.92794, 41.73332
    ring = ((west, south), (west + size, south), (west + size, south + size), (west, south + size), (west, south))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class BackfilledGeometryStalenessTests(TestCase):
    """``geometry_stale`` decides whether the provider chain ever runs again."""

    def _place(self, *, generated_at, geometry=None) -> Place:
        return baker.make(
            Place,
            kind=PlaceKind.PARCEL,
            geometry=geometry if geometry is not None else _square(0.01),
            geometry_generated_at=generated_at,
        )

    def test_a_backfilled_place_is_stale(self) -> None:
        """The reported case: geometry the provider chain never produced.

        Left as "not stale", REData is never asked for this parcel and the
        pre-places boundary stands forever.
        """
        place = self._place(generated_at=None)

        self.assertTrue(geometry_stale(place), "a backfilled parcel is pinned to its pre-chain geometry forever")

    def test_a_freshly_generated_place_is_not_stale(self) -> None:
        """The cache window still has to mean something."""
        place = self._place(generated_at=timezone.now())

        self.assertFalse(geometry_stale(place))

    def test_an_expired_place_is_stale(self) -> None:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        window = SiteSettings.get_current().boundary_cache_days
        place = self._place(generated_at=timezone.now() - timedelta(days=window + 1))

        self.assertTrue(geometry_stale(place))

    def test_a_place_exactly_at_the_cache_window_is_not_stale(self) -> None:
        """The comparison is strict (``>``): aged to exactly the window is still fresh.

        A ``>=`` mutant would flip this one case without affecting any of the others above.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings

        window = SiteSettings.get_current().boundary_cache_days
        with patch("django.utils.timezone.now", return_value=_INSTANT):
            place = self._place(generated_at=_INSTANT - timedelta(days=window))
            self.assertFalse(geometry_stale(place))

    def test_a_place_one_second_past_the_cache_window_is_stale(self) -> None:
        """The same boundary, one second later, must already read stale."""
        from urbanlens.dashboard.models.site_settings import SiteSettings

        window = SiteSettings.get_current().boundary_cache_days
        with patch("django.utils.timezone.now", return_value=_INSTANT):
            place = self._place(generated_at=_INSTANT - timedelta(days=window, seconds=1))
            self.assertTrue(geometry_stale(place))

    def test_a_geometryless_place_is_stale_too(self) -> None:
        """Nothing is lost by asking: it cannot be resolved onto as it stands."""
        place = self._place(generated_at=None, geometry=None)

        self.assertTrue(geometry_stale(place))

    @given(
        configured_days=st.integers(min_value=1, max_value=365),
        age_days=st.floats(min_value=0, max_value=400, allow_nan=False),
    )
    @_hyp
    def test_staleness_matches_the_configured_window_at_any_age(self, configured_days: int, age_days: float) -> None:
        """The whole comparison, generalised - not just the specific ages pinned above.

        Time must be frozen: geometry_stale() calls timezone.now() again internally, and an unfrozen clock lets
        real elapsed time between that call and generated_at's construction drift age_days past configured_days
        for examples that land exactly on the boundary."""
        from urbanlens.dashboard.models.site_settings import SiteSettings

        site_settings = SiteSettings.get_current()
        site_settings.boundary_cache_days = configured_days
        site_settings.save()

        with patch("django.utils.timezone.now", return_value=_INSTANT):
            place = self._place(generated_at=_INSTANT - timedelta(days=age_days))
            self.assertEqual(geometry_stale(place), age_days > configured_days)
