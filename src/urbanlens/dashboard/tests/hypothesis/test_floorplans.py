"""Floorplans: versioned interior structure, absent by default.

The document format is REData's, verbatim, so one editor parses both origins.
The load-bearing properties:

- Version resolution by date: the undated baseline is in force until the
  first dated version; no date means the newest; a building with no plan
  answers None from one indexed query (the common case must stay free).
- The document round-trips through REData's shape: source/reference pools
  with uuid indirection, elements per floor and plan-level, ``mounted_on``
  across the document, locks under elements. Round-tripped uuids update in
  place, omission deletes, labels resolve only against the saver's own.
- Resolution prefers the user's local plan and falls back to REData's
  two-step lookup, whose absence in every form is a quiet None.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.floorplans.model import (
    Floorplan,
    FloorplanElement,
    FloorplanFloor,
    FloorplanLock,
    FloorplanRoom,
)
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.floorplans.resolution import resolve_document
from urbanlens.dashboard.services.floorplans.serialization import document_for, save_document

_SQUARE = {"type": "Polygon", "coordinates": [[[-73.93, 41.733], [-73.929, 41.733], [-73.929, 41.734], [-73.93, 41.734], [-73.93, 41.733]]]}
_LINE = {"type": "LineString", "coordinates": [[-73.93, 41.733], [-73.929, 41.733]]}
_POINT = {"type": "Point", "coordinates": [-73.9295, 41.733]}


def _building(parcel: Place | None = None) -> Place:
    return baker.make(Place, kind=PlaceKind.BUILDING, provider="redata", provider_key="cris:1", parent=parcel)


class FloorplanVersioningTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.place = _building()

    def test_a_building_with_no_plan_answers_none(self) -> None:
        self.assertIsNone(Floorplan.objects.at(self.place))

    def test_no_date_returns_the_newest_version(self) -> None:
        baker.make(Floorplan, place=self.place, valid_from=None, name="baseline")
        baker.make(Floorplan, place=self.place, valid_from=datetime.date(1962, 5, 1), name="after fire")

        self.assertEqual(Floorplan.objects.at(self.place).name, "after fire")

    def test_a_date_before_the_first_dated_version_returns_the_baseline(self) -> None:
        baker.make(Floorplan, place=self.place, valid_from=None, name="baseline")
        baker.make(Floorplan, place=self.place, valid_from=datetime.date(1962, 5, 1), name="after fire")

        self.assertEqual(Floorplan.objects.at(self.place, datetime.date(1950, 1, 1)).name, "baseline")

    def test_a_date_between_versions_returns_the_one_then_in_force(self) -> None:
        baker.make(Floorplan, place=self.place, valid_from=datetime.date(1900, 1, 1), name="as built")
        baker.make(Floorplan, place=self.place, valid_from=datetime.date(1962, 5, 1), name="after fire")
        baker.make(Floorplan, place=self.place, valid_from=datetime.date(1990, 1, 1), name="sealed")

        self.assertEqual(Floorplan.objects.at(self.place, datetime.date(1975, 6, 15)).name, "after fire")


class FloorplanDocumentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile)

    def _document(self) -> dict:
        return {
            "name": "As built",
            "source_pool": [{"uuid": "src-1", "title": "HABS sheet 4", "author": "HABS", "url": "https://loc.gov/habs/4"}],
            "reference_pool": [{"uuid": "ref-1", "kind": "photo", "title": "Boiler room, 2019", "url": "https://example.test/p.jpg"}],
            "floors": [
                {
                    "level": 0,
                    "name": "Ground",
                    "geometry": _SQUARE,
                    "rooms": [{"uuid": "room-1", "name": "Boiler room", "geometry": _SQUARE, "condition": "collapsed", "references": ["ref-1"]}],
                    "elements": [
                        {"uuid": "wall-1", "kind": "wall", "geometry": _LINE, "material": "brick", "source": "src-1"},
                        {
                            "uuid": "door-1",
                            "kind": "door",
                            "geometry": _POINT,
                            "room": "room-1",
                            "mounted_on": "wall-1",
                            "locks": [
                                {"name": "padlock", "key_attributes": {"brand": "Abus", "keyway": "AB1"}},
                                {"name": "chain", "key_attributes": {}},
                            ],
                        },
                    ],
                },
            ],
            "elements": [{"uuid": "key-1", "kind": "key", "name": "Boiler room key", "attributes": {"cut": "worn"}}],
        }

    def test_a_document_round_trips_in_redata_shape(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)

        out = document_for(self.floorplan)

        floor = out["floors"][0]
        self.assertEqual(floor["rooms"][0]["name"], "Boiler room")
        wall = next(e for e in floor["elements"] if e["kind"] == "wall")
        door = next(e for e in floor["elements"] if e["kind"] == "door")
        self.assertEqual(wall["material"], "brick")
        self.assertEqual(door["mounted_on"], wall["uuid"], "mounted_on must survive as uuid indirection")
        self.assertEqual(door["room"], floor["rooms"][0]["uuid"])
        self.assertEqual({lock["name"] for lock in door["locks"]}, {"padlock", "chain"})
        self.assertEqual(next(lock for lock in door["locks"] if lock["name"] == "padlock")["key_attributes"]["brand"], "Abus")
        # Pools carry provenance; items point into them by uuid.
        self.assertEqual(out["source_pool"][0]["title"], "HABS sheet 4")
        self.assertEqual(wall["source"], out["source_pool"][0]["uuid"])
        self.assertEqual(floor["rooms"][0]["references"], [out["reference_pool"][0]["uuid"]])
        # Plan-level elements (a key has no floor and no geometry).
        self.assertEqual(out["elements"][0]["kind"], "key")
        self.assertIsNone(out["elements"][0]["geometry"])

    def test_round_tripped_uuids_update_in_place(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)
        out = document_for(self.floorplan)
        room_uuid = out["floors"][0]["rooms"][0]["uuid"]
        out["floors"][0]["rooms"][0]["name"] = "Ward B"

        save_document(self.floorplan, out, profile=self.profile)

        self.assertEqual(FloorplanRoom.objects.count(), 1, "a round-tripped uuid must update, not duplicate")
        self.assertEqual(str(FloorplanRoom.objects.get().uuid), room_uuid)
        self.assertEqual(FloorplanRoom.objects.get().name, "Ward B")

    def test_omitted_items_are_deleted(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)
        out = document_for(self.floorplan)
        out["floors"][0]["rooms"] = []

        save_document(self.floorplan, out, profile=self.profile)

        self.assertEqual(FloorplanRoom.objects.count(), 0)
        self.assertEqual(FloorplanElement.objects.filter(kind="wall").count(), 1, "removing rooms must not touch walls")

    def test_locks_can_be_absent_entirely(self) -> None:
        document = self._document()
        document["floors"][0]["elements"][1]["locks"] = []

        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(FloorplanLock.objects.count(), 0)

    def test_labels_resolve_only_against_the_saving_profiles_own(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label

        mine = baker.make(Label, profile=self.profile, name="floorplan-hazard")
        theirs = baker.make(Label, profile=baker.make(User).profile, name="floorplan-theirs")
        document = self._document()
        document["floors"][0]["rooms"][0]["labels"] = [str(mine.uuid), str(theirs.uuid)]

        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual([label.pk for label in FloorplanRoom.objects.get().labels.all()], [mine.pk], "a document must not attach another user's label by guessing its uuid")

    def test_versions_and_dates_persist(self) -> None:
        document = {**self._document(), "valid_from": "1962-05-01", "floor_count": 4}
        document["floors"][0]["built_date"] = "1900-06-01"

        save_document(self.floorplan, document, profile=self.profile)

        self.floorplan.refresh_from_db()
        self.assertEqual(self.floorplan.valid_from, datetime.date(1962, 5, 1))
        self.assertEqual(self.floorplan.floor_count, 4)
        self.assertEqual(FloorplanFloor.objects.get().built_date, datetime.date(1900, 6, 1))


class FloorplanResolutionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL, provider="redata", provider_key="parcel-uuid-1")
        self.place = _building(self.parcel)

    def _configured(self):
        settings_path = "urbanlens.UrbanLens.settings.app.settings"
        return (
            mock.patch(f"{settings_path}.redata_api_url", "https://redata.example.test"),
            mock.patch(f"{settings_path}.redata_api_key", "k"),
        )

    def test_a_local_plan_wins(self) -> None:
        Floorplan.objects.create(place=self.place, profile=self.profile, name="mine")

        with mock.patch("urbanlens.dashboard.services.floorplans.resolution._redata_document") as upstream:
            document = resolve_document(self.place)

        self.assertEqual(document["origin"], "local")
        upstream.assert_not_called()

    def test_redata_fills_when_local_is_absent(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = [{"uuid": "fp-1", "building_ref": "cris:1"}]
            gateway.return_value.lookup_floorplan_document.return_value = {"name": "REData plan", "floors": []}

            document = resolve_document(self.place)

        self.assertEqual(document["origin"], "redata")
        self.assertEqual(document["name"], "REData plan")
        gateway.return_value.lookup_floorplans.assert_called_once_with("parcel-uuid-1", building_ref="cris:1", on_date=None)
        gateway.return_value.lookup_floorplan_document.assert_called_once_with("fp-1")

    def test_an_empty_summary_list_is_a_quiet_none(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = []

            self.assertIsNone(resolve_document(self.place))

        gateway.return_value.lookup_floorplan_document.assert_not_called()

    def test_upstream_trouble_is_never_load_bearing(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.side_effect = RuntimeError("boom")

            self.assertIsNone(resolve_document(self.place))

    def test_a_building_with_no_redata_parcel_never_calls_upstream(self) -> None:
        orphan = baker.make(Place, kind=PlaceKind.BUILDING, provider="redata", provider_key="cris:9", parent=None)

        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway:
            self.assertIsNone(resolve_document(orphan))

        gateway.assert_not_called()

    def test_the_date_flows_through_to_upstream(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = []

            resolve_document(self.place, on_date=datetime.date(1954, 1, 1))

        gateway.return_value.lookup_floorplans.assert_called_once_with("parcel-uuid-1", building_ref="cris:1", on_date="1954-01-01")
