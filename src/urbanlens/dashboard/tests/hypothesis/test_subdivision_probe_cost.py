"""Correcting an oversized parcel must not cost one provider-chain run per location it captured.

A legacy parcel outline for Hudson River State Hospital covered 102.9 km² and held 99 locations.
Re-resolving it read the shrink to 0.47 km² as a subdivision, then ran the full chain for every
other location in the domain - 228 REData lookups in an hour without finishing the first parcel.
"""

from __future__ import annotations

from io import StringIO
import math
from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries
from urbanlens.dashboard.services.places import provisioning

_CHAIN = "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain"
WEST, SOUTH = -73.95, 41.70


def _box(west: float, south: float, size: float) -> MultiPolygon:
    ring = ((west, south), (west + size, south), (west + size, south + size), (west, south + size), (west, south))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _Chain:
    """Answers each coordinate with the small parcel tile it falls in, and counts the calls."""

    tile = 0.01
    calls = 0

    def get_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> ResolvedBoundaries:
        type(self).calls += 1
        west = WEST + int((longitude - WEST) / self.tile) * self.tile
        south = SOUTH + int((latitude - SOUTH) / self.tile) * self.tile
        return ResolvedBoundaries(property_polygon=_box(west, south, self.tile))


class SubdivisionProbeCostTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        _Chain.calls = 0
        # Large but plausible: a county-sized outline is never probed at all (P148).
        self.oversized = baker.make(
            Place, kind=PlaceKind.PARCEL, geometry=_box(WEST, SOUTH, 0.03), area_sqm=8_300_000.0
        )
        self.oversized.domain_root = self.oversized
        self.oversized.save()
        # Two small parcels' worth of locations, ten in each, all captured by the oversized outline.
        self.locations = [
            baker.make(
                Location,
                latitude=SOUTH + 0.001 + row * 0.0005,
                longitude=WEST + 0.001 + col * 0.01,
                place=self.oversized,
            )
            for col in (0, 1)
            for row in range(10)
        ]

    def test_a_real_split_probes_each_successor_once_not_each_location(self) -> None:
        with mock.patch(_CHAIN, _Chain):
            provisioning.ensure_place_for_location(self.locations[0], force=True)

        self.assertLessEqual(
            _Chain.calls,
            3,
            "every location inside a successor already found was probed again - one chain run per captured location",
        )

    def test_the_split_still_finds_every_successor(self) -> None:
        """Anti-vacuity: skipping covered locations must not skip a successor nobody has found yet."""
        with (
            mock.patch(_CHAIN, _Chain),
            mock.patch("urbanlens.dashboard.services.places.splits.process_split") as split,
        ):
            provisioning.ensure_place_for_location(self.locations[0], force=True)

        split.assert_called_once()
        self.assertEqual(len(split.call_args.args[1]), 2, "the second parcel's locations must still yield a successor")

    def test_the_repair_command_does_not_probe_for_subdivisions(self) -> None:
        """It re-homes everything the old outline captured itself, so a split probe is pure cost."""
        with mock.patch(_CHAIN, _Chain):
            call_command("repair_place_boundaries", "--all", stdout=StringIO(), stderr=StringIO())

        self.assertEqual(_Chain.calls, 1, "the repair ran the chain for more than the one coordinate it re-resolves")


class ScatteredSubdivisionProbeCapTests(TestCase):
    """A domain whose every location sits in its own new parcel still costs a bounded number of chain runs."""

    def setUp(self) -> None:
        super().setUp()
        _Chain.calls = 0
        oversized = baker.make(Place, kind=PlaceKind.PARCEL, geometry=_box(WEST, SOUTH, 0.06), area_sqm=8_300_000.0)
        oversized.domain_root = oversized
        oversized.save()
        self.locations = [
            baker.make(
                Location, latitude=SOUTH + 0.005 + row * 0.01, longitude=WEST + 0.005 + col * 0.01, place=oversized
            )
            for col in range(6)
            for row in range(6)
        ]

    def test_probing_stops_at_the_cap(self) -> None:
        with (
            mock.patch(_CHAIN, _Chain),
            mock.patch("urbanlens.dashboard.services.places.splits.process_split") as split,
        ):
            provisioning.ensure_place_for_location(self.locations[0], force=True)

        # One run resolves the triggering coordinate; the rest are successor probes.
        self.assertLessEqual(_Chain.calls, 1 + provisioning.MAX_SUBDIVISION_PROBES)
        split.assert_called_once()
        self.assertGreaterEqual(len(split.call_args.args[1]), 2)

    def test_the_nearest_neighbours_are_probed_first(self) -> None:
        probed: list[tuple[float, float]] = []

        class _Recording(_Chain):
            def get_boundaries(
                self, latitude: float, longitude: float, *, name: str | None = None
            ) -> ResolvedBoundaries:
                probed.append((latitude, longitude))
                return super().get_boundaries(latitude, longitude, name=name)

        with mock.patch(_CHAIN, _Recording), mock.patch("urbanlens.dashboard.services.places.splits.process_split"):
            provisioning.ensure_place_for_location(self.locations[0], force=True)

        origin = self.locations[0]
        shrink = math.cos(math.radians(float(origin.latitude)))
        distances = [
            round((lat - float(origin.latitude)) ** 2 + ((lng - float(origin.longitude)) * shrink) ** 2, 9)
            for lat, lng in probed[1:]
        ]
        self.assertEqual(distances, sorted(distances))
