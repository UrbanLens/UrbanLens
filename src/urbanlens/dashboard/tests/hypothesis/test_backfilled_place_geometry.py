"""A backfilled parcel must not keep its pre-chain geometry forever.

Reported from staging, on the HRSH pin: "the parcel boundary is monumentally
too big, not the suggested boundary from REData", and consequently "buildings
on this property" offered 2604 buildings.

The two are one defect, not two. ``parcel_buildings`` asks Overpass for the
buildings *inside the parcel polygon*, so an oversized parcel is an oversized
building count - the sources are strictly either/or there (REData's own
buildings, else OSM; never merged), so no amount of cross-source deduplication
would have reduced 2604.

Why the polygon is oversized is the real bug, and it is a migration artifact.
``0027_places_backfill`` created a Place per pre-existing location boundary
with ``geometry_generated_at=None`` - correctly, because the provider chain had
not produced that geometry. But ``geometry_stale`` read the same null as
"pending, not stale" and returned False, and ``ensure_place_for_location``
only re-runs the chain when the place is absent or stale. So every backfilled
parcel is pinned to whatever boundary predated the places system, permanently:
REData is never asked, and its parcel is only ever recorded as a losing
*candidate*.

A null timestamp cannot mean "pending" any more, because nothing writes one:
``upsert_place`` stamps ``now`` on both its create and its update branch, with
or without geometry. The only rows carrying a null are the backfilled ones, and
those are exactly the rows that have never been offered to a provider.

The re-resolution is one-time per place - the chain stamps a timestamp on the
way through, and normal ``boundary_cache_days`` caching resumes.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.places.provisioning import geometry_stale


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

    def test_a_geometryless_place_is_stale_too(self) -> None:
        """Nothing is lost by asking: it cannot be resolved onto as it stands."""
        place = self._place(generated_at=None, geometry=None)

        self.assertTrue(geometry_stale(place))
