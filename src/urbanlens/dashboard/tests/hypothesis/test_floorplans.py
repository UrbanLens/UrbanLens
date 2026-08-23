"""Floorplans: versioned interior structure, absent by default, walls-first.

The load-bearing properties:

- **Walls are the only geometry.** Rooms are seed points that bind to whichever
  enclosed region contains them, so editing walls can never delete a room's
  name, labels or references. Openings are intervals along a wall and cannot
  outlive it, escape it, or be inside out.
- **Coordinates are plan-local metres** about one per-plan origin shared by
  every floor, not WGS-84. ``services.floorplans.features`` is the only place
  they become degrees, and it must agree with the editor's own projection.
- **Version resolution by date**: the undated baseline is in force until the
  first dated version; no date means the newest; a building with no plan
  answers None from one indexed query (the common case must stay free).
- **A save never destroys a plan it was not editing** - another user's plan,
  or a baseline being re-dated, forks into a new version instead.
- **Publishing copies rather than hands over**, and a personal plan is never
  served to anyone else.
- **A lock belongs to its door.** Which door is locked and what opens it is
  field data worth keeping; a lock outliving the opening it was fitted to
  would not be.
"""

from __future__ import annotations

import base64
import datetime
import itertools
import json as jsonlib
from unittest import mock

from django.contrib.auth.models import User
from django.db.utils import IntegrityError
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.floorplans.model import (
    Floorplan,
    FloorplanFloor,
    FloorplanLock,
    FloorplanMarker,
    FloorplanOpening,
    FloorplanRoomSeed,
    FloorplanWall,
)
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.services.floorplans.resolution import resolve_document
from urbanlens.dashboard.services.floorplans.serialization import document_for, save_document

#: A minimal valid 1x1 PNG - real bytes, so the stored-file read path is exercised.
_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
)

_ORIGIN = {"lat": 41.733, "lng": -73.93}


def _square_walls(size: float = 10.0) -> list[dict]:
    """Four walls enclosing a ``size`` metre square at the plan origin."""
    corners = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)]
    return [
        {"kind": "exterior", "ax": ax, "ay": ay, "bx": corners[(i + 1) % 4][0], "by": corners[(i + 1) % 4][1]}
        for i, (ax, ay) in enumerate(corners)
    ]


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

    def test_a_plan_needs_no_place_at_all(self) -> None:
        """Most of what gets explored has no footprint any provider knows.

        A plan tied to nothing is still a plan; requiring a Place would mean
        the only drawable buildings are the ones a data provider already
        catalogued.
        """
        plan = Floorplan.objects.create(place=None, name="sketch from memory")

        self.assertIsNone(plan.place_id)
        self.assertEqual(Floorplan.objects.get(pk=plan.pk).name, "sketch from memory")


class FloorplanDocumentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile)

    def _document(self) -> dict:
        walls = _square_walls()
        walls[0] = {
            **walls[0],
            "uuid": "wall-1",
            "name": "South wall",
            "thickness": "thick",
            "source": "src-1",
            "openings": [{"uuid": "door-1", "kind": "door", "t_start": 0.4, "t_end": 0.6, "swing": "left"}],
        }
        return {
            "name": "As built",
            "plan_origin": _ORIGIN,
            "rotation_degrees": 12.5,
            "source_pool": [{"uuid": "src-1", "title": "HABS sheet 4", "author": "HABS", "url": "https://loc.gov/habs/4"}],
            "reference_pool": [{"uuid": "ref-1", "kind": "photo", "title": "Boiler room, 2019", "url": "https://example.test/p.jpg"}],
            "floors": [
                {
                    "level": 0,
                    "name": "Ground",
                    "walls": walls,
                    "rooms": [{"uuid": "room-1", "name": "Boiler room", "x": 5.0, "y": 5.0, "condition": "collapsed", "references": ["ref-1"]}],
                    "markers": [{"uuid": "m-1", "kind": "stair", "name": "North stair", "x": 2.0, "y": 8.0, "connector_id": "stair-a"}],
                },
            ],
        }

    def test_a_document_round_trips(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)

        document = document_for(self.floorplan)
        floor = document["floors"][0]
        self.assertEqual(document["name"], "As built")
        self.assertEqual(document["plan_origin"], _ORIGIN)
        self.assertEqual(document["rotation_degrees"], 12.5)
        self.assertEqual(len(floor["walls"]), 4)
        self.assertEqual(floor["rooms"][0]["name"], "Boiler room")
        self.assertEqual(floor["markers"][0]["connector_id"], "stair-a")

    def test_local_metres_survive_the_round_trip_exactly(self) -> None:
        """Coordinates are stored as authored - no projection on the way in.

        A wall that shifts by centimetres per save would drift a plan apart
        over an editing session.
        """
        save_document(self.floorplan, self._document(), profile=self.profile)

        walls = document_for(self.floorplan)["floors"][0]["walls"]
        self.assertEqual(
            [(wall["ax"], wall["ay"], wall["bx"], wall["by"]) for wall in walls],
            [(0.0, 0.0, 10.0, 0.0), (10.0, 0.0, 10.0, 10.0), (10.0, 10.0, 0.0, 10.0), (0.0, 10.0, 0.0, 0.0)],
        )

    def test_an_opening_rides_with_its_wall(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)

        wall = document_for(self.floorplan)["floors"][0]["walls"][0]
        self.assertEqual(wall["name"], "South wall")
        self.assertEqual(wall["thickness"], "thick")
        self.assertEqual(wall["openings"][0]["kind"], "door")
        self.assertEqual((wall["openings"][0]["t_start"], wall["openings"][0]["t_end"]), (0.4, 0.6))

    def test_round_tripped_uuids_update_in_place(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)
        document = document_for(self.floorplan)
        room_uuid = document["floors"][0]["rooms"][0]["uuid"]

        document["floors"][0]["rooms"][0]["name"] = "Ward B"
        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(FloorplanRoomSeed.objects.count(), 1, "a round-tripped uuid must update, not duplicate")
        self.assertEqual(str(FloorplanRoomSeed.objects.get().uuid), room_uuid)
        self.assertEqual(FloorplanRoomSeed.objects.get().name, "Ward B")

    def test_omitted_items_are_deleted(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)
        document = document_for(self.floorplan)

        document["floors"][0]["rooms"] = []
        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(FloorplanRoomSeed.objects.count(), 0)
        self.assertEqual(FloorplanWall.objects.count(), 4, "removing rooms must not touch walls")

    def test_labels_resolve_only_against_the_saving_profiles_own(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label

        mine = baker.make(Label, profile=self.profile, name="Asbestos")
        theirs = baker.make(Label, profile=baker.make(User).profile, name="Theirs")
        document = self._document()
        document["floors"][0]["rooms"][0]["attributes"] = {"urbanlens": {"labels": [str(mine.uuid), str(theirs.uuid)]}}

        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(
            [label.pk for label in FloorplanRoomSeed.objects.get().labels.all()],
            [mine.pk],
            "a document must not attach another user's label by guessing its uuid",
        )

    def test_versions_and_dates_persist(self) -> None:
        document = self._document()
        document["valid_from"] = "1962-05-01"

        save_document(self.floorplan, document, profile=self.profile)

        self.floorplan.refresh_from_db()
        self.assertEqual(self.floorplan.valid_from, datetime.date(1962, 5, 1))

    def test_an_unknown_wall_kind_is_refused_rather_than_coerced(self) -> None:
        """Silently turning an unrecognised kind into the default is how a
        whole class of item becomes wrong while looking like it saved."""
        document = self._document()
        document["floors"][0]["walls"][1]["kind"] = "forcefield"

        with self.assertRaises(ValueError) as caught:
            save_document(self.floorplan, document, profile=self.profile)

        self.assertIn("forcefield", str(caught.exception))

    def test_a_wall_missing_an_endpoint_is_refused(self) -> None:
        """Defaulting a missing coordinate to zero would move the wall to the
        plan origin without saying so."""
        document = self._document()
        del document["floors"][0]["walls"][1]["bx"]

        with self.assertRaises(ValueError) as caught:
            save_document(self.floorplan, document, profile=self.profile)

        self.assertIn("bx", str(caught.exception))

    def test_a_doors_swing_survives_the_round_trip(self) -> None:
        """It is drawn from this value, so losing it silently loses the symbol.

        The field had a column, choices and a serializer long before anything
        set it, which is exactly the situation where nobody would notice it
        failing to come back.
        """
        walls = _square_walls()
        walls[0] = {**walls[0], "openings": [{"kind": "door", "t_start": 0.4, "t_end": 0.6, "swing": "double"}]}
        save_document(self.floorplan, {"floors": [{"level": 0, "walls": walls, "rooms": [], "markers": []}]}, profile=self.profile)

        opening = document_for(self.floorplan)["floors"][0]["walls"][0]["openings"][0]
        self.assertEqual(opening["swing"], "double")

    def test_an_unknown_swing_is_refused_without_writing_half_a_plan(self) -> None:
        """A whole plan is one document: a bad field late in it must lose none of it.

        The value is rejected rather than coerced, which is the same thing every
        other choice field here does - but the save is a wholesale replacement,
        so the interesting question is what survives the refusal.
        """
        walls = _square_walls()
        walls[0] = {**walls[0], "name": "the original south wall"}
        save_document(self.floorplan, {"floors": [{"level": 0, "walls": walls, "rooms": [], "markers": []}]}, profile=self.profile)

        replacement = _square_walls()
        replacement[0] = {**replacement[0], "name": "a replacement"}
        replacement[2] = {**replacement[2], "openings": [{"kind": "door", "t_start": 0.4, "t_end": 0.6, "swing": "sideways"}]}
        with pytest.raises(ValueError, match="opening swing"):
            save_document(self.floorplan, {"floors": [{"level": 0, "walls": replacement, "rooms": [], "markers": []}]}, profile=self.profile)

        document = document_for(self.floorplan)
        self.assertEqual(len(document["floors"][0]["walls"]), 4)
        self.assertEqual(document["floors"][0]["walls"][0]["name"], "the original south wall")

class FloorplanRoomSeedTests(TestCase):
    """Room identity is a point, so wall edits cannot destroy it."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    def test_a_room_survives_every_wall_around_it_being_rewritten(self) -> None:
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [{"level": 0, "walls": _square_walls(), "rooms": [{"name": "Ward B", "x": 5.0, "y": 5.0}]}],
            },
            profile=self.profile,
        )
        room_uuid = document_for(self.floorplan)["floors"][0]["rooms"][0]["uuid"]
        document = document_for(self.floorplan)

        # Replace every wall with a differently-sized square: all four wall
        # uuids are dropped, so each is deleted and recreated.
        document["floors"][0]["walls"] = _square_walls(25.0)
        save_document(self.floorplan, document, profile=self.profile)

        rooms = document_for(self.floorplan)["floors"][0]["rooms"]
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["uuid"], room_uuid, "the room kept its identity through a full wall replacement")
        self.assertEqual(rooms[0]["name"], "Ward B")

    def test_a_room_outside_every_wall_is_still_stored(self) -> None:
        """An unenclosed seed is a drawing that is not finished, not an error.

        Refusing to store it would lose the name the moment a wall was
        deleted.
        """
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": [], "rooms": [{"name": "Somewhere", "x": 900.0, "y": 900.0}]}]},
            profile=self.profile,
        )

        self.assertEqual(FloorplanRoomSeed.objects.get().name, "Somewhere")


class FloorplanOpeningConstraintTests(TestCase):
    """An opening outside its wall, or inside out, is meaningless - not a
    rendering quirk. The database refuses it so no write path can produce one."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)
        self.floor = FloorplanFloor.objects.create(floorplan=self.floorplan, level=0)
        self.wall = FloorplanWall.objects.create(floor=self.floor, ax=0, ay=0, bx=10, by=0)

    def test_a_valid_interval_is_accepted(self) -> None:
        opening = FloorplanOpening.objects.create(wall=self.wall, kind="door", t_start=0.25, t_end=0.75)

        self.assertEqual(opening.wall_id, self.wall.pk)

    def test_an_inside_out_interval_is_refused(self) -> None:
        with self.assertRaises(IntegrityError):
            FloorplanOpening.objects.create(wall=self.wall, kind="door", t_start=0.8, t_end=0.2)

    def test_an_interval_running_past_the_wall_is_refused(self) -> None:
        with self.assertRaises(IntegrityError):
            FloorplanOpening.objects.create(wall=self.wall, kind="door", t_start=0.5, t_end=1.5)

    def test_a_negative_start_is_refused(self) -> None:
        with self.assertRaises(IntegrityError):
            FloorplanOpening.objects.create(wall=self.wall, kind="door", t_start=-0.1, t_end=0.5)

    def test_a_zero_length_opening_is_refused(self) -> None:
        with self.assertRaises(IntegrityError):
            FloorplanOpening.objects.create(wall=self.wall, kind="door", t_start=0.5, t_end=0.5)

    def test_deleting_a_wall_takes_its_openings_with_it(self) -> None:
        """An opening has no position of its own - orphaning one would leave a
        door floating at coordinates that no longer exist."""
        FloorplanOpening.objects.create(wall=self.wall, kind="door", t_start=0.2, t_end=0.4)

        self.wall.delete()

        self.assertEqual(FloorplanOpening.objects.count(), 0)

    def test_the_service_reports_a_bad_interval_rather_than_hitting_the_constraint(self) -> None:
        """Reaching the constraint is an IntegrityError - a 500. The service
        catches it first and names what is wrong, which a client can act on."""
        document = {
            "plan_origin": _ORIGIN,
            "floors": [{"level": 0, "walls": [{"kind": "interior", "ax": 0, "ay": 0, "bx": 5, "by": 0, "openings": [{"kind": "door", "t_start": 0.9, "t_end": 0.1}]}]}],
        }

        with self.assertRaises(ValueError) as caught:
            save_document(self.floorplan, document, profile=self.profile)

        self.assertIn("t_start", str(caught.exception))


class FloorplanLockTests(TestCase):
    """A lock belongs to the door it is fitted to.

    "Which door is locked, and what opens it" is the field note this table
    exists for, so it has to survive a save/reload untouched - including the
    part of it nobody agreed a schema for. And a lock whose door is gone is a
    fact about nothing, so it goes with it.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    #: The lock every test starts from unless it needs a different one.
    _PADLOCK = {"name": "padlock", "state": "locked", "key_attributes": {"brand": "Abus", "bitting": "44213"}}

    def _document(self, locks: list[dict] | None = None) -> dict:
        """A one-wall plan whose single door carries ``locks``."""
        door = {"kind": "door", "t_start": 0.4, "t_end": 0.6, "locks": [dict(self._PADLOCK)] if locks is None else locks}
        wall = {"kind": "exterior", "ax": 0.0, "ay": 0.0, "bx": 10.0, "by": 0.0, "openings": [door]}
        return {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": [wall]}]}

    def _locks_in(self, document: dict) -> list[dict]:
        return document["floors"][0]["walls"][0]["openings"][0]["locks"]

    def test_a_lock_round_trips_under_its_opening(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)

        locks = self._locks_in(document_for(self.floorplan))
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["name"], "padlock")
        self.assertEqual(locks[0]["state"], "locked")
        self.assertEqual(locks[0]["key_attributes"], {"brand": "Abus", "bitting": "44213"})

    def test_key_attributes_survive_a_round_trip_whatever_shape_they_are(self) -> None:
        """The point of a free-form field is that an unanticipated shape comes
        back as it went in - otherwise the note may as well have been typed
        into a column somebody chose in advance."""
        recorded = {
            "keyway": "SC1",
            "bitting": [4, 4, 2, 1, 3],
            "seen": {"date": "2019-06-02", "by": "note in the caretaker's log"},
            "opens_with": None,
            "confirmed": False,
        }
        save_document(self.floorplan, self._document([{"name": "deadbolt", "key_attributes": recorded}]), profile=self.profile)

        self.assertEqual(self._locks_in(document_for(self.floorplan))[0]["key_attributes"], recorded)

    def test_a_round_tripped_lock_updates_in_place(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)
        document = document_for(self.floorplan)
        lock_uuid = self._locks_in(document)[0]["uuid"]

        self._locks_in(document)[0]["state"] = "unlocked"
        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(FloorplanLock.objects.count(), 1, "a round-tripped uuid must update, not duplicate")
        self.assertEqual(str(FloorplanLock.objects.get().uuid), lock_uuid)
        self.assertEqual(FloorplanLock.objects.get().state, "unlocked")

    def test_omitting_a_lock_deletes_it(self) -> None:
        save_document(self.floorplan, self._document(), profile=self.profile)
        document = document_for(self.floorplan)

        document["floors"][0]["walls"][0]["openings"][0]["locks"] = []
        save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(FloorplanLock.objects.count(), 0)
        self.assertEqual(FloorplanOpening.objects.count(), 1, "removing a lock must not touch the door it was on")

    def test_a_second_lock_can_be_added_to_one_door(self) -> None:
        """A hasp with a padlock and a deadbolt below it is one door, two
        locks - and the pair has to keep its authored order."""
        save_document(
            self.floorplan,
            self._document([{"name": "padlock", "state": "locked"}, {"name": "chain", "state": "unlocked"}]),
            profile=self.profile,
        )

        self.assertEqual([lock["name"] for lock in self._locks_in(document_for(self.floorplan))], ["padlock", "chain"])

    def test_deleting_an_opening_takes_its_locks_with_it(self) -> None:
        floor = FloorplanFloor.objects.create(floorplan=self.floorplan, level=0)
        wall = FloorplanWall.objects.create(floor=floor, ax=0, ay=0, bx=10, by=0)
        opening = FloorplanOpening.objects.create(wall=wall, kind="door", t_start=0.2, t_end=0.4)
        FloorplanLock.objects.create(opening=opening, name="padlock")

        opening.delete()

        self.assertEqual(FloorplanLock.objects.count(), 0)

    def test_deleting_a_wall_takes_the_locks_on_its_doors_with_it(self) -> None:
        """Two cascades deep: the lock hangs off the opening, which hangs off
        the wall, so redrawing a wall cannot leave a lock behind."""
        floor = FloorplanFloor.objects.create(floorplan=self.floorplan, level=0)
        wall = FloorplanWall.objects.create(floor=floor, ax=0, ay=0, bx=10, by=0)
        opening = FloorplanOpening.objects.create(wall=wall, kind="door", t_start=0.2, t_end=0.4)
        FloorplanLock.objects.create(opening=opening, name="padlock")

        wall.delete()

        self.assertEqual(FloorplanLock.objects.count(), 0)

    def test_a_lock_defaults_to_an_unknown_state(self) -> None:
        """Most locks are recorded from a photograph, which shows a padlock
        but not whether it is shut. Defaulting to "locked" would invent that."""
        save_document(self.floorplan, self._document([{"name": "padlock"}]), profile=self.profile)

        lock = self._locks_in(document_for(self.floorplan))[0]
        self.assertEqual(lock["state"], "unknown")
        self.assertEqual(lock["key_attributes"], {})

    def test_an_unknown_lock_state_is_refused_rather_than_coerced(self) -> None:
        """Silently falling back to "unknown" is how a door that was recorded
        as locked reads as unrecorded, with nothing to show it happened."""
        with self.assertRaises(ValueError) as caught:
            save_document(self.floorplan, self._document([{"name": "padlock", "state": "jammed"}]), profile=self.profile)

        self.assertIn("jammed", str(caught.exception))
        self.assertEqual(FloorplanLock.objects.count(), 0)

    def test_key_attributes_that_are_not_an_object_are_reported(self) -> None:
        """Free-form is not shapeless: a list reaches the database intact and
        breaks readers later, so it is a 400 here rather than a 500 there."""
        with self.assertRaises(ValueError) as caught:
            save_document(self.floorplan, self._document([{"name": "padlock", "key_attributes": ["bitting", "44213"]}]), profile=self.profile)

        self.assertIn("key_attributes", str(caught.exception))

    def test_a_locks_own_item_surface_round_trips(self) -> None:
        """A lock is a floorplan item like any other - its condition and
        references are how physical state is recorded, which is why ``state``
        carries only whether it is shut."""
        document = self._document([{"name": "padlock", "state": "locked", "condition": "rusted", "description": "hasp bent", "references": ["ref-1"]}])
        document["reference_pool"] = [{"uuid": "ref-1", "kind": "photo", "title": "Door 3, 2019", "url": "https://example.test/d3.jpg"}]

        save_document(self.floorplan, document, profile=self.profile)

        lock = self._locks_in(document_for(self.floorplan))[0]
        self.assertEqual(lock["condition"], "rusted")
        self.assertEqual(lock["description"], "hasp bent")
        self.assertEqual(len(lock["references"]), 1)


class FloorplanResolutionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL, provider="redata", provider_key="parcel-uuid-1")
        self.place = _building(self.parcel, provider_key="cris:res-1")

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
        """A plan records doors and entrances - not shared data.

        Resolution is place-scoped, so without this every user who pinned the
        same building would receive whatever anyone else had traced.
        """
        Floorplan.objects.create(place=self.place, profile=baker.make(User).profile, name="theirs")

        with mock.patch("urbanlens.dashboard.services.floorplans.resolution._redata_document", return_value=None):
            self.assertIsNone(resolve_document(self.place, profile=self.profile))

    def test_redata_fills_when_local_is_absent(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = [{"uuid": "fp-1", "building_ref": "cris:res-1"}]
            gateway.return_value.lookup_floorplan_document.return_value = {"name": "REData plan", "floors": []}

            document = resolve_document(self.place, profile=self.profile)

        self.assertEqual(document["origin"], "redata")
        self.assertEqual(document["name"], "REData plan")
        gateway.return_value.lookup_floorplans.assert_called_once_with("parcel-uuid-1", building_ref="cris:res-1", on_date=None)
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
        orphan = baker.make(Place, kind=PlaceKind.BUILDING, provider="redata", provider_key="cris:orphan", parent=None)

        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway:
            self.assertIsNone(resolve_document(orphan, profile=self.profile))

        gateway.assert_not_called()

    def test_the_date_flows_through_to_upstream(self) -> None:
        url_patch, key_patch = self._configured()
        with mock.patch("urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway") as gateway, url_patch, key_patch:
            gateway.return_value.lookup_floorplans.return_value = []

            resolve_document(self.place, profile=self.profile, on_date=datetime.date(1954, 1, 1))

        gateway.return_value.lookup_floorplans.assert_called_once_with("parcel-uuid-1", building_ref="cris:res-1", on_date="1954-01-01")


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

    def _save(self, document: dict):
        return self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps(document),
            content_type="application/json",
        )

    def test_no_plan_is_a_204_not_an_error(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/json/")

        self.assertEqual(response.status_code, 204)

    def test_save_then_get_round_trips(self) -> None:
        document = {
            "name": "As built",
            "plan_origin": _ORIGIN,
            "floors": [{"level": 0, "name": "Ground", "walls": _square_walls(), "rooms": [{"name": "Boiler room", "x": 5.0, "y": 5.0}]}],
        }
        save = self._save(document)
        self.assertEqual(save.status_code, 200, save.content)

        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/json/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["origin"], "local")
        self.assertEqual(body["floors"][0]["rooms"][0]["name"], "Boiler room")
        self.assertEqual(len(body["floors"][0]["walls"]), 4)

    def test_the_origin_is_seeded_from_the_pin_when_the_document_omits_it(self) -> None:
        """Every coordinate is metres from the origin, so a plan saved without
        one would have nowhere to be drawn."""
        response = self._save({"floors": [{"level": 0, "walls": _square_walls()}]})

        self.assertEqual(response.status_code, 200, response.content)
        origin = response.json()["floorplan"]["plan_origin"]
        self.assertAlmostEqual(origin["lat"], 41.733, places=3)
        self.assertAlmostEqual(origin["lng"], -73.928, places=3)

    def test_someone_elses_pin_is_a_404(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        other_location = baker.make(Location, latitude=41.9, longitude=-73.9, place=self.place)
        other = baker.make(Pin, profile=baker.make(User).profile, location=other_location, parent_pin=None, slug="not-mine")

        self.assertEqual(self.client.get(f"/dashboard/map/pin/{other.slug}/floorplan/json/").status_code, 404)

    def test_a_bad_number_is_a_400_naming_the_field(self) -> None:
        response = self._save({"floor_count": "several"})

        self.assertEqual(response.status_code, 400, "a non-numeric field must not reach the database as a 500")
        self.assertIn("floor_count", response.json()["error"])

    def test_a_bad_date_is_a_400(self) -> None:
        response = self._save({"floors": [{"level": 0, "built_date": "sometime in 1890"}]})

        self.assertEqual(response.status_code, 400)

    def test_a_missing_wall_coordinate_is_a_400_naming_the_defect(self) -> None:
        response = self._save({"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": [{"kind": "interior", "ax": 0, "ay": 0, "bx": 5}]}]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("by", response.json()["error"])

    def test_the_editor_page_renders(self) -> None:
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "floorplan-map")

    def test_a_markers_icon_and_colour_use_the_shared_pickers(self) -> None:
        """Not controls of this editor's own, so the two cannot drift apart.

        A marker is a pin by another name, and picking its icon or colour should
        be the same act in both places. The colour swatches here were this
        editor's own until they were replaced by the partial the label and pin
        dialogs use.
        """
        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/")

        self.assertContains(response, 'id="color-picker-floorplan-marker"')
        self.assertContains(response, 'class="color-swatch')
        self.assertContains(response, 'id="icon-value-floorplan-marker"')
        self.assertNotContains(response, "floorplan-swatch")


class FloorplanVersionSafetyTests(TestCase):
    """A save must never destroy a plan it was not editing.

    Floorplans are expensive hand work: hours of tracing. Two ways that work
    could have been lost - re-dating a loaded plan silently rewrote the
    version in force at the new date, and any user could write over any other
    user's plan for the same building, since resolution is place-scoped.
    Both fork into a new version instead.
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
    """Document order is the stored order, so an editor's arrangement survives."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    def _save(self, names: list[str]) -> None:
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [
                    {
                        "level": 0,
                        "walls": [{"kind": "interior", "ax": float(i), "ay": 0.0, "bx": float(i) + 1, "by": 0.0, "name": name} for i, name in enumerate(names)],
                    },
                ],
            },
            profile=self.profile,
        )

    def test_document_order_round_trips(self) -> None:
        self._save(["first", "second", "third"])

        walls = document_for(self.floorplan)["floors"][0]["walls"]
        self.assertEqual([wall["name"] for wall in walls], ["first", "second", "third"])

    def test_re_ordering_in_a_later_save_sticks(self) -> None:
        self._save(["first", "second", "third"])
        document = document_for(self.floorplan)
        document["floors"][0]["walls"].reverse()

        save_document(self.floorplan, document, profile=self.profile)

        walls = document_for(self.floorplan)["floors"][0]["walls"]
        self.assertEqual([wall["name"] for wall in walls], ["third", "second", "first"])


class FloorplanFeatureCollectionTests(TestCase):
    """The map-facing read: plan-local metres projected to WGS-84."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [
                    {
                        "level": 0,
                        "walls": _square_walls(),
                        "rooms": [{"name": "Boiler room", "x": 5.0, "y": 5.0}],
                        "markers": [{"kind": "stair", "name": "North stair", "x": 2.0, "y": 8.0}],
                    },
                    {"level": 1, "walls": _square_walls(6.0)},
                ],
            },
            profile=self.profile,
        )

    def _collection(self, **kwargs):
        from urbanlens.dashboard.services.floorplans.features import feature_collection

        return feature_collection(self.floorplan, **kwargs)

    def test_it_is_a_geojson_feature_collection(self) -> None:
        body = self._collection()

        self.assertEqual(body["type"], "FeatureCollection")
        self.assertTrue(all(feature["type"] == "Feature" for feature in body["features"]))

    def test_every_feature_carries_the_uuid_it_can_be_edited_by(self) -> None:
        body = self._collection()

        self.assertTrue(all(feature["properties"]["uuid"] for feature in body["features"]))

    def test_walls_project_to_coordinates_near_the_origin(self) -> None:
        """Local metres become degrees here, and nowhere else. A wall ten
        metres from the origin must land within a fraction of a degree of it."""
        wall = next(f for f in self._collection()["features"] if f["properties"]["item_type"] == "wall")

        (lng, lat) = wall["geometry"]["coordinates"][0]
        self.assertAlmostEqual(lat, _ORIGIN["lat"], places=2)
        self.assertAlmostEqual(lng, _ORIGIN["lng"], places=2)

    def test_the_projection_matches_the_editors_own(self) -> None:
        """The server and the editor must agree, or a plan appears to move
        when a viewer switches between them. Mirrors coords.ts exactly."""
        import math

        from urbanlens.dashboard.services.floorplans.features import EARTH_RADIUS_M, PlanProjection

        projection = PlanProjection(_ORIGIN["lat"], _ORIGIN["lng"])
        metres_per_deg_lat = (math.pi / 180) * EARTH_RADIUS_M
        expected_lat = _ORIGIN["lat"] + 10.0 / metres_per_deg_lat
        expected_lng = _ORIGIN["lng"] + 10.0 / (metres_per_deg_lat * math.cos(math.radians(_ORIGIN["lat"])))

        self.assertAlmostEqual(projection.to_world(10.0, 10.0)[1], expected_lat, places=9)
        self.assertAlmostEqual(projection.to_world(10.0, 10.0)[0], expected_lng, places=9)

    def test_a_level_filter_returns_one_storey(self) -> None:
        body = self._collection(level=1)

        self.assertTrue(body["features"])
        self.assertTrue(all(feature["properties"]["level"] == 1 for feature in body["features"]))

    def test_a_kind_filter_narrows_walls(self) -> None:
        body = self._collection(item_types=("wall",), kind="exterior")

        self.assertTrue(body["features"])
        self.assertTrue(all(feature["properties"]["kind"] == "exterior" for feature in body["features"]))

    def test_openings_ride_with_their_wall_rather_than_as_features(self) -> None:
        document = document_for(self.floorplan)
        document["floors"][0]["walls"][0]["openings"] = [{"kind": "door", "t_start": 0.4, "t_end": 0.6}]
        save_document(self.floorplan, document, profile=self.profile)

        walls = [f for f in self._collection(item_types=("wall",))["features"] if f["properties"]["openings"]]

        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0]["properties"]["openings"][0]["kind"], "door")

    def test_a_bbox_excludes_what_it_does_not_cover(self) -> None:
        body = self._collection(bbox=(-10.0, 10.0, -9.0, 11.0))

        self.assertEqual(body["features"], [])

    def test_the_cap_is_reported_rather_than_silent(self) -> None:
        body = self._collection(limit=2)

        self.assertEqual(len(body["features"]), 2)
        self.assertTrue(body["truncated"], "a cut-off list that says nothing reads as 'that is everything'")

    def test_bounds_cover_everything_drawn(self) -> None:
        from urbanlens.dashboard.services.floorplans.features import bounds_of

        bounds = bounds_of(self.floorplan)

        self.assertIsNotNone(bounds)
        min_lng, min_lat, max_lng, max_lat = bounds
        self.assertLess(min_lng, max_lng)
        self.assertLess(min_lat, max_lat)

    def test_bounds_are_none_for_an_empty_plan(self) -> None:
        from urbanlens.dashboard.services.floorplans.features import bounds_of

        empty = Floorplan.objects.create(place=_building(), profile=self.profile)

        self.assertIsNone(bounds_of(empty))

    def test_a_plan_with_no_origin_draws_nothing_rather_than_guessing(self) -> None:
        """Without an origin the metres have no meaning; placing them at 0,0
        would drop the whole plan into the Gulf of Guinea."""
        from urbanlens.dashboard.services.floorplans.features import feature_collection

        self.floorplan.origin_lat = None
        self.floorplan.origin_lng = None
        self.floorplan.save(update_fields=["origin_lat", "origin_lng"])

        self.assertEqual(feature_collection(self.floorplan)["features"], [])


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
        save_document(
            self.floorplan,
            {"name": "mine", "plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls(), "rooms": [{"name": "Boiler room", "x": 5.0, "y": 5.0}]}]},
            profile=self.profile,
        )

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
        self._wiki()

        community = self._publish()

        self.assertIsNotNone(community)
        self.assertNotEqual(community.pk, self.floorplan.pk)
        self.floorplan.refresh_from_db()
        self.assertIsNone(self.floorplan.wiki_id, "the author's own plan must stay personal")
        self.assertEqual(document_for(community)["floors"][0]["rooms"][0]["name"], "Boiler room")

    def test_a_published_plan_gets_its_own_rows(self) -> None:
        """Copying must not move the personal plan's items onto the wiki."""
        self._wiki()

        community = self._publish()

        personal_rooms = {str(room["uuid"]) for room in document_for(self.floorplan)["floors"][0]["rooms"]}
        community_rooms = {str(room["uuid"]) for room in document_for(community)["floors"][0]["rooms"]}
        self.assertTrue(personal_rooms)
        self.assertFalse(personal_rooms & community_rooms)

    def test_publishing_carries_the_origin_across(self) -> None:
        """Without it the copy's metres would have no anchor and the published
        plan would be unplaceable."""
        self._wiki()

        community = self._publish()

        self.assertEqual(document_for(community)["plan_origin"], _ORIGIN)

    def test_publishing_records_a_wiki_edit(self) -> None:
        from urbanlens.dashboard.models.wiki_edit import WikiEdit

        wiki = self._wiki()

        self._publish()

        self.assertTrue(WikiEdit.objects.filter(wiki=wiki, editor=self.profile).exists())

    def test_a_personal_plan_is_still_not_served_to_others(self) -> None:
        with mock.patch("urbanlens.dashboard.services.floorplans.resolution._redata_document", return_value=None), \
             mock.patch("urbanlens.dashboard.services.floorplans.resolution._community_plan", return_value=None):
            self.assertIsNone(resolve_document(self.place, profile=self.other))


class FloorplanCommunityOverwriteTests(TestCase):
    """A save that carries a community plan's uuid must not rewrite that plan.

    ``floorplan_for_editing`` deliberately lets anyone who can edit the wiki
    write the shared row - but the editor reaches it from a debounced autosave
    that fires seconds after the page opens, while the banner on screen says
    "Saving creates your own version". These pin the promise the UI makes.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.author = baker.make(User).profile
        self.visitor = baker.make(User)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.wiki.model import Wiki

        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL)
        self.place = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel, parent_relation=PlaceRelation.PART_OF)
        # Without a domain root the wiki is visible to nobody, can_edit_community
        # is False for everyone, and these tests would pass by never reaching
        # the in-place community write they exist to pin down.
        Place.objects.filter(pk=self.place.pk).update(domain_root_id=self.parcel.domain_root_id)
        self.place.refresh_from_db()
        location = baker.make(Location, latitude=41.7331, longitude=-73.9281, place=self.place)
        baker.make(Wiki, place=self.place, location=location)
        self.pin = baker.make(Pin, profile=self.visitor.profile, location=location, parent_pin=None, slug="visitor-pin")
        from urbanlens.dashboard.services.wiki.wiki_access import place_visible_to

        assert place_visible_to(self.place, self.visitor.profile), "test setup must actually grant wiki access"


        personal = Floorplan.objects.create(place=self.place, profile=self.author, name="author's plan")
        save_document(
            personal,
            {"name": "author's plan", "plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls(), "rooms": [{"name": "Boiler room", "x": 5.0, "y": 5.0}]}]},
            profile=self.author,
        )
        from urbanlens.dashboard.services.floorplans.resolution import publish_to_wiki

        self.community = publish_to_wiki(personal, self.author)
        assert self.community is not None
        self.client.force_login(self.visitor)

    def _save(self, document: dict):
        return self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps(document),
            content_type="application/json",
        )

    def test_saving_a_community_plans_uuid_does_not_destroy_it(self) -> None:
        """The whole-document save deletes by omission, so an overwrite here
        wipes every wall and room the author published."""
        response = self._save({"uuid": str(self.community.uuid), "plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": []}]})

        self.assertEqual(response.status_code, 200, response.content)
        document = document_for(Floorplan.objects.get(pk=self.community.pk))
        self.assertEqual(len(document["floors"][0]["walls"]), 4, "the published plan lost its walls")
        self.assertEqual(document["floors"][0]["rooms"][0]["name"], "Boiler room")

    def test_a_community_row_never_adopts_the_saving_users_private_pin(self) -> None:
        """serialization._sync_linked_pin documents that a wiki copy has no
        owning pin; adopting one mints detail pins in that account for other
        people's markers."""
        self._save({"uuid": str(self.community.uuid), "plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]})

        self.community.refresh_from_db()
        self.assertIsNone(self.community.pin_id)

    def test_the_response_reports_provenance_rather_than_assuming_local(self) -> None:
        response = self._save({"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]})

        body = response.json()["floorplan"]
        saved = Floorplan.objects.get(uuid=body["uuid"])
        self.assertIsNone(saved.wiki_id)
        self.assertEqual(body["origin"], "local")


class FloorplanFloorDesignationTests(TestCase):
    """A floor's position in the stack and what it is called are separate facts."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile, name="plan")

    def _save(self, floors: list[dict]) -> None:
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": floors}, profile=self.profile)

    def test_designation_round_trips(self) -> None:
        self._save([{"level": 0, "designation": "G"}, {"level": 1, "designation": "4A"}])

        floors = document_for(self.floorplan)["floors"]
        self.assertEqual([f["designation"] for f in floors], ["G", "4A"])

    def test_a_blank_designation_stays_blank_rather_than_being_invented(self) -> None:
        """Blank means "derive a label", which is the client's job - so the
        server must not helpfully fill one in."""
        self._save([{"level": 0}])

        self.assertEqual(document_for(self.floorplan)["floors"][0]["designation"], "")

    def test_an_over_long_designation_is_refused_not_truncated(self) -> None:
        with self.assertRaises(ValueError):
            self._save([{"level": 0, "designation": "123456789"}])

    def test_the_name_is_independent_of_the_designation(self) -> None:
        self._save([{"level": 3, "designation": "M", "name": "Boiler level"}])

        floor = document_for(self.floorplan)["floors"][0]
        self.assertEqual(floor["designation"], "M")
        self.assertEqual(floor["name"], "Boiler level")
        self.assertEqual(floor["level"], 3)

    def test_two_floors_cannot_share_a_level(self) -> None:
        """Refused up front as a 400. The unique constraint is DEFERRED, so it
        would otherwise not fire until the outer commit - by which point the
        view can only answer 500."""
        with self.assertRaises(ValueError):
            self._save([{"level": 1}, {"level": 1}])

    def test_swapping_two_levels_in_one_save_commits(self) -> None:
        """The constraint has to be DEFERRED: save_document writes floors one
        row at a time inside a single transaction, so a swap necessarily
        collides part-way through and would fail a per-statement check."""
        self._save([{"level": 0, "designation": "G"}, {"level": 1, "designation": "1"}])
        floors = document_for(self.floorplan)["floors"]
        lower, upper = floors[0], floors[1]

        self._save(
            [
                {"uuid": str(lower["uuid"]), "level": 1, "designation": lower["designation"]},
                {"uuid": str(upper["uuid"]), "level": 0, "designation": upper["designation"]},
            ],
        )

        by_level = {f["level"]: f["designation"] for f in document_for(self.floorplan)["floors"]}
        self.assertEqual(by_level, {0: "1", 1: "G"})

    def test_a_mid_stack_renumber_in_one_save_commits(self) -> None:
        """The other shape the deferred constraint exists for: deleting a
        middle floor and closing the gap moves 2 onto 1's old level."""
        self._save([{"level": 0}, {"level": 1}, {"level": 2}])
        floors = document_for(self.floorplan)["floors"]

        self._save([{"uuid": str(floors[0]["uuid"]), "level": 0}, {"uuid": str(floors[2]["uuid"]), "level": 1}])

        self.assertEqual([f["level"] for f in document_for(self.floorplan)["floors"]], [0, 1])


class FloorplanOpeningRehostTests(TestCase):
    """A door dragged onto another wall keeps its identity, and its locks."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile, name="plan")
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]},
            profile=self.profile,
        )
        document = document_for(self.floorplan)
        self.walls = document["floors"][0]["walls"]

    def _walls_with(self, opening_on_index: int, opening: dict) -> list[dict]:
        out = []
        for index, wall in enumerate(self.walls):
            entry = {k: wall[k] for k in ("uuid", "kind", "ax", "ay", "bx", "by")}
            entry["openings"] = [opening] if index == opening_on_index else []
            out.append(entry)
        return out

    def test_moving_an_opening_to_another_wall_keeps_its_row(self) -> None:
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": self._walls_with(0, {"kind": "door", "t_start": 0.4, "t_end": 0.6})}]}, profile=self.profile)
        first = document_for(self.floorplan)["floors"][0]["walls"][0]["openings"][0]

        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": self._walls_with(2, {"uuid": str(first["uuid"]), "kind": "door", "t_start": 0.4, "t_end": 0.6})}]},
            profile=self.profile,
        )

        after = document_for(self.floorplan)["floors"][0]["walls"]
        self.assertEqual(after[0]["openings"], [])
        self.assertEqual(len(after[2]["openings"]), 1)
        self.assertEqual(str(after[2]["openings"][0]["uuid"]), str(first["uuid"]), "the opening was recreated instead of moved")

    def test_a_moved_opening_keeps_its_locks(self) -> None:
        """FloorplanLock cascades from the opening, so a delete-and-recreate
        would destroy the lock silently."""
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": self._walls_with(0, {"kind": "door", "t_start": 0.4, "t_end": 0.6, "locks": [{"name": "Padlock", "state": "locked"}]})}]},
            profile=self.profile,
        )
        first = document_for(self.floorplan)["floors"][0]["walls"][0]["openings"][0]
        moved = {"uuid": str(first["uuid"]), "kind": "door", "t_start": 0.4, "t_end": 0.6, "locks": [{"uuid": str(first["locks"][0]["uuid"]), "name": "Padlock", "state": "locked"}]}

        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": self._walls_with(1, moved)}]}, profile=self.profile)

        after = document_for(self.floorplan)["floors"][0]["walls"][1]["openings"][0]
        self.assertEqual(len(after["locks"]), 1)
        self.assertEqual(after["locks"][0]["name"], "Padlock")
        # The row itself, not a copy of its contents: a recreated opening
        # cascades its locks away and rebuilds them under new identities, which
        # reads as "the lock survived" while silently breaking anything holding
        # a reference to it.
        self.assertEqual(str(after["locks"][0]["uuid"]), str(first["locks"][0]["uuid"]))

    def test_an_opening_left_out_entirely_is_still_deleted(self) -> None:
        """The plan-wide match must not turn omission into permanence."""
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": self._walls_with(0, {"kind": "door", "t_start": 0.4, "t_end": 0.6})}]}, profile=self.profile)

        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": self._walls_with(99, {})}]}, profile=self.profile)

        walls = document_for(self.floorplan)["floors"][0]["walls"]
        self.assertEqual(sum(len(w["openings"]) for w in walls), 0)


class FloorplanFenceAndGateTests(TestCase):
    """A site is often a fence before it is a building."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile, name="plan")

    def _save(self, walls: list[dict]) -> None:
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": walls}]}, profile=self.profile)

    def test_a_fence_round_trips(self) -> None:
        self._save([{"kind": "fence", "ax": 0.0, "ay": 0.0, "bx": 10.0, "by": 0.0}])

        self.assertEqual(document_for(self.floorplan)["floors"][0]["walls"][0]["kind"], "fence")

    def test_a_gate_round_trips(self) -> None:
        self._save([{"kind": "fence", "ax": 0.0, "ay": 0.0, "bx": 10.0, "by": 0.0, "openings": [{"kind": "gate", "t_start": 0.4, "t_end": 0.6}]}])

        opening = document_for(self.floorplan)["floors"][0]["walls"][0]["openings"][0]
        self.assertEqual(opening["kind"], "gate")

    def test_an_unknown_wall_kind_is_still_refused(self) -> None:
        """Widening the enum must not have widened it to anything."""
        with self.assertRaises(ValueError):
            self._save([{"kind": "hedge", "ax": 0.0, "ay": 0.0, "bx": 1.0, "by": 0.0}])

    def test_a_gap_in_a_fence_is_a_virtual_span_and_still_encloses(self) -> None:
        """A missing run is a stretch where nothing is built, not an opening cut
        into fabric that continues - and it still bounds the yard, which is what
        makes the enclosed area nameable."""
        corners = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        walls = []
        for index, (ax, ay) in enumerate(corners):
            bx, by = corners[(index + 1) % 4]
            walls.append({"kind": "virtual" if index == 2 else "fence", "ax": ax, "ay": ay, "bx": bx, "by": by})
        self._save(walls)

        kinds = [wall["kind"] for wall in document_for(self.floorplan)["floors"][0]["walls"]]
        self.assertEqual(kinds.count("fence"), 3)
        self.assertEqual(kinds.count("virtual"), 1)


class FloorplanConcurrencyTests(TestCase):
    """Two tabs on one plan must not silently overwrite each other."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile, name="plan")
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]}, profile=self.profile)

    def test_a_save_carrying_the_current_token_is_accepted(self) -> None:
        document = document_for(self.floorplan)
        self.assertTrue(document["version_token"])

        save_document(self.floorplan, document, profile=self.profile)

    def test_a_save_built_on_a_replaced_version_is_refused(self) -> None:
        """The second tab's document is valid; it is just no longer current, and
        a whole-document save deletes by omission."""
        from urbanlens.dashboard.services.floorplans.serialization import StaleDocumentError

        first_tab = document_for(self.floorplan)
        second_tab = document_for(self.floorplan)
        # A real edit, not a re-send: the token moves when the plan does.
        first_tab["name"] = "renamed by the other tab"
        save_document(self.floorplan, first_tab, profile=self.profile)
        self.floorplan.refresh_from_db()

        with self.assertRaises(StaleDocumentError):
            save_document(self.floorplan, second_tab, profile=self.profile)

    def test_a_document_with_no_token_still_saves(self) -> None:
        """An older client, and a deliberate fork, both send none - refusing
        those to catch a rarer problem is the wrong trade."""
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]}, profile=self.profile)

    def test_the_endpoint_answers_409_rather_than_400(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        user = baker.make(User)
        location = baker.make(Location, latitude=41.7351, longitude=-73.9351, place=self.place)
        pin = baker.make(Pin, profile=user.profile, location=location, parent_pin=None, slug="concurrent-pin")
        self.client.force_login(user)
        own = Floorplan.objects.create(place=self.place, profile=user.profile, pin=pin)
        save_document(own, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]}, profile=user.profile)
        stale = document_for(own)
        newer = document_for(own)
        newer["name"] = "renamed by the other tab"
        save_document(own, newer, profile=user.profile)
        own.refresh_from_db()

        response = self.client.post(
            f"/dashboard/map/pin/{pin.slug}/floorplan/save/",
            data=jsonlib.dumps(stale),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertTrue(response.json()["stale"])


class FloorplanDocumentLimitsTests(TestCase):
    """A malformed or hostile document is a 400, not a 500."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = _building()
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile)

    def test_an_over_long_name_is_refused_rather_than_reaching_postgres(self) -> None:
        """Django does not enforce max_length on save, so this used to surface
        as a DataError - a 500 saying nothing about which field was wrong."""
        with self.assertRaises(ValueError):
            save_document(self.floorplan, {"plan_origin": _ORIGIN, "name": "x" * 300, "floors": []}, profile=self.profile)

    def test_an_over_long_wall_name_is_refused(self) -> None:
        walls = _square_walls()
        walls[0]["name"] = "y" * 300
        with self.assertRaises(ValueError):
            save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": walls}]}, profile=self.profile)

    def test_too_many_floors_is_refused_before_anything_is_written(self) -> None:
        document = {"plan_origin": _ORIGIN, "floors": [{"level": index} for index in range(400)]}

        with self.assertRaises(ValueError):
            save_document(self.floorplan, document, profile=self.profile)

        self.assertEqual(self.floorplan.floors.count(), 0)

    def test_too_many_walls_on_one_floor_is_refused(self) -> None:
        walls = [{"kind": "interior", "ax": float(i), "ay": 0.0, "bx": float(i), "by": 1.0} for i in range(2100)]

        with self.assertRaises(ValueError):
            save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": walls}]}, profile=self.profile)

    def test_a_normal_plan_is_nowhere_near_the_ceilings(self) -> None:
        """The limits exist to stop one request writing a million rows, not to
        constrain anybody's building."""
        floors = [{"level": level, "walls": _square_walls()} for level in range(20)]

        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": floors}, profile=self.profile)

        self.assertEqual(self.floorplan.floors.count(), 20)


class FloorplanAutosaveCostTests(TestCase):
    """An autosave that changes nothing should cost almost nothing.

    The editor saves on a debounce after every edit, so this runs constantly.
    A whole-document save that rewrites every row regardless turns a
    one-character rename into a write across the entire plan.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        self.place = _building()
        location = baker.make(Location, latitude=41.7361, longitude=-73.9361, place=self.place)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None)
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.user.profile, pin=self.pin)
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [
                    {
                        "level": 0,
                        "walls": _square_walls(),
                        "rooms": [{"name": "Boiler room", "x": 5.0, "y": 5.0}],
                        "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7361, "lng": -73.9361}],
                    },
                ],
            },
            profile=self.user.profile,
        )

    def _resave_unchanged(self) -> int:
        """Save the plan exactly as it stands, and count the queries."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        document = document_for(self.floorplan)
        with CaptureQueriesContext(connection) as captured:
            save_document(self.floorplan, document, profile=self.user.profile)
        return len(captured)

    def test_an_unchanged_resave_stays_within_its_current_cost(self) -> None:
        """A ceiling on today's behaviour, not an endorsement of it.

        A whole-document save rewrites every row whether or not it changed, so a
        four-wall plan costs about 33 queries and a 400-wall one costs
        proportionally more - on a debounce, after every edit. Making that
        cheaper is recorded in docs/PROBLEMS.md; an attempt at it is what this
        test was written alongside, and it lost data (see the same entry).

        Until then this exists so the number cannot quietly grow.
        """
        cost = self._resave_unchanged()

        self.assertLess(cost, 45, f"an unchanged resave took {cost} queries")

    def test_an_unchanged_resave_leaves_every_row_alone(self) -> None:
        """Identity is the part that matters: rewriting a row it did not need to
        touch is wasted work, but *replacing* one loses whatever pointed at it."""
        before = {str(wall["uuid"]) for wall in document_for(self.floorplan)["floors"][0]["walls"]}

        self._resave_unchanged()

        after = {str(wall["uuid"]) for wall in document_for(self.floorplan)["floors"][0]["walls"]}
        self.assertEqual(before, after)

    def test_a_real_edit_still_lands(self) -> None:
        document = document_for(self.floorplan)
        document["floors"][0]["rooms"][0]["name"] = "Plant room"

        save_document(self.floorplan, document, profile=self.user.profile)

        self.assertEqual(document_for(self.floorplan)["floors"][0]["rooms"][0]["name"], "Plant room")


class FloorplanMarkerTests(TestCase):
    """Markers collapse five old tools into one table with a kind."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    def test_a_connector_id_ties_a_stair_across_two_floors(self) -> None:
        """A stack of floors reads as one building only when the vertical
        connections between them are identified."""
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [
                    {"level": 0, "markers": [{"kind": "stair", "name": "North stair", "x": 2.0, "y": 8.0, "connector_id": "north"}]},
                    {"level": 1, "markers": [{"kind": "stair", "name": "North stair", "x": 2.0, "y": 8.0, "connector_id": "north"}]},
                ],
            },
            profile=self.profile,
        )

        linked = FloorplanMarker.objects.filter(connector_id="north")
        self.assertEqual(linked.count(), 2)
        self.assertEqual({marker.floor.level for marker in linked}, {0, 1})

    def test_a_marker_keeps_its_facing(self) -> None:
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "facing_degrees": 217.5}]}]},
            profile=self.profile,
        )

        self.assertEqual(FloorplanMarker.objects.get().facing_degrees, 217.5)

    def test_an_unknown_marker_kind_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            save_document(
                self.floorplan,
                {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "portal", "x": 1.0, "y": 1.0}]}]},
                profile=self.profile,
            )


class FloorplanMarkerAppearanceTests(TestCase):
    """A marker's look is stored on its linked detail pin, and must survive a save."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        self.place = _building()
        location = baker.make(Location, latitude=41.7401, longitude=-73.9401, place=self.place)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None)
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.user.profile, pin=self.pin)

    def _save(self, marker: dict) -> dict:
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [marker]}]},
            profile=self.user.profile,
        )
        return document_for(self.floorplan)["floors"][0]["markers"][0]

    def test_an_icon_and_colour_survive_the_round_trip(self) -> None:
        """The document has always *read* these off the linked pin; nothing
        wrote them back, so anything set in the editor vanished on save."""
        saved = self._save({"kind": "hazard", "x": 1.0, "y": 2.0, "lat": 41.7401, "lng": -73.9401, "icon": "warning", "color": "#F44336"})

        self.assertEqual(saved["icon"], "warning")
        self.assertEqual(saved["color"], "#F44336")

    def test_clearing_a_colour_returns_the_kind_default(self) -> None:
        """Blank means "no override", which has to be distinguishable from a
        payload that simply did not mention the field."""
        first = self._save({"kind": "hazard", "x": 1.0, "y": 2.0, "lat": 41.7401, "lng": -73.9401, "icon": "warning", "color": "#F44336"})
        # Asserted before clearing: without it this test passes whenever the
        # colour is never written at all, which is the bug it exists to catch.
        self.assertEqual(first["color"], "#F44336")

        saved = self._save({"uuid": str(first["uuid"]), "kind": "hazard", "x": 1.0, "y": 2.0, "lat": 41.7401, "lng": -73.9401, "icon": "", "color": ""})

        self.assertIsNone(saved["color"])

    def test_appearance_is_not_stored_on_the_marker(self) -> None:
        """One value, not two: FloorplanMarker must stay free of appearance
        columns or the pin page and the floorplan can disagree."""
        self._save({"kind": "hazard", "x": 1.0, "y": 2.0, "lat": 41.7401, "lng": -73.9401, "icon": "warning", "color": "#F44336"})

        marker = self.floorplan.floors.first().markers.first()
        self.assertFalse(hasattr(marker, "color"))
        self.assertEqual(marker.linked_pin.color, "#F44336")
        self.assertEqual(marker.linked_pin.icon, "warning")


