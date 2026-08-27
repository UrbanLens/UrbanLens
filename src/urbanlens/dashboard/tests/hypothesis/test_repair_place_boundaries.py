"""The boundary repair pass must sweep the area the *old* geometry covered.

`provision_places_for_coordinate` calls `resolve_locations_in(place.geometry)`
whenever it stores an outline, so an oversized parcel re-homed pins across a
wide area onto itself. Correcting the outline does not undo that.

`resolve_locations_in` re-resolves each location it visits authoritatively, but
its scope is `Location.objects.filter(point__within=polygon)`. Sweeping with the
corrected (smaller) polygon therefore visits only the locations still inside it
and leaves every wrongly-captured location outside it attached to the wrong
place - a fix that looks applied and isn't. The command must capture the old
geometry before re-provisioning and sweep with that.

That ordering is the whole point of the command, so it is what these tests pin.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind

_COMMAND = "repair_place_boundaries"
_MODULE = "urbanlens.dashboard.management.commands.repair_place_boundaries"


def _square(size: float, *, west: float = -73.92794, south: float = 41.73332) -> MultiPolygon:
    ring = ((west, south), (west + size, south), (west + size, south + size), (west, south + size), (west, south))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class RepairPlaceBoundariesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._seq = 0

    def _parcel(self, *, generated_at=None, size: float = 1.0, area_sqm: float = 1_000_000.0, with_location: bool = True) -> Place:
        self._seq += 1
        place = baker.make(
            Place,
            kind=PlaceKind.PARCEL,
            geometry=_square(size, south=41.73332 + self._seq),
            geometry_generated_at=generated_at,
            area_sqm=area_sqm,
        )
        if with_location:
            baker.make(Location, latitude=41.73332 + self._seq, longitude=-73.92794, place=place)
        return place

    def _run(self, *args) -> str:
        out = StringIO()
        call_command(_COMMAND, *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_the_sweep_uses_the_geometry_captured_before_re_resolution(self) -> None:
        """The defect this command exists to repair is outside the corrected polygon."""
        place = self._parcel(size=1.0)
        old_geometry = place.geometry
        corrected = _square(0.001)

        def shrink(location, *, force=False, name=None):
            Place.objects.filter(pk=place.pk).update(geometry=corrected, area_sqm=100.0)
            return place

        with mock.patch(f"{_MODULE}.ensure_place_for_location", side_effect=shrink), mock.patch(f"{_MODULE}.resolution") as resolution:
            resolution.resolve_locations_in.return_value = 3
            self._run()

        swept = resolution.resolve_locations_in.call_args.args[0]
        self.assertAlmostEqual(swept.area, old_geometry.area, places=9, msg="swept the corrected polygon, leaving wrongly re-homed pins outside it untouched")
        self.assertGreater(swept.area, corrected.area, "the sweep must cover the area the bad boundary captured")

    def test_re_resolution_is_forced(self) -> None:
        """Without force the chain short-circuits on the place that is already there."""
        self._parcel()

        with mock.patch(f"{_MODULE}.ensure_place_for_location") as ensure, mock.patch(f"{_MODULE}.resolution"):
            self._run()

        self.assertTrue(ensure.call_args.kwargs.get("force"), "re-provisioning must bypass the freshness gate")

    def test_only_never_generated_parcels_are_repaired_by_default(self) -> None:
        backfilled = self._parcel(generated_at=None)
        self._parcel(generated_at=timezone.now())

        with mock.patch(f"{_MODULE}.ensure_place_for_location") as ensure, mock.patch(f"{_MODULE}.resolution"):
            self._run()

        swept_locations = [call.args[0].place_id for call in ensure.call_args_list]
        self.assertEqual(swept_locations, [backfilled.pk])

    def test_all_includes_already_generated_parcels(self) -> None:
        self._parcel(generated_at=None)
        self._parcel(generated_at=timezone.now())

        with mock.patch(f"{_MODULE}.ensure_place_for_location") as ensure, mock.patch(f"{_MODULE}.resolution"):
            self._run("--all")

        self.assertEqual(ensure.call_count, 2)

    def test_a_parcel_with_no_located_row_is_skipped_not_failed(self) -> None:
        """The chain answers a coordinate; there is nothing to re-provision from."""
        self._parcel(with_location=False)

        with mock.patch(f"{_MODULE}.ensure_place_for_location") as ensure, mock.patch(f"{_MODULE}.resolution"):
            output = self._run()

        ensure.assert_not_called()
        self.assertIn("skipped 1", output)

    def test_dry_run_neither_fetches_nor_sweeps(self) -> None:
        self._parcel()

        with mock.patch(f"{_MODULE}.ensure_place_for_location") as ensure, mock.patch(f"{_MODULE}.resolution") as resolution:
            output = self._run("--dry-run")

        ensure.assert_not_called()
        resolution.resolve_locations_in.assert_not_called()
        self.assertIn("would re-resolve", output)

    def test_largest_parcels_are_repaired_first(self) -> None:
        """Area is the symptom, so a --limit run should take the worst offenders."""
        self._parcel(area_sqm=10.0)
        biggest = self._parcel(area_sqm=9_000_000.0)

        with mock.patch(f"{_MODULE}.ensure_place_for_location") as ensure, mock.patch(f"{_MODULE}.resolution"):
            self._run("--limit", "1")

        self.assertEqual(ensure.call_args.args[0].place_id, biggest.pk)

    def test_one_failure_does_not_abort_the_run(self) -> None:
        from django.db import DatabaseError

        self._parcel()
        self._parcel()

        with mock.patch(f"{_MODULE}.ensure_place_for_location", side_effect=[DatabaseError("boom"), mock.DEFAULT]), mock.patch(f"{_MODULE}.resolution"):
            output = self._run()

        self.assertIn("failed 1", output)
        self.assertIn("repaired 1", output)
