"""Overture building places move from REData's drifting content-hash refs to ``overture:<gers_id>`` (REData P98)."""

from __future__ import annotations

from io import StringIO
import json
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import CommandError, call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError
from urbanlens.dashboard.services.places import overture_refs
from urbanlens.dashboard.services.places.provisioning import ensure_building_places

from .place_helpers import make_place

_STABLE = "overture:08b2a100d2c8dfff0200f8b4d3e8a1c7"
_OTHER_STABLE = "overture:08b2a100d2c8dfff0200f8b4d3e8ffff"
_OLD = "overture:aaaaaaaaaaaa"
_NEW = "overture:bbbbbbbbbbbb"


def _square(x: float, y: float, size: float = 0.1) -> MultiPolygon:
    return MultiPolygon(Polygon(((x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y))), srid=4326)


def _geojson(shape: MultiPolygon) -> dict[str, Any]:
    return json.loads(shape[0].geojson)


def _record(ref: str, shape: MultiPolygon, *, stable_ref: str | None = None, name: str = "Mill") -> dict[str, Any]:
    record: dict[str, Any] = {
        "ref": ref,
        "name": name,
        "geometry": _geojson(shape),
        "latitude": shape.centroid.y,
        "longitude": shape.centroid.x,
    }
    if stable_ref is not None:
        record["stable_ref"] = stable_ref
    return record


class FakeResolver(overture_refs.RedataResolver):
    """REData's ``/buildings/resolve/`` answers from a table; ``None`` for a ref means REData cannot be asked."""

    def __init__(self, table: dict[str, dict[str, Any] | None] | None = None) -> None:
        super().__init__()
        self.table = table or {}
        self.asked: list[str] = []

    def answer(self, ref: str) -> dict[str, Any] | None:
        self.asked.append(ref)
        if ref in self.table:
            return self.table[ref]
        return {"ref": ref, "status": "unknown", "stable_ref": None, "candidates": []}


def _resolved(stable: str) -> dict[str, Any]:
    return {"status": "resolved", "stable_ref": stable, "candidates": []}


class _PlacesTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.parcel = make_place(PlaceKind.PARCEL, _square(0.0, 0.0, 1.0), name="campus")
        Place.objects.filter(pk=self.parcel.pk).update(
            provider="redata", provider_key="11111111-1111-1111-1111-111111111111"
        )
        self.parcel.refresh_from_db()

    def _building_place(self, key: str, shape: MultiPolygon | None) -> Place:
        place = make_place(PlaceKind.BUILDING, shape, parent=self.parcel, name=key)
        Place.objects.filter(pk=place.pk).update(provider="redata", provider_key=key)
        place.refresh_from_db()
        return place


