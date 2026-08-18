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

import base64
import datetime
import itertools
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
#: Deliberately outside _SQUARE, for viewport-filter tests.
_FAR_SQUARE = {"type": "Polygon", "coordinates": [[[-73.90, 41.70], [-73.899, 41.70], [-73.899, 41.701], [-73.90, 41.701], [-73.90, 41.70]]]}
#: A minimal valid 1x1 PNG - real bytes, so the stored-file read path is exercised.
_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
)


_building_seq = itertools.count(1)


def _building(parcel: Place | None = None, *, provider_key: str = "") -> Place:
    """A building place. Provider keys are unique per provider, so vary them."""
    return baker.make(
        Place,
        kind=PlaceKind.BUILDING,
        provider="redata",
        provider_key=provider_key or f"cris:{next(_building_seq)}",
        parent=parcel,
    )


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
        self.place = _building(self.parcel, provider_key="cris:1")

    def _configured(self):
        settings_path = "urbanlens.UrbanLens.settings.app.settings"
        return (
            mock.patch(f"{settings_path}.redata_api_url", "https://redata.example.test"),
            mock.patch(f"{settings_path}.redata_api_key", "k"),
        )

    def test_a_local_plan_wins(self) -> None:
        Floorplan.objects.create(place=self.place, profile=self.profile, name="mine")

        with mock.patch("urbanlens.dashboard.services.floorplans.resolution._redata_document") as upstream:
            document = resolve_document(self.place, profile=self.profile)

        self.assertEqual(document["origin"], "local")
        upstream.assert_not_called()

    def test_another_users_plan_is_not_served(self) -> None:
        """A plan records doors, locks and what opens them - not shared data.

        Resolution is place-scoped, so without this every user who pinned the
        same building would receive whatever anyone else had traced.
        """
        Floorplan.objects.create(place=self.place, profile=baker.make(User).profile, name="theirs")

        with mock.patch("urbanlens.dashboard.services.floorplans.resolution._redata_document", return_value=None):
            self.assertIsNone(resolve_document(self.place, profile=self.profile))

    def test_redata_fills_when_local_is_absent(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = [{"uuid": "fp-1", "building_ref": "cris:1"}]
            gateway.return_value.lookup_floorplan_document.return_value = {"name": "REData plan", "floors": []}

            document = resolve_document(self.place, profile=self.profile)

        self.assertEqual(document["origin"], "redata")
        self.assertEqual(document["name"], "REData plan")
        gateway.return_value.lookup_floorplans.assert_called_once_with("parcel-uuid-1", building_ref="cris:1", on_date=None)
        gateway.return_value.lookup_floorplan_document.assert_called_once_with("fp-1")

    def test_an_empty_summary_list_is_a_quiet_none(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = []

            self.assertIsNone(resolve_document(self.place, profile=self.profile))

        gateway.return_value.lookup_floorplan_document.assert_not_called()

    def test_upstream_trouble_is_never_load_bearing(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.side_effect = RuntimeError("boom")

            self.assertIsNone(resolve_document(self.place, profile=self.profile))

    def test_a_building_with_no_redata_parcel_never_calls_upstream(self) -> None:
        orphan = baker.make(Place, kind=PlaceKind.BUILDING, provider="redata", provider_key="cris:9", parent=None)

        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway:
            self.assertIsNone(resolve_document(orphan, profile=self.profile))

        gateway.assert_not_called()

    def test_the_date_flows_through_to_upstream(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = []

            resolve_document(self.place, profile=self.profile, on_date=datetime.date(1954, 1, 1))

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
        from django.core.files.uploadedfile import SimpleUploadedFile

        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        sheet = baker.make("dashboard.Image")
        sheet.image.save("blueprint.png", SimpleUploadedFile("blueprint.png", _PNG_BYTES, content_type="image/png"), save=True)
        self.overlay = baker.make(
            MapImageOverlay,
            profile=self.profile,
            image=sheet,
            nw_latitude=41.734, nw_longitude=-73.93,
            ne_latitude=41.734, ne_longitude=-73.92,
            se_latitude=41.724, se_longitude=-73.92,
            sw_latitude=41.724, sw_longitude=-73.93,
        )

    def _extract(self, structure):
        """Run extraction with only the *model* mocked.

        Deliberately not mocking the image attribute: an earlier version of
        this test faked `overlay.image.file`, which does not exist (the field
        is `Image.image`), so it passed against code that would have raised in
        production. The stored file below is real, so the read path is under
        test too.
        """
        from urbanlens.dashboard.services.floorplans import extraction

        with mock.patch.object(extraction, "_structure_from_model", return_value=structure) as model:
            result = extraction.extract_overlay_structure(self.overlay)
        if structure is not None:
            self.assertEqual(model.call_args.args[0], _PNG_BYTES, "the overlay's own stored bytes must reach the model")
        return result

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

    def test_a_bad_number_is_a_400_naming_the_field(self) -> None:
        import json as jsonlib

        response = self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps({"floor_count": "several"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, "a non-numeric field must not reach the database as a 500")
        self.assertIn("floor_count", response.json()["error"])

    def test_a_bad_date_is_a_400(self) -> None:
        import json as jsonlib

        response = self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps({"floors": [{"level": 0, "built_date": "sometime in 1890"}]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

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


class FloorplanVersionSafetyTests(TestCase):
    """A save must never destroy a plan it was not editing.

    Floorplans are expensive hand work: hours of tracing. Two ways that work
    could have been lost - re-dating a loaded plan silently rewrote the
    version in force at the new date, and any user could write over any other
    user's plan for the same building, since resolution is place-scoped.
    Both now fork into a new version instead.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = _building()

    def _edit(self, *, version_uuid: str = "", on_date=None):
        from urbanlens.dashboard.services.floorplans.resolution import floorplan_for_editing

        return floorplan_for_editing(self.place, self.profile, version_uuid=version_uuid, on_date=on_date)

    def test_saving_a_loaded_plan_updates_it(self) -> None:
        mine = Floorplan.objects.create(place=self.place, profile=self.profile, name="mine")

        self.assertEqual(self._edit(version_uuid=str(mine.uuid)).pk, mine.pk)
        self.assertEqual(Floorplan.objects.count(), 1)

    def test_re_dating_without_a_uuid_creates_a_version_instead_of_overwriting(self) -> None:
        """The reported shape: a baseline must survive a dated save."""
        baseline = Floorplan.objects.create(place=self.place, profile=self.profile, name="as built", valid_from=None)

        created = self._edit(on_date=datetime.date(1962, 5, 1))

        self.assertNotEqual(created.pk, baseline.pk)
        baseline.refresh_from_db()
        self.assertIsNone(baseline.valid_from, "the baseline was re-dated instead of a new version being created")
        self.assertEqual(baseline.name, "as built")

    def test_another_users_plan_is_never_written_over(self) -> None:
        theirs = Floorplan.objects.create(place=self.place, profile=baker.make(User).profile, name="theirs")

        created = self._edit(version_uuid=str(theirs.uuid))

        self.assertNotEqual(created.pk, theirs.pk)
        self.assertEqual(created.profile_id, self.profile.pk)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "theirs")

    def test_a_redata_origin_document_forks_to_a_local_plan(self) -> None:
        """Its uuid is REData's, so it can never match a local row."""
        created = self._edit(version_uuid="11111111-1111-1111-1111-111111111111")

        self.assertEqual(created.profile_id, self.profile.pk)
        self.assertEqual(Floorplan.objects.count(), 1)

    def test_repeated_saves_of_one_plan_do_not_pile_up_versions(self) -> None:
        first = self._edit()

        again = self._edit(version_uuid=str(first.uuid))

        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Floorplan.objects.count(), 1)


class FloorplanVersionListingTests(TestCase):
    """The editor switches versions without a page reload, so the API lists them."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        self.place = baker.make(Place, kind=PlaceKind.BUILDING, parent=parcel)
        location = baker.make(Location, latitude=41.7331, longitude=-73.9281, place=self.place)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, slug="hrsh-kirkbride")
        self.baseline = Floorplan.objects.create(place=self.place, profile=self.user.profile, name="as built", valid_from=None)
        self.later = Floorplan.objects.create(place=self.place, profile=self.user.profile, name="after fire", valid_from=datetime.date(1962, 5, 1))

    def _get(self, query: str = ""):
        return self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/json/{query}")

    def test_the_response_lists_every_version_oldest_first(self) -> None:
        body = self._get().json()

        self.assertEqual([v["name"] for v in body["versions"]], ["as built", "after fire"])

    def test_a_specific_version_can_be_opened(self) -> None:
        body = self._get(f"?version={self.baseline.uuid}").json()

        self.assertEqual(body["uuid"], str(self.baseline.uuid))
        self.assertEqual(body["name"], "as built")

    def test_another_users_version_cannot_be_opened_by_uuid(self) -> None:
        theirs = Floorplan.objects.create(place=self.place, profile=baker.make(User).profile, name="theirs")

        response = self._get(f"?version={theirs.uuid}")

        self.assertEqual(response.status_code, 204, "a version uuid must not be a way to read someone else's plan")

    def test_versions_are_listed_only_for_their_owner(self) -> None:
        Floorplan.objects.create(place=self.place, profile=baker.make(User).profile, name="theirs")

        body = self._get().json()

        self.assertEqual([v["name"] for v in body["versions"]], ["as built", "after fire"])


class FloorplanDocumentOrderTests(TestCase):
    """Document order is the stored order, matching REData's sort_order.

    Without it, re-arranging items in an editor is lost on the next read: the
    rows come back in whatever order the database chose, so a plan the author
    ordered deliberately (a floor's walls traced in sequence, a door's locks
    outermost-first) reads back scrambled.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    def _rooms(self, names):
        return {"floors": [{"level": 0, "rooms": [{"name": name, "geometry": _SQUARE} for name in names]}]}

    def test_document_order_round_trips(self) -> None:
        save_document(self.floorplan, self._rooms(["c", "a", "b"]), profile=self.profile)

        out = document_for(self.floorplan)

        self.assertEqual([room["name"] for room in out["floors"][0]["rooms"]], ["c", "a", "b"])

    def test_re_ordering_in_a_later_save_sticks(self) -> None:
        save_document(self.floorplan, self._rooms(["c", "a", "b"]), profile=self.profile)
        out = document_for(self.floorplan)
        out["floors"][0]["rooms"].reverse()

        save_document(self.floorplan, out, profile=self.profile)

        again = document_for(self.floorplan)
        self.assertEqual([room["name"] for room in again["floors"][0]["rooms"]], ["b", "a", "c"])

    def test_elements_keep_their_document_order(self) -> None:
        document = {"floors": [{"level": 0, "elements": [
            {"kind": "wall", "name": "north", "geometry": _LINE},
            {"kind": "wall", "name": "east", "geometry": _LINE},
            {"kind": "door", "name": "main", "geometry": _POINT},
        ]}]}

        save_document(self.floorplan, document, profile=self.profile)

        out = document_for(self.floorplan)
        self.assertEqual([e["name"] for e in out["floors"][0]["elements"]], ["north", "east", "main"])


class FloorplanFeatureCollectionTests(TestCase):
    """The map-facing read: flat GeoJSON, filtered in the database."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)
        document = {
            "floors": [
                {"level": 0, "name": "Ground", "geometry": _SQUARE, "rooms": [{"name": "Boiler room", "geometry": _SQUARE}],
                 "elements": [{"kind": "wall", "geometry": _LINE}, {"kind": "door", "geometry": _POINT}]},
                {"level": 1, "name": "First", "rooms": [{"name": "Ward B", "geometry": _FAR_SQUARE}], "elements": []},
            ],
        }
        save_document(self.floorplan, document, profile=self.profile)

    def _collect(self, **kwargs):
        from urbanlens.dashboard.services.floorplans.features import feature_collection

        return feature_collection(self.floorplan, **kwargs)

    def test_it_is_a_geojson_feature_collection(self) -> None:
        collection = self._collect()

        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertTrue(all(feature["type"] == "Feature" for feature in collection["features"]))
        self.assertTrue(all(feature["geometry"]["type"] in {"Point", "LineString", "Polygon"} for feature in collection["features"]))

    def test_every_feature_carries_the_uuid_it_can_be_edited_by(self) -> None:
        collection = self._collect()

        for feature in collection["features"]:
            self.assertEqual(feature["id"], feature["properties"]["uuid"])

    def test_a_level_filter_returns_one_storey(self) -> None:
        collection = self._collect(level=1)

        names = {feature["properties"]["name"] for feature in collection["features"]}
        self.assertEqual(names, {"Ward B"})

    def test_a_kind_filter_narrows_elements(self) -> None:
        collection = self._collect(kind="door")

        kinds = {f["properties"].get("kind") for f in collection["features"] if f["properties"]["item_type"] == "element"}
        self.assertEqual(kinds, {"door"})

    def test_a_bbox_excludes_what_it_does_not_cover(self) -> None:
        """The filter runs in the database, on the spatial index."""
        collection = self._collect(bbox=(-73.931, 41.732, -73.928, 41.735))

        names = {feature["properties"]["name"] for feature in collection["features"]}
        self.assertNotIn("Ward B", names, "a room outside the viewport should not be returned")
        self.assertIn("Boiler room", names)

    def test_the_cap_is_reported_rather_than_silent(self) -> None:
        """Silence about a truncated list reads as 'that is everything'."""
        collection = self._collect(limit=2)

        self.assertEqual(len(collection["features"]), 2)
        self.assertTrue(collection["truncated"])

    def test_bounds_cover_everything_drawn(self) -> None:
        from urbanlens.dashboard.services.floorplans.features import bounds_of

        bounds = bounds_of(self.floorplan)

        self.assertIsNotNone(bounds)
        self.assertLessEqual(bounds[0], -73.93)
        self.assertGreaterEqual(bounds[2], -73.90)

    def test_bounds_are_none_for_an_empty_plan(self) -> None:
        from urbanlens.dashboard.services.floorplans.features import bounds_of

        self.assertIsNone(bounds_of(Floorplan.objects.create(place=_building(), profile=self.profile)))


class FloorplanCommunityTests(TestCase):
    """Publishing is explicit; a published plan is edited like any wiki content."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.other = baker.make(User).profile
        self._seq = 0
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile, name="mine")
        save_document(self.floorplan, {"name": "mine", "floors": [{"level": 0, "rooms": [{"name": "Boiler room", "geometry": _SQUARE}]}]}, profile=self.profile)

    def _wiki(self):
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = baker.make(Location, latitude=41.7 + self._seq / 1000, longitude=-73.9, place=self.place)
        return baker.make(Wiki, place=self.place, location=location)

    def _publish(self):
        from urbanlens.dashboard.services.floorplans.resolution import publish_to_wiki

        return publish_to_wiki(self.floorplan, self.profile)

    def test_publishing_without_a_wiki_is_refused_not_crashed(self) -> None:
        self.assertIsNone(self._publish())

    def test_publishing_copies_rather_than_hands_over(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki

        self._wiki()

        community = self._publish()

        self.assertIsNotNone(community)
        self.assertNotEqual(community.pk, self.floorplan.pk)
        self.floorplan.refresh_from_db()
        self.assertIsNone(self.floorplan.wiki_id, "the author's own plan must stay personal")
        self.assertEqual(document_for(community)["floors"][0]["rooms"][0]["name"], "Boiler room")

    def test_a_published_plan_gets_its_own_rows(self) -> None:
        """Copying must not move the personal plan's items onto the wiki."""
        from urbanlens.dashboard.models.wiki.model import Wiki

        self._wiki()

        community = self._publish()

        personal_rooms = {str(room["uuid"]) for room in document_for(self.floorplan)["floors"][0]["rooms"]}
        community_rooms = {str(room["uuid"]) for room in document_for(community)["floors"][0]["rooms"]}
        self.assertTrue(personal_rooms)
        self.assertFalse(personal_rooms & community_rooms)

    def test_publishing_records_a_wiki_edit(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.models.wiki_edit import WikiEdit

        wiki = self._wiki()

        self._publish()

        self.assertTrue(WikiEdit.objects.filter(wiki=wiki, editor=self.profile).exists())

    def test_a_personal_plan_is_still_not_served_to_others(self) -> None:
        with mock.patch("urbanlens.dashboard.services.floorplans.resolution._redata_document", return_value=None), \
             mock.patch("urbanlens.dashboard.services.floorplans.resolution._community_plan", return_value=None):
            self.assertIsNone(resolve_document(self.place, profile=self.other))


class ProjectiveTransformTests(TestCase):
    """Tracing must agree with what the browser drew.

    The overlay is rendered through a projective homography (CSS matrix3d, via
    `matrix3dForCorners`). Tracing it with bilinear interpolation instead put
    traced geometry beside the walls it came from on any sheet whose corners
    are not an affine parallelogram - which is what dragging corners onto
    oblique imagery produces.
    """

    def _transform(self, corners):
        from urbanlens.dashboard.services.floorplans.extraction import _corner_transform

        overlay = mock.Mock(pk=1)
        (overlay.nw_longitude, overlay.nw_latitude) = corners[0]
        (overlay.ne_longitude, overlay.ne_latitude) = corners[1]
        (overlay.se_longitude, overlay.se_latitude) = corners[2]
        (overlay.sw_longitude, overlay.sw_latitude) = corners[3]
        return _corner_transform(overlay)

    def test_corners_map_exactly(self) -> None:
        corners = [(-73.93, 41.734), (-73.92, 41.7345), (-73.919, 41.724), (-73.931, 41.7235)]
        transform = self._transform(corners)

        for (x, y), (lng, lat) in zip([(0, 0), (1, 0), (1, 1), (0, 1)], corners, strict=True):
            got = transform(x, y)
            self.assertAlmostEqual(got[0], lng, places=9)
            self.assertAlmostEqual(got[1], lat, places=9)

    def test_a_rectangle_still_maps_linearly(self) -> None:
        """The affine case must not regress: the centre is the centre."""
        transform = self._transform([(-73.93, 41.734), (-73.92, 41.734), (-73.92, 41.724), (-73.93, 41.724)])

        lng, lat = transform(0.5, 0.5)

        self.assertAlmostEqual(lng, -73.925, places=9)
        self.assertAlmostEqual(lat, 41.729, places=9)

    def test_a_keystoned_sheet_is_not_bilinear(self) -> None:
        """Where the two disagree, we must follow the renderer, not the average.

        Deliberately an *irregular* quad: a symmetric trapezoid's centre lands
        in the same place under both transforms, so it proves nothing.
        """
        corners = [(-73.930, 41.7340), (-73.9250, 41.7335), (-73.9210, 41.7245), (-73.9305, 41.7238)]
        transform = self._transform(corners)

        lng, lat = transform(0.5, 0.5)
        top = ((corners[0][0] + corners[1][0]) / 2, (corners[0][1] + corners[1][1]) / 2)
        bottom = ((corners[3][0] + corners[2][0]) / 2, (corners[3][1] + corners[2][1]) / 2)
        bilinear = ((top[0] + bottom[0]) / 2, (top[1] + bottom[1]) / 2)

        self.assertGreater(
            abs(lng - bilinear[0]) + abs(lat - bilinear[1]),
            1e-6,
            "projective and bilinear agreeing here would mean the transform is not projective",
        )

    def test_degenerate_corners_fall_back_rather_than_raise(self) -> None:
        transform = self._transform([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)])

        self.assertEqual(transform(0.5, 0.5), (0.0, 0.0))


class FloorplanDocumentContractTests(TestCase):
    """The document contract REData defines, which ours must not drift from.

    Its write side rejects unknown keys outright, so anything we emit that it
    doesn't accept would break a future push upstream - and anything it emits
    that we ignore is data silently dropped on the way in.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    #: Exactly REData's `_PLAN_KEYS`/`_ITEM_KEYS` (services/floorplans.py), plus
    #: `labels`, which is UrbanLens's own per-item addition.
    _PLAN_KEYS = {
        "uuid", "description", "condition", "built_date", "attributes", "source", "references", "labels",
        "name", "building_ref", "building_name", "valid_from", "floor_count", "source_pool", "reference_pool",
        "floors", "elements",
    }
    _ELEMENT_KEYS = {
        "uuid", "description", "condition", "built_date", "attributes", "source", "references", "labels",
        "kind", "name", "geometry", "material", "room", "mounted_on", "base_elevation_meters", "height_meters",
        "thickness_meters", "rotation_degrees", "connects_rooms", "spans_floors", "locks",
    }
    _ROOM_KEYS = {
        "uuid", "description", "condition", "built_date", "attributes", "source", "references", "labels",
        "name", "geometry", "height_meters", "parent", "spans_floors",
    }

    def test_we_emit_no_key_redata_would_reject(self) -> None:
        save_document(self.floorplan, {"floors": [{"level": 0, "rooms": [{"name": "a", "geometry": _SQUARE}],
                                                   "elements": [{"kind": "door", "geometry": _POINT, "locks": [{"name": "padlock"}]}]}]}, profile=self.profile)

        out = document_for(self.floorplan)

        self.assertEqual(set(out) - self._PLAN_KEYS, set(), "plan-level keys drifted from REData's contract")
        floor = out["floors"][0]
        self.assertEqual(set(floor["rooms"][0]) - self._ROOM_KEYS, set(), "room keys drifted")
        self.assertEqual(set(floor["elements"][0]) - self._ELEMENT_KEYS, set(), "element keys drifted")

    def test_a_document_local_key_links_two_new_items(self) -> None:
        """Neither exists yet, so neither has a uuid to reference."""
        document = {"floors": [{"level": 0, "key": "ground", "elements": [
            {"key": "north-wall", "kind": "wall", "geometry": _LINE},
            {"kind": "door", "geometry": _POINT, "mounted_on": "north-wall"},
        ]}]}

        save_document(self.floorplan, document, profile=self.profile)

        out = document_for(self.floorplan)
        wall = next(e for e in out["floors"][0]["elements"] if e["kind"] == "wall")
        door = next(e for e in out["floors"][0]["elements"] if e["kind"] == "door")
        self.assertEqual(door["mounted_on"], wall["uuid"], "a document-local key must resolve like a uuid")

    def test_keys_are_never_emitted_on_read(self) -> None:
        save_document(self.floorplan, {"floors": [{"level": 0, "key": "ground"}]}, profile=self.profile)

        self.assertNotIn("key", document_for(self.floorplan)["floors"][0])

    def test_a_door_connects_rooms_by_key(self) -> None:
        document = {"floors": [{"level": 0,
                                "rooms": [{"key": "ward", "name": "Ward B", "geometry": _SQUARE},
                                          {"key": "hall", "name": "Hall", "geometry": _SQUARE}],
                                "elements": [{"kind": "door", "geometry": _POINT, "connects_rooms": ["ward", "hall"]}]}]}

        save_document(self.floorplan, document, profile=self.profile)

        door = document_for(self.floorplan)["floors"][0]["elements"][0]
        self.assertEqual(len(door["connects_rooms"]), 2, "a door joining two rooms is what a router walks")

    def test_a_stair_spans_floors(self) -> None:
        document = {"floors": [{"key": "g", "level": 0, "elements": [{"kind": "stair", "geometry": _LINE, "spans_floors": ["g", "first"]}]},
                               {"key": "first", "level": 1}]}

        save_document(self.floorplan, document, profile=self.profile)

        stair = document_for(self.floorplan)["floors"][0]["elements"][0]
        self.assertEqual(len(stair["spans_floors"]), 2)

    def test_a_room_nests_inside_another(self) -> None:
        document = {"floors": [{"level": 0, "rooms": [
            {"key": "ward", "name": "Ward B", "geometry": _SQUARE},
            {"name": "Closet", "geometry": _SQUARE, "parent": "ward"},
        ]}]}

        save_document(self.floorplan, document, profile=self.profile)

        rooms = document_for(self.floorplan)["floors"][0]["rooms"]
        ward = next(room for room in rooms if room["name"] == "Ward B")
        closet = next(room for room in rooms if room["name"] == "Closet")
        self.assertEqual(closet["parent"], ward["uuid"])

    def test_an_out_of_range_level_is_a_message_not_a_500(self) -> None:
        with self.assertRaises(ValueError):
            save_document(self.floorplan, {"floors": [{"level": 99_999}]}, profile=self.profile)


class FloorplanEditorContextTests(TestCase):
    """What the editor page hands the client."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        self.place = _building(parcel)
        location = baker.make(Location, latitude=41.7332, longitude=-73.9282, place=self.place)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, slug="hrsh-ward-b")

    def test_the_pins_photos_are_offered_as_references(self) -> None:
        """The pool attaches to every item, so a pin's own photos are the
        likeliest evidence for a wall, a door, or its lock."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        photo = baker.make("dashboard.Image", pin=self.pin, caption="Boiler room, 2019")
        photo.image.save("p.png", SimpleUploadedFile("p.png", _PNG_BYTES, content_type="image/png"), save=True)

        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        photos = response.context["photos_json"]
        self.assertEqual([entry["uuid"] for entry in photos], [str(photo.uuid)])
        self.assertEqual(photos[0]["caption"], "Boiler room, 2019")

    def test_a_pin_with_no_photos_offers_none(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        self.assertEqual(response.context["photos_json"], [])


class FloorplanMultiBuildingPickerTests(TestCase):
    """A parcel holding several buildings has no single plan - offer a choice."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        self.kirkbride = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel, name="Kirkbride", area_sqm=9000)
        self.chapel = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel, name="Chapel", area_sqm=400)
        location = baker.make(Location, latitude=41.7333, longitude=-73.9283, place=self.parcel)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, slug="hrsh-campus")

    def test_the_picker_lists_the_buildings_largest_first(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        self.assertIsNone(response.context["place"], "a multi-building parcel has no single plan")
        self.assertEqual([choice["name"] for choice in response.context["building_choices"]], ["Kirkbride", "Chapel"])

    def test_a_building_with_a_child_pin_links_to_it(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        child_location = baker.make(Location, latitude=41.7334, longitude=-73.9284, place=self.kirkbride)
        child = baker.make(Pin, profile=self.user.profile, location=child_location, parent_pin=self.pin, slug="kirkbride")

        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        chosen = next(entry for entry in response.context["building_choices"] if entry["name"] == "Kirkbride")
        self.assertEqual(chosen["pin_slug"], child.slug)

    def test_an_unpinned_building_is_listed_without_a_link(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        chapel = next(entry for entry in response.context["building_choices"] if entry["name"] == "Chapel")
        self.assertEqual(chapel["pin_slug"], "", "a building with no pin yet cannot be linked to")

    def test_a_single_building_parcel_still_resolves_directly(self) -> None:
        self.chapel.delete()

        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        self.assertEqual(response.context["place"], self.kirkbride)
        self.assertEqual(response.context["building_choices"], [])
