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


class BlueprintExtractionTests(TestCase):
    """The trace pipeline: model output in image space → world coordinates.

    The vision model is always mocked - what's under test is the mapping
    through the overlay's corner georeference and the tolerance for
    malformed model output.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        # A 0.01° square: NW at (-73.93, 41.734), SE at (-73.92, 41.724).
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        self.overlay = baker.make(
            MapImageOverlay,
            profile=self.profile,
            image=baker.make("dashboard.Image"),
            nw_latitude=41.734, nw_longitude=-73.93,
            ne_latitude=41.734, ne_longitude=-73.92,
            se_latitude=41.724, se_longitude=-73.92,
            sw_latitude=41.724, sw_longitude=-73.93,
        )

    def _extract(self, structure):
        from urbanlens.dashboard.services.floorplans import extraction

        with mock.patch.object(extraction, "_structure_from_model", return_value=structure), \
             mock.patch.object(type(self.overlay), "image", new_callable=mock.PropertyMock) as image:
            image.return_value = mock.Mock(file=mock.Mock(read=mock.Mock(return_value=b"png")))
            return extraction.extract_overlay_structure(self.overlay)

    def test_image_corners_map_to_overlay_corners(self) -> None:
        """(0,0) is the sheet's top-left = the overlay's NW corner."""
        result = self._extract({"doors": [{"point": [0.0, 0.0]}, {"point": [1.0, 1.0]}]})

        first, second = (element["geometry"]["coordinates"] for element in result["elements"])
        self.assertAlmostEqual(first[0], -73.93, places=6)
        self.assertAlmostEqual(first[1], 41.734, places=6)
        self.assertAlmostEqual(second[0], -73.92, places=6)
        self.assertAlmostEqual(second[1], 41.724, places=6)

    def test_the_center_maps_to_the_center(self) -> None:
        result = self._extract({"doors": [{"point": [0.5, 0.5]}]})

        lng, lat = result["elements"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lng, -73.925, places=6)
        self.assertAlmostEqual(lat, 41.729, places=6)

    def test_rooms_close_their_rings(self) -> None:
        result = self._extract({"rooms": [{"name": "Ward B", "polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]]}]})

        ring = result["rooms"][0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1], "a GeoJSON polygon ring must be closed")
        self.assertEqual(result["rooms"][0]["name"], "Ward B")

    def test_malformed_model_output_is_skipped_not_fatal(self) -> None:
        result = self._extract({
            "rooms": [{"polygon": [[0.1, 0.1], [0.2, 0.2]]}, "garbage", {"polygon": None}],
            "walls": [{"line": [[0.1, "x"], [0.2, 0.2]]}],
            "doors": [{"point": [1.5, -0.2]}],
        })

        self.assertEqual(result["rooms"], [], "a two-point polygon is not a room")
        self.assertEqual([e["kind"] for e in result["elements"]], ["door"], "out-of-range coordinates clamp rather than discard")

    def test_a_non_floorplan_answer_is_empty_not_none(self) -> None:
        self.assertEqual(self._extract({}), {})

    def test_ai_unavailable_is_none(self) -> None:
        self.assertIsNone(self._extract(None))


class FloorplanEndpointTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        self.place = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel)
        location = baker.make(Location, latitude=41.733, longitude=-73.928, place=self.place)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, slug="hrsh-admin")

    def test_no_plan_is_a_204_not_an_error(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/json/")

        self.assertEqual(response.status_code, 204)

    def test_save_then_get_round_trips(self) -> None:
        import json as jsonlib

        document = {"name": "As built", "floors": [{"level": 0, "name": "Ground", "rooms": [{"name": "Boiler room", "geometry": _SQUARE}]}]}
        save = self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps(document),
            content_type="application/json",
        )
        self.assertEqual(save.status_code, 200, save.content)

        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/json/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["origin"], "local")
        self.assertEqual(body["floors"][0]["rooms"][0]["name"], "Boiler room")

    def test_someone_elses_pin_is_a_404(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        other_location = baker.make(Location, latitude=41.9, longitude=-73.9, place=self.place)
        other = baker.make(Pin, profile=baker.make(User).profile, location=other_location, parent_pin=None, slug="not-mine")

        self.assertEqual(self.client.get(f"/dashboard/map/pin/{other.slug}/floorplan/json/").status_code, 404)

    def test_a_bad_geometry_is_a_400_naming_the_defect(self) -> None:
        import json as jsonlib

        document = {"floors": [{"level": 0, "elements": [{"kind": "wall", "geometry": {"type": "LineString", "coordinates": "junk"}}]}]}
        response = self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps(document),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("geometry", response.json()["error"])

    def test_the_editor_page_renders(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "floorplan-map")
