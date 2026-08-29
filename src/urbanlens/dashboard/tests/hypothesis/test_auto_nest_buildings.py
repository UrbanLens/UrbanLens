"""Confident buildings become child pins by default; ambiguous ones wait.

A new pin on a multi-building property is a parcel pin, and its buildings are
what describe it - so the parcel/building split should be the default outcome,
not a reward for opening a dialog. The dividing line is confidence:
``overlap_refs`` marks the one relationship REData's reconciliation refuses to
resolve, so those records keep the existing approval flow (the "add buildings"
dialog) while everything else is created unprompted.

Control and cleanup are part of the design, not an afterthought:

- a profile toggle turns it off wholesale;
- the sweep is one-shot per pin, so deleting an auto-created child sticks;
- a dismissed restructure offer is honoured as a standing "no";
- an existing child hierarchy is the user's own arrangement and is left alone.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.auto_nest import auto_nest_location, auto_nest_pin

_LAT, _LNG = 41.73332, -73.92794


def _building(seq: int, **extra) -> dict:
    return {
        "ref": f"cris:{seq}",
        "name": f"Building {seq}",
        "latitude": _LAT + seq / 1000,
        "longitude": _LNG,
        "is_on_property": True,
        "sources": [{"source": "cris"}],
        **extra,
    }


class ConfidentBuildingsTests(SimpleTestCase):
    def test_an_unresolved_overlap_is_not_confident(self) -> None:
        """REData refused to say what this record is; so must we."""
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import confident_buildings

        records = [_building(1), _building(2, overlap_refs=["osm:way/9"])]

        self.assertEqual([b["ref"] for b in confident_buildings(records)], ["cris:1"])

    def test_containment_is_not_ambiguity(self) -> None:
        """A parent and its children are a verified relationship, kept on both sides."""
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import confident_buildings

        records = [_building(1, child_refs=["cris:2"]), _building(2, parent_ref="cris:1")]

        # A count-only assertion would pass a bug that returned the same record twice
        # instead of both distinct ones - check identity, not just length.
        self.assertEqual({b["ref"] for b in confident_buildings(records)}, {"cris:1", "cris:2"})

    def test_off_property_records_are_never_confident(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import confident_buildings

        self.assertEqual(confident_buildings([_building(1, is_on_property=False)]), [])


class AutoNestPinTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self._seq = 0

    def _pin(self, **kwargs) -> Pin:
        self._seq += 1
        location = baker.make(Location, latitude=_LAT + self._seq, longitude=_LNG)
        return baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name=f"Site {self._seq}", **kwargs)

    def _cache(self, pin: Pin, buildings: list[dict]) -> None:
        LocationCache.set(pin.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": buildings, "provider": "redata"}, query_key="test")

    def test_confident_buildings_become_child_pins(self) -> None:
        pin = self._pin()
        self._cache(pin, [_building(1), _building(2)])

        created = auto_nest_pin(pin)

        self.assertEqual(created, 2)
        self.assertEqual(pin.detail_pins.count(), 2)
        self.assertTrue(all(child.pin_type == PinType.BUILDING for child in pin.detail_pins.all()))

    def test_ambiguous_buildings_are_left_for_the_dialog(self) -> None:
        """The approval step survives exactly where approval means something."""
        pin = self._pin()
        self._cache(pin, [_building(1), _building(2), _building(3, overlap_refs=["osm:way/9"])])

        auto_nest_pin(pin)

        names = {child.name for child in pin.detail_pins.all()}
        self.assertEqual(names, {"Building 1", "Building 2"})

    def test_the_sweep_is_one_shot(self) -> None:
        """Deleting an auto-created child must stick; nothing may recreate it."""
        pin = self._pin()
        self._cache(pin, [_building(1), _building(2)])
        auto_nest_pin(pin)
        pin.detail_pins.all().delete()

        created_again = auto_nest_pin(pin)

        self.assertEqual(created_again, 0)
        self.assertEqual(pin.detail_pins.count(), 0)

    def test_a_single_building_property_stays_one_pin(self) -> None:
        """An ordinary house is not a campus, and stays unswept for later."""
        pin = self._pin()
        self._cache(pin, [_building(1)])

        self.assertEqual(auto_nest_pin(pin), 0)
        pin.refresh_from_db()
        self.assertIsNone(pin.buildings_auto_nested_at, "a below-threshold sweep must not stamp - more buildings may become known")

    def test_the_profile_toggle_turns_it_off(self) -> None:
        self.profile.auto_create_building_pins = False
        self.profile.save(update_fields=["auto_create_building_pins"])
        pin = self._pin()
        self._cache(pin, [_building(1), _building(2)])

        self.assertEqual(auto_nest_pin(pin), 0)
        self.assertEqual(pin.detail_pins.count(), 0)

    def test_a_dismissed_restructure_offer_is_a_standing_no(self) -> None:
        pin = self._pin(restructure_offer_dismissed=True)
        self._cache(pin, [_building(1), _building(2)])

        self.assertEqual(auto_nest_pin(pin), 0)

    def test_an_existing_hierarchy_is_the_users_own_arrangement(self) -> None:
        pin = self._pin()
        child_location = baker.make(Location, latitude=_LAT + self._seq + 0.5, longitude=_LNG)
        baker.make(Pin, profile=self.profile, location=child_location, parent_pin=pin, name="My child")
        self._cache(pin, [_building(1), _building(2)])

        self.assertEqual(auto_nest_pin(pin), 0)

    def test_a_user_typed_building_pin_is_not_a_parcel(self) -> None:
        pin = self._pin(pin_type=PinType.BUILDING, pin_type_is_user_provided=True)
        self._cache(pin, [_building(1), _building(2)])

        self.assertEqual(auto_nest_pin(pin), 0)

    def test_a_lone_parent_and_child_read_as_one_building(self) -> None:
        """One structure with a mapped part is still one building - no split.

        Distinctness is counted by leaves (see ``countable_buildings``), so an
        envelope over a single record does not clear the multi-building bar.
        """
        pin = self._pin()
        self._cache(pin, [_building(1, child_refs=["cris:2"]), _building(2, parent_ref="cris:1")])

        self.assertEqual(auto_nest_pin(pin), 0)

    def test_nested_buildings_nest_their_pins(self) -> None:
        """REData's building tree becomes the pin tree, not a flat list."""
        pin = self._pin()
        self._cache(pin, [_building(1, child_refs=["cris:2"]), _building(2, parent_ref="cris:1"), _building(3)])

        auto_nest_pin(pin)

        parent = pin.detail_pins.get(name="Building 1")
        child = Pin.objects.get(name="Building 2")
        self.assertEqual(child.parent_pin_id, parent.pk, "a contained building's pin should nest under its container's pin")

    def test_the_location_sweep_covers_every_root_pin(self) -> None:
        pin = self._pin()
        other = baker.make(Pin, profile=baker.make(User).profile, location=pin.location, parent_pin=None, name="Other user's")
        self._cache(pin, [_building(1), _building(2)])

        created = auto_nest_location(pin.location)

        self.assertEqual(created, 4, "what stands on a property is a fact about the property, for every user pinned there")
        self.assertEqual(other.detail_pins.count(), 2)