class ProvisioningTests(_PlacesTestCase):
    def _ensure(self, records: list[dict[str, Any]], resolver: FakeResolver | None = None) -> dict[int, Place]:
        with mock.patch.object(overture_refs, "RedataResolver", return_value=resolver or FakeResolver()):
            return ensure_building_places(self.parcel, records, provider="redata")

    def test_a_new_overture_building_is_filed_under_its_stable_ref(self) -> None:
        places = self._ensure([_record(_NEW, _square(0.2, 0.2), stable_ref=_STABLE)])
        self.assertEqual(places[0].provider_key, _STABLE)

    def test_a_place_on_the_buildings_own_legacy_ref_is_rekeyed_not_duplicated(self) -> None:
        old = self._building_place(_NEW, _square(0.2, 0.2))
        plan = baker.make(Floorplan, place=old, building_ref=_NEW)
        pin = baker.make(
            Pin,
            auto_nested_buildings=[
                {"latitude": 0.25, "longitude": 0.25, "ref": _NEW},
                {"latitude": 1, "longitude": 1, "ref": "cris:1"},
            ],
        )

        places = self._ensure([_record(_NEW, _square(0.2, 0.2), stable_ref=_STABLE)])

        self.assertEqual(places[0].pk, old.pk)
        self.assertEqual(Place.objects.get(pk=old.pk).provider_key, _STABLE)
        self.assertEqual(Floorplan.objects.get(pk=plan.pk).building_ref, _STABLE)
        self.assertEqual(
            [entry["ref"] for entry in Pin.objects.get(pk=pin.pk).auto_nested_buildings], [_STABLE, "cris:1"]
        )
        self.assertEqual(Place.objects.filter(kind=PlaceKind.BUILDING).count(), 1)

    def test_a_drifted_place_is_found_through_resolve_and_rekeyed(self) -> None:
        old = self._building_place(_OLD, _square(0.2, 0.2))
        places = self._ensure(
            [_record(_NEW, _square(0.21, 0.2), stable_ref=_STABLE)], FakeResolver({_OLD: _resolved(_STABLE)})
        )
        self.assertEqual(places[0].pk, old.pk)
        self.assertEqual(Place.objects.get(pk=old.pk).provider_key, _STABLE)

    def test_a_drifted_place_resolving_elsewhere_is_left_alone(self) -> None:
        old = self._building_place(_OLD, _square(0.2, 0.2))
        self._ensure(
            [_record(_NEW, _square(0.21, 0.2), stable_ref=_STABLE)], FakeResolver({_OLD: _resolved(_OTHER_STABLE)})
        )
        self.assertEqual(Place.objects.get(pk=old.pk).provider_key, _OLD)
        self.assertTrue(Place.objects.filter(provider_key=_STABLE).exists())

    def test_an_answer_from_before_stable_ref_finds_the_rekeyed_place(self) -> None:
        current = self._building_place(_STABLE, _square(0.2, 0.2))
        places = self._ensure([_record(_NEW, _square(0.21, 0.2))], FakeResolver({_NEW: _resolved(_STABLE)}))
        self.assertEqual(places[0].pk, current.pk)
        self.assertEqual(Place.objects.filter(kind=PlaceKind.BUILDING).count(), 1)

    def test_a_redata_that_cannot_resolve_keeps_the_old_behaviour(self) -> None:
        places = self._ensure([_record(_NEW, _square(0.2, 0.2))], FakeResolver({_NEW: None}))
        self.assertEqual(places[0].provider_key, _NEW)

    def test_other_refs_never_ask_redata(self) -> None:
        resolver = FakeResolver()
        places = self._ensure(
            [_record("cris:02714.000098", _square(0.2, 0.2), stable_ref="cris:02714.000098")], resolver
        )
        self.assertEqual(places[0].provider_key, "cris:02714.000098")
        self.assertEqual(resolver.asked, [])

    def test_the_resolver_rides_out_one_failure_and_stops_after_three_in_a_row(self) -> None:
        resolver = overture_refs.RedataResolver()
        down = PropertyRecordsUnavailableError("source_error", "down")
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway:
            gateway.return_value.resolve_building_ref.side_effect = [down, _resolved(_STABLE), down, down, down]
            self.assertIsNone(resolver(_OLD))
            self.assertEqual(resolver(_NEW), _STABLE)
            for ref in (
                "overture:cccccccccccc",
                "overture:dddddddddddd",
                "overture:eeeeeeeeeeee",
                "overture:ffffffffffff",
            ):
                self.assertIsNone(resolver(ref))
        self.assertEqual(gateway.return_value.resolve_building_ref.call_count, 5)
        self.assertTrue(resolver.unavailable)
        self.assertEqual(resolver.failed, 4)


