"""Regression tests for the "switch" picker's candidate list, not just its access gate."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.places.ambiguity import competing_wiki_locations


def square(lng: float, lat: float, delta: float) -> MultiPolygon:
    """An axis-aligned square centred on a coordinate."""
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


def make_place(
    kind: str,
    geometry: MultiPolygon | None,
    *,
    parent: Place | None = None,
    relation: str = PlaceRelation.PART_OF,
    name: str = "",
) -> Place:
    """Create a place with its derived columns filled, as provisioning would."""
    place = Place.objects.create(kind=kind, geometry=geometry, name=name)
    if geometry is not None:
        resolution.refresh_area(place)
    if parent is not None:
        lineage.set_parent(place, parent, relation)
    return place


class SwitchCandidateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_an_unresolved_pin_has_no_switch_candidates(self) -> None:
        """Nothing can be said to compete with a coordinate that resolves to nothing."""
        rival = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), name="Rival Parcel")
        rival_location = Location.objects.create(latitude=40.0001, longitude=-74.0001)
        Wiki.objects.create(name="Rival Wiki", location=rival_location, place=rival)
        baker.make(Pin, profile=self.profile, location=rival_location)  # grants the rival's domain

        # The pin's own location resolves onto the rival at first - proving
        # the rival really would compete without the guard - then loses that
        # answer (a re-resolution mid-flight, a superseded place) with no
        # replacement yet.
        own_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.assertEqual(own_location.place_id, rival.pk)
        Location.objects.filter(pk=own_location.pk).update(place=None)
        own_location.refresh_from_db()
        pin = baker.make(Pin, profile=self.profile, location=own_location)

        self.assertEqual(competing_wiki_locations(pin, self.profile), [])

    def test_a_pins_own_parcel_is_never_a_switch_target(self) -> None:
        """A ``PART_OF`` ancestor must never be offered, even if domain_root drifted."""
        parcel = make_place(PlaceKind.PARCEL, square(-73.93, 41.73, 0.01), name="Campus Parcel")
        building = make_place(PlaceKind.BUILDING, square(-73.935, 41.73, 0.0002), parent=parcel, name="One Building")
        Place.objects.filter(pk=building.pk).update(domain_root=building.pk)
        building.refresh_from_db()

        parcel_location = Location.objects.create(latitude=41.7345, longitude=-73.9345)
        self.assertEqual(parcel_location.place_id, parcel.pk)
        Wiki.objects.create(name="Campus Wiki", location=parcel_location, place=parcel)
        baker.make(Pin, profile=self.profile, location=parcel_location)  # the profile already knows the campus

        building_location = Location.objects.create(latitude=41.730149, longitude=-73.935149)
        self.assertEqual(building_location.place_id, building.pk)
        pin = baker.make(Pin, profile=self.profile, location=building_location)

        self.assertEqual(competing_wiki_locations(pin, self.profile), [])

    def test_only_one_location_is_offered_per_competing_place(self) -> None:
        """One row per rival place, not one per Location that resolves onto it.

        Guards against the 124-Locations/one-parcel explosion the Place model was built to end (see the module
        docstring in ``ambiguity.py``) - two Locations here independently carry their own wiki while resolving
        onto the same rival, and only one may be offered."""
        own_place = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.001), name="Own Parcel")
        own_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.assertEqual(own_location.place_id, own_place.pk)
        pin = baker.make(Pin, profile=self.profile, location=own_location)

        rival = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), name="Rival Parcel")
        first = Location.objects.create(latitude=40.005, longitude=-74.005)
        second = Location.objects.create(latitude=39.995, longitude=-73.995)
        self.assertEqual({first.place_id, second.place_id}, {rival.pk})
        Wiki.objects.create(name="Rival Wiki One", location=first, place=None)
        Wiki.objects.create(name="Rival Wiki Two", location=second, place=None)
        baker.make(Pin, profile=self.profile, location=first)  # grants the rival's domain

        candidates = competing_wiki_locations(pin, self.profile)

        self.assertEqual(len(candidates), 1)

    def test_a_wiki_location_with_no_slug_is_not_offered(self) -> None:
        own_place = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.001), name="Own Parcel")
        own_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.assertEqual(own_location.place_id, own_place.pk)
        pin = baker.make(Pin, profile=self.profile, location=own_location)

        rival = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01), name="Rival Parcel")
        rival_location = Location.objects.create(latitude=40.005, longitude=-74.005)
        self.assertEqual(rival_location.place_id, rival.pk)
        Wiki.objects.create(name="Rival Wiki", location=rival_location, place=rival)
        baker.make(Pin, profile=self.profile, location=rival_location)  # grants the rival's domain
        Location.objects.filter(pk=rival_location.pk).update(slug=None)

        self.assertEqual(competing_wiki_locations(pin, self.profile), [])