class FloorplanMarkerLinkedPinTests(TestCase):
    """A marker on a personal, pin-owned plan is also a detail pin elsewhere on the site."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.place = _building()
        location = baker.make(Location, latitude=41.733, longitude=-73.93, place=self.place)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, slug="the-parent-pin")
        self.floorplan = Floorplan.objects.create(place=self.place, pin=self.pin, profile=self.profile)

    def test_a_marker_creates_a_linked_detail_pin(self) -> None:
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "name": "Wet floor", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )

        marker = FloorplanMarker.objects.get()
        self.assertIsNotNone(marker.linked_pin)
        linked = marker.linked_pin
        self.assertEqual(linked.parent_pin_id, self.pin.pk)
        self.assertEqual(linked.profile_id, self.profile.pk)
        self.assertEqual(linked.name, "Wet floor")
        self.assertEqual(linked.pin_type, "danger")
        self.assertTrue(linked.pin_type_is_user_provided)
        self.assertAlmostEqual(float(linked.location.latitude), 41.7331, places=4)
        self.assertAlmostEqual(float(linked.location.longitude), -73.9299, places=4)

    def test_stair_and_elevator_kinds_map_to_their_own_pin_type(self) -> None:
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [
                    {
                        "level": 0,
                        "markers": [
                            {"kind": "stair", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299},
                            {"kind": "elevator", "x": 2.0, "y": 2.0, "lat": 41.7332, "lng": -73.9298},
                        ],
                    },
                ],
            },
            profile=self.profile,
        )

        kinds = {marker.kind: marker.linked_pin.pin_type for marker in FloorplanMarker.objects.select_related("linked_pin")}
        self.assertEqual(kinds, {"stair": "stair", "elevator": "elevator"})

    def test_a_marker_without_coordinates_gets_no_linked_pin(self) -> None:
        """An older client that never sends lat/lng shouldn't fail to save the rest of the document."""
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0}]}]},
            profile=self.profile,
        )

        self.assertIsNone(FloorplanMarker.objects.get().linked_pin)

    def test_a_marker_on_a_pinless_plan_gets_no_linked_pin(self) -> None:
        """The wiki-published copy of a plan (see publish_to_wiki) has no owning pin to
        parent a detail pin under, and must never reach across to another profile's own."""
        placeless = Floorplan.objects.create(place=self.place, profile=self.profile)  # pin left unset

        save_document(
            placeless,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )

        self.assertIsNone(FloorplanMarker.objects.get().linked_pin)

    def test_deleting_a_marker_from_the_document_deletes_its_linked_pin(self) -> None:
        from urbanlens.dashboard.models.pin.model import Pin

        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )
        marker = FloorplanMarker.objects.get()
        linked_pk = marker.linked_pin_id
        floor_uuid = str(marker.floor.uuid)

        # Same floor uuid, but its markers list is now empty - an in-place
        # edit, not a floor being torn down and rebuilt.
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"uuid": floor_uuid, "level": 0, "markers": []}]}, profile=self.profile)

        self.assertFalse(FloorplanMarker.objects.exists())
        self.assertFalse(Pin.objects.filter(pk=linked_pk).exists())

    def test_deleting_the_linked_pin_deletes_the_marker(self) -> None:
        """CASCADE the other way too: removing a detail pin from the pin page
        must not leave a floorplan marker pointing at nothing."""
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )
        marker = FloorplanMarker.objects.get()
        linked = marker.linked_pin

        linked.delete()

        self.assertFalse(FloorplanMarker.objects.filter(pk=marker.pk).exists())

    def test_deleting_a_whole_floor_takes_its_markers_linked_pins_with_it(self) -> None:
        """A floor going away (torn down and redrawn, or the whole plan
        deleted) cascades to its markers through Django's own FK collector,
        never through serialization.py's per-marker sync - the linked-pin
        cleanup has to catch that path too, not just an in-place marker edit."""
        from urbanlens.dashboard.models.pin.model import Pin

        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )
        marker = FloorplanMarker.objects.get()
        linked_pk = marker.linked_pin_id

        marker.floor.delete()

        self.assertFalse(FloorplanMarker.objects.exists())
        self.assertFalse(Pin.objects.filter(pk=linked_pk).exists())

    def test_two_floors_can_share_the_exact_same_ground_point(self) -> None:
        """A stairwell sits at the same lat/lng on every storey it passes
        through - resolve_child_pin_location's "no two of one profile's pins
        at the exact point" rule must not apply here."""
        save_document(
            self.floorplan,
            {
                "plan_origin": _ORIGIN,
                "floors": [
                    {"level": 0, "markers": [{"kind": "stair", "name": "Main stair", "x": 2.0, "y": 8.0, "connector_id": "main", "lat": 41.7331, "lng": -73.9299}]},
                    {"level": 1, "markers": [{"kind": "stair", "name": "Main stair", "x": 2.0, "y": 8.0, "connector_id": "main", "lat": 41.7331, "lng": -73.9299}]},
                ],
            },
            profile=self.profile,
        )

        markers = list(FloorplanMarker.objects.select_related("linked_pin__location"))
        self.assertEqual(len(markers), 2)
        pins = {marker.linked_pin_id for marker in markers}
        self.assertEqual(len(pins), 2, "each floor's marker gets its own pin")
        locations = {marker.linked_pin.location_id for marker in markers}
        self.assertEqual(len(locations), 1, "siblings on the same ground point share one Location")

    def test_moving_a_marker_moves_its_linked_pin(self) -> None:
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )
        marker = FloorplanMarker.objects.get()
        uuid = str(marker.uuid)
        floor_uuid = str(marker.floor.uuid)

        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"uuid": floor_uuid, "level": 0, "markers": [{"uuid": uuid, "kind": "hazard", "x": 5.0, "y": 5.0, "lat": 41.7355, "lng": -73.9311}]}]},
            profile=self.profile,
        )

        marker.refresh_from_db()
        self.assertAlmostEqual(float(marker.linked_pin.location.latitude), 41.7355, places=4)
        self.assertAlmostEqual(float(marker.linked_pin.location.longitude), -73.9311, places=4)

    def test_document_for_prefers_the_linked_pins_own_name_and_style(self) -> None:
        """The pin is the freshest copy once it exists - it may have been
        renamed or restyled from the pin detail page since this marker was
        last saved here."""
        save_document(
            self.floorplan,
            {"plan_origin": _ORIGIN, "floors": [{"level": 0, "markers": [{"kind": "hazard", "name": "Wet floor", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}]},
            profile=self.profile,
        )
        marker = FloorplanMarker.objects.get()
        linked = marker.linked_pin
        linked.name = "Renamed from the pin page"
        linked.icon = "warning"
        linked.color = "#ff0000"
        linked.save()

        document = document_for(self.floorplan)

        out = document["floors"][0]["markers"][0]
        self.assertEqual(out["name"], "Renamed from the pin page")
        self.assertEqual(out["icon"], "warning")
        self.assertEqual(out["color"], "#ff0000")


class FloorplanSessionItemIdentityTests(TestCase):
    """A session-created item's row must survive a second save.

    ``_sync()`` matches a payload item to an existing row purely by uuid and
    deletes anything left unmatched as an orphan - so the *client* is the one
    responsible for round-tripping the real uuid a save just assigned. This
    reproduces exactly what the editor's fixed ``save()`` sends on its second
    autosave: the same document, with every item's uuid replaced by whatever
    the first save's response returned for the item at that position (see
    ``applyServerIds()`` in ``frontend/ts/entries/floorplan-editor.ts``).
    Before that merge existed, a second save reused nothing - it deleted and
    recreated every floor/wall/room/marker under a new pk (and, for a marker,
    a new linked ``Pin``) on every autosave after the first.
    """

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        self.user = baker.make(User)
        self.profile = self.user.profile
        self.place = _building()
        location = baker.make(Location, latitude=41.733, longitude=-73.93, place=self.place)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, slug="the-parent-pin")
        self.floorplan = Floorplan.objects.create(place=self.place, pin=self.pin, profile=self.profile)

    @staticmethod
    def _merge_server_uuids(original: dict, saved: dict) -> dict:
        """What the fixed editor's second save sends - see applyServerIds()."""
        merged = jsonlib.loads(jsonlib.dumps(original))
        for floor, saved_floor in zip(merged.get("floors", []), saved.get("floors", []), strict=False):
            floor["uuid"] = saved_floor["uuid"]
            for wall, saved_wall in zip(floor.get("walls", []), saved_floor.get("walls", []), strict=False):
                wall["uuid"] = saved_wall["uuid"]
                for opening, saved_opening in zip(wall.get("openings", []), saved_wall.get("openings", []), strict=False):
                    opening["uuid"] = saved_opening["uuid"]
            for room, saved_room in zip(floor.get("rooms", []), saved_floor.get("rooms", []), strict=False):
                room["uuid"] = saved_room["uuid"]
            for marker, saved_marker in zip(floor.get("markers", []), saved_floor.get("markers", []), strict=False):
                marker["uuid"] = saved_marker["uuid"]
        return merged

    def test_walls_a_room_and_a_marker_keep_their_row_across_a_second_save(self) -> None:
        document = {
            "plan_origin": _ORIGIN,
            "floors": [
                {
                    "level": 0,
                    "walls": _square_walls(),
                    "rooms": [{"name": "Great room", "x": 5.0, "y": 5.0}],
                    "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}],
                },
            ],
        }
        save_document(self.floorplan, document, profile=self.profile)
        first_save = document_for(self.floorplan)

        floor_pk = FloorplanFloor.objects.get().pk
        wall_pks = set(FloorplanWall.objects.values_list("pk", flat=True))
        room_pk = FloorplanRoomSeed.objects.get().pk
        marker = FloorplanMarker.objects.get()
        marker_pk = marker.pk
        linked_pin_pk = marker.linked_pin_id

        second_payload = self._merge_server_uuids(document, first_save)
        save_document(self.floorplan, second_payload, profile=self.profile)

        self.assertEqual(FloorplanFloor.objects.get().pk, floor_pk, "the floor was destroyed and recreated")
        self.assertEqual(set(FloorplanWall.objects.values_list("pk", flat=True)), wall_pks, "walls were destroyed and recreated")
        self.assertEqual(FloorplanRoomSeed.objects.get().pk, room_pk, "the room seed was destroyed and recreated")
        second_marker = FloorplanMarker.objects.get()
        self.assertEqual(second_marker.pk, marker_pk, "the marker was destroyed and recreated")
        self.assertEqual(second_marker.linked_pin_id, linked_pin_pk, "the marker's linked pin churned to a new row")

    def test_without_the_uuid_merge_a_second_save_does_churn(self) -> None:
        """Documents the failure mode the fix above closes: the same second
        save, but built the way the *old*, unfixed save() built it - carrying
        forward only the top-level document uuid, leaving every nested item's
        client-only local id untouched."""
        document = {
            "plan_origin": _ORIGIN,
            "floors": [{"level": 0, "markers": [{"kind": "hazard", "x": 1.0, "y": 1.0, "lat": 41.7331, "lng": -73.9299}]}],
        }
        save_document(self.floorplan, document, profile=self.profile)
        marker_pk = FloorplanMarker.objects.get().pk
        linked_pin_pk = FloorplanMarker.objects.get().linked_pin_id

        # Same document sent again, uuids untouched - what the old save() did.
        save_document(self.floorplan, document, profile=self.profile)

        second_marker = FloorplanMarker.objects.get()
        self.assertNotEqual(second_marker.pk, marker_pk)
        self.assertNotEqual(second_marker.linked_pin_id, linked_pin_pk)


class FloorplanDocumentContractTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    def test_an_out_of_range_level_is_a_message_not_a_500(self) -> None:
        with self.assertRaises(ValueError) as caught:
            save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 99_999}]}, profile=self.profile)

        self.assertIn("level", str(caught.exception))

    def test_a_floor_carries_no_outline_of_its_own(self) -> None:
        """The storey's shape is whatever its walls enclose; a stored outline
        would be a second, divergent answer to the same question."""
        save_document(self.floorplan, {"plan_origin": _ORIGIN, "floors": [{"level": 0, "walls": _square_walls()}]}, profile=self.profile)

        self.assertNotIn("geometry", document_for(self.floorplan)["floors"][0])


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
        likeliest evidence for a wall or the door cut into it."""
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


class PlacelessFloorplanTests(TestCase):
    """A plan need not belong to a known building outline.

    Most pins on a hand-mapped site resolve to no building place at all (no
    provider has an outline for a derelict structure), and refusing to save
    those made the editor a dead end for exactly the buildings most worth
    drawing. The pin is the plan's identity in that case.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        # Deliberately no place: this is the "no building outline" case.
        location = baker.make(Location, latitude=41.733, longitude=-73.928, place=None)
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, slug="placeless-pin")

    def _save(self, document: dict):
        return self.client.post(
            f"/dashboard/map/pin/{self.pin.slug}/floorplan/save/",
            data=jsonlib.dumps(document),
            content_type="application/json",
        )

    def _document(self) -> dict:
        return {
            "name": "Sketch",
            "plan_origin": _ORIGIN,
            "floors": [{"level": 0, "name": "Ground", "walls": _square_walls(), "rooms": [{"name": "Hall", "x": 5.0, "y": 5.0}]}],
        }

    def test_saving_a_plan_for_a_pin_with_no_building_succeeds(self) -> None:
        response = self._save(self._document())

        self.assertEqual(response.status_code, 200)

    def test_the_saved_plan_has_no_place_and_belongs_to_the_pin(self) -> None:
        self._save(self._document())

        plan = Floorplan.objects.get(pin=self.pin)
        self.assertIsNone(plan.place)
        self.assertEqual(plan.profile_id, self.user.profile.pk)

    def test_a_placeless_plan_round_trips(self) -> None:
        self._save(self._document())

        response = self.client.get(f"/dashboard/map/pin/{self.pin.slug}/floorplan/json/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "Sketch")
        self.assertEqual(len(body["floors"][0]["walls"]), 4)
        self.assertEqual(body["floors"][0]["rooms"][0]["name"], "Hall")

    def test_saving_twice_updates_rather_than_piling_up_versions(self) -> None:
        first = self._save(self._document()).json()["floorplan"]
        again = {**self._document(), "uuid": first["uuid"], "name": "Sketch v2"}

        self._save(again)

        self.assertEqual(Floorplan.objects.filter(pin=self.pin).count(), 1)
        self.assertEqual(Floorplan.objects.get(pin=self.pin).name, "Sketch v2")

    def test_one_users_placeless_plan_is_not_served_to_another(self) -> None:
        """`place=None` must not become a global bucket every placeless plan shares."""
        self._save(self._document())
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        other_user = baker.make(User)
        other_location = baker.make(Location, latitude=42.1, longitude=-74.1, place=None)
        other_pin = baker.make(Pin, profile=other_user.profile, location=other_location, parent_pin=None, slug="other-placeless")
        self.client.force_login(other_user)

        response = self.client.get(f"/dashboard/map/pin/{other_pin.slug}/floorplan/json/")

        self.assertEqual(response.status_code, 204)

    def test_editing_requires_a_place_or_a_pin(self) -> None:
        """Neither would leave the row unreachable by any lookup path."""
        from urbanlens.dashboard.services.floorplans.resolution import floorplan_for_editing

        with pytest.raises(ValueError, match="place or a pin"):
            floorplan_for_editing(None, self.user.profile)


class FloorplanResponseOrderTests(TestCase):
    """The order a saved document comes back in, which the editor relies on.

    After a save the editor copies the returned uuids back onto the objects it
    sent, so that a newly drawn wall keeps its identity instead of being created
    again on the next save. Floors are matched by level and items within a floor
    by position, and both of those are claims about this ordering.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(place=_building(), profile=self.profile)

    def test_floors_come_back_in_storey_order_whatever_order_they_were_sent(self) -> None:
        """Which is why the editor cannot match floors positionally."""
        sent = {
            "floors": [
                {"level": 0, "name": "Ground", "walls": _square_walls(), "rooms": [], "markers": []},
                {"level": 2, "name": "Second", "walls": [], "rooms": [], "markers": []},
                {"level": -1, "name": "Basement", "walls": [], "rooms": [], "markers": []},
            ],
        }
        save_document(self.floorplan, sent, profile=self.profile)

        levels = [floor["level"] for floor in document_for(self.floorplan)["floors"]]
        self.assertEqual(levels, [-1, 0, 2])

    def test_items_within_a_floor_come_back_in_the_order_they_were_sent(self) -> None:
        """Which is why matching them positionally is sound."""
        walls = _square_walls()
        walls[0] = {**walls[0], "name": "first"}
        walls[1] = {**walls[1], "name": "second"}
        walls[2] = {**walls[2], "name": "third"}
        markers = [
            {"kind": "stair", "x": 1.0, "y": 1.0, "name": "alpha"},
            {"kind": "hazard", "x": 2.0, "y": 2.0, "name": "beta"},
        ]
        save_document(self.floorplan, {"floors": [{"level": 0, "walls": walls, "rooms": [], "markers": markers}]}, profile=self.profile)

        floor = document_for(self.floorplan)["floors"][0]
        self.assertEqual([wall["name"] for wall in floor["walls"][:3]], ["first", "second", "third"])
        self.assertEqual([marker["name"] for marker in floor["markers"]], ["alpha", "beta"])

    def test_a_doors_locks_come_back_in_the_order_they_were_sent(self) -> None:
        """The editor matches locks to rows by position, same as everything else."""
        walls = _square_walls()
        walls[0] = {
            **walls[0],
            "openings": [
                {
                    "kind": "door",
                    "t_start": 0.4,
                    "t_end": 0.6,
                    "swing": "none",
                    "locks": [
                        {"name": "padlock", "state": "locked"},
                        {"name": "deadbolt", "state": "unlocked"},
                        {"name": "chain", "state": "unknown"},
                    ],
                },
            ],
        }
        save_document(self.floorplan, {"floors": [{"level": 0, "walls": walls, "rooms": [], "markers": []}]}, profile=self.profile)

        locks = document_for(self.floorplan)["floors"][0]["walls"][0]["openings"][0]["locks"]
        self.assertEqual([lock["name"] for lock in locks], ["padlock", "deadbolt", "chain"])
        self.assertEqual([lock["state"] for lock in locks], ["locked", "unlocked", "unknown"])

    def test_a_floors_level_is_unique_so_it_can_serve_as_the_key(self) -> None:
        """Matching floors by level is only valid because two cannot share one."""
        with pytest.raises(ValueError, match="share level"):
            save_document(
                self.floorplan,
                {"floors": [{"level": 1, "walls": [], "rooms": [], "markers": []}, {"level": 1, "walls": [], "rooms": [], "markers": []}]},
                profile=self.profile,
            )