class MigrationTests(_PlacesTestCase):
    def _migrate(
        self,
        table: dict[str, dict[str, Any] | None],
        *,
        apply: bool = True,
        buildings: list[dict[str, Any]] | None = None,
    ) -> overture_refs.MigrationReport:
        def fetch(uuid: str) -> list[dict[str, Any]]:
            if buildings is None:
                raise PropertyRecordsUnavailableError("source_error", "down")
            return buildings

        return overture_refs.migrate_legacy_refs(apply=apply, resolver=FakeResolver(table), fetch_buildings=fetch)

    def test_a_resolved_place_moves_with_its_floorplans_and_a_dry_run_moves_nothing(self) -> None:
        place = self._building_place(_OLD, _square(0.2, 0.2))
        baker.make(Floorplan, place=place, building_ref=_OLD)
        dry = self._migrate({_OLD: _resolved(_STABLE)}, apply=False)
        self.assertEqual([(row.outcome, row.new) for row in dry.places], [(overture_refs.RESOLVED, _STABLE)])
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _OLD)

        self._migrate({_OLD: _resolved(_STABLE)})
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _STABLE)
        self.assertEqual(Floorplan.objects.get().building_ref, _STABLE)
        self.assertEqual(self._migrate({}).places, [])

    def test_an_unrecorded_ref_falls_back_to_one_footprint_match(self) -> None:
        place = self._building_place(_OLD, _square(0.2, 0.2))
        report = self._migrate(
            {},
            buildings=[
                _record(_NEW, _square(0.205, 0.2), stable_ref=_STABLE),
                _record("overture:cccccccccccc", _square(0.6, 0.6), stable_ref=_OTHER_STABLE),
            ],
        )
        self.assertEqual([(row.outcome, row.new) for row in report.places], [(overture_refs.FOOTPRINT, _STABLE)])
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _STABLE)

    def test_two_footprint_matches_are_ambiguous_and_leave_the_place(self) -> None:
        place = self._building_place(_OLD, _square(0.2, 0.2))
        report = self._migrate(
            {},
            buildings=[
                _record(_NEW, _square(0.205, 0.2), stable_ref=_STABLE),
                _record("overture:cccccccccccc", _square(0.2, 0.205), stable_ref=_OTHER_STABLE),
            ],
        )
        self.assertEqual(report.places[0].outcome, overture_refs.AMBIGUOUS)
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _OLD)

    def test_a_contained_wing_is_not_a_footprint_match(self) -> None:
        self._building_place(_OLD, _square(0.2, 0.2, 0.3))
        report = self._migrate({}, buildings=[_record(_NEW, _square(0.21, 0.21, 0.05), stable_ref=_STABLE)])
        self.assertEqual(report.places[0].outcome, overture_refs.NO_MATCH)

    def test_a_wing_inside_an_overture_envelope_is_ambiguous_not_moved_onto_it(self) -> None:
        wing = self._building_place(_OLD, _square(0.2, 0.2, 0.04))
        buildings = [
            _record("county:wing-1", _square(0.2, 0.2, 0.04), stable_ref="county:wing-1"),
            _record(_NEW, _square(0.16, 0.16, 0.12), stable_ref=_STABLE),
        ]
        report = self._migrate({}, buildings=buildings)
        self.assertEqual(report.places[0].outcome, overture_refs.AMBIGUOUS)
        self.assertEqual(Place.objects.get(pk=wing.pk).provider_key, _OLD)

    def test_a_place_now_another_sources_building_is_not_moved(self) -> None:
        place = self._building_place(_OLD, _square(0.2, 0.2))
        report = self._migrate({}, buildings=[_record("cris:1", _square(0.2, 0.2), stable_ref="cris:1")])
        self.assertEqual((report.places[0].outcome, report.places[0].detail), (overture_refs.NO_MATCH, "cris:1"))
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _OLD)

    def test_a_building_still_served_under_the_places_ref_matches_whatever_its_shape(self) -> None:
        courtyard = MultiPolygon(
            Polygon(
                (
                    (0.2, 0.2),
                    (0.5, 0.2),
                    (0.5, 0.5),
                    (0.45, 0.5),
                    (0.45, 0.25),
                    (0.25, 0.25),
                    (0.25, 0.5),
                    (0.2, 0.5),
                    (0.2, 0.2),
                )
            ),
            srid=4326,
        )
        place = self._building_place(_OLD, courtyard)
        report = self._migrate({}, buildings=[_record(_OLD, courtyard, stable_ref=_STABLE)])
        self.assertEqual([(row.outcome, row.new) for row in report.places], [(overture_refs.FOOTPRINT, _STABLE)])
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _STABLE)

    def test_a_dry_run_reports_what_apply_will_do_when_two_places_resolve_alike(self) -> None:
        self._building_place(_OLD, _square(0.2, 0.2))
        self._building_place(_NEW, _square(0.5, 0.5))
        table = {_OLD: _resolved(_STABLE), _NEW: _resolved(_STABLE)}
        dry = [row.outcome for row in self._migrate(table, apply=False).places]
        applied = [row.outcome for row in self._migrate(table).places]
        self.assertEqual(dry, applied)
        self.assertEqual(applied, [overture_refs.RESOLVED, overture_refs.DUPLICATE])

    def test_ambiguous_unavailable_and_duplicate_places_are_left(self) -> None:
        ambiguous = self._building_place(_OLD, _square(0.2, 0.2))
        unavailable = self._building_place(_NEW, _square(0.5, 0.5))
        duplicate = self._building_place("overture:cccccccccccc", _square(0.7, 0.7))
        self._building_place(_STABLE, _square(0.7, 0.7))
        report = self._migrate(
            {
                _OLD: {"status": "ambiguous", "stable_ref": None, "candidates": [_STABLE, _OTHER_STABLE]},
                _NEW: None,
                "overture:cccccccccccc": _resolved(_STABLE),
            },
        )
        outcomes = {row.place: row.outcome for row in report.places}
        self.assertEqual(
            outcomes,
            {
                ambiguous.pk: overture_refs.AMBIGUOUS,
                unavailable.pk: overture_refs.UNAVAILABLE,
                duplicate.pk: overture_refs.DUPLICATE,
            },
        )
        self.assertEqual(
            sorted(Place.objects.filter(provider_key__startswith="overture:").values_list("provider_key", flat=True)),
            sorted([_OLD, _NEW, "overture:cccccccccccc", _STABLE]),
        )

    def test_no_redata_parcel_means_no_footprint_match(self) -> None:
        orphan = make_place(PlaceKind.BUILDING, _square(0.2, 0.2))
        Place.objects.filter(pk=orphan.pk).update(provider="redata", provider_key=_OLD)
        report = self._migrate({}, buildings=[_record(_NEW, _square(0.2, 0.2), stable_ref=_STABLE)])
        self.assertEqual(report.places[0].outcome, overture_refs.NO_MATCH)

    def test_floorplans_and_swept_buildings_no_place_holds_are_moved(self) -> None:
        baker.make(Floorplan, building_ref=_OLD)
        pin = baker.make(Pin, auto_nested_buildings=[{"latitude": 0.1, "longitude": 0.1, "ref": _NEW}])
        report = self._migrate({_OLD: _resolved(_STABLE), _NEW: _resolved(_OTHER_STABLE)})
        self.assertEqual((report.floorplans, report.pins), ({_OLD: _STABLE}, {_NEW: _OTHER_STABLE}))
        self.assertEqual(Floorplan.objects.get().building_ref, _STABLE)
        self.assertEqual(Pin.objects.get(pk=pin.pk).auto_nested_buildings[0]["ref"], _OTHER_STABLE)
        self.assertEqual(
            overture_refs.legacy_refs_remaining(),
            {"Place.provider_key": 0, "Floorplan.building_ref": 0, "Pin.auto_nested_buildings": 0},
        )

    def test_check_counts_every_store_and_fails_until_none_remain(self) -> None:
        self._building_place(_OLD, _square(0.2, 0.2))
        baker.make(Floorplan, building_ref=_NEW)
        baker.make(Pin, auto_nested_buildings=[{"latitude": 0.1, "longitude": 0.1, "ref": _NEW}])
        self.assertEqual(
            overture_refs.legacy_refs_remaining(),
            {"Place.provider_key": 1, "Floorplan.building_ref": 1, "Pin.auto_nested_buildings": 1},
        )
        with self.assertRaises(CommandError):
            call_command("rekey_overture_places", "--check", stdout=StringIO())
        Place.objects.filter(provider_key=_OLD).update(provider_key=_STABLE)
        Floorplan.objects.update(building_ref=_STABLE)
        Pin.objects.update(auto_nested_buildings=[])
        call_command("rekey_overture_places", "--check", stdout=StringIO())

    def test_the_command_reports_without_writing_by_default(self) -> None:
        place = self._building_place(_OLD, _square(0.2, 0.2))
        out = StringIO()
        with mock.patch.object(overture_refs, "RedataResolver", return_value=FakeResolver({_OLD: _resolved(_STABLE)})):
            call_command("rekey_overture_places", stdout=out)
        self.assertIn(f"would re-key to {_STABLE} (resolved)", out.getvalue())
        self.assertEqual(Place.objects.get(pk=place.pk).provider_key, _OLD)


class ClusterKeyTests(TestCase):
    def test_swept_buildings_remember_the_stable_ref(self) -> None:
        from urbanlens.dashboard.services.pins.building_clusters import BuildingCluster

        members = [{"ref": _OLD, "stable_ref": _STABLE}, {"ref": "cris:1"}]
        cluster = BuildingCluster(representative=members[0], latitude=0.0, longitude=0.0, members=members)
        self.assertEqual(cluster.keys, {_STABLE, "cris:1"})
        self.assertEqual(cluster.refs, {_OLD, "cris:1"})
