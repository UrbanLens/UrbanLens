"""Nothing one account does changes another account's private pin.

Locations are shared rows keyed by coordinate, and places and wikis are community data, so another account's
lookups may fill in what a pin reads from them. The pin's own row, and every row hanging off it, belongs to its
owner alone: each test here takes a snapshot of user B's pin, has user A do something that reaches B's pin
through shared data, and compares.

P181 is the reported case: A's campus sweep put a building's wiki on B's coordinate, pointed B's Location at a
building place that has no footprint, and retyped B's pin as a building.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources
from urbanlens.dashboard.services.locations.site_scope import reclassify_markers_on_place
from urbanlens.dashboard.services.pins import pin_restructure
from urbanlens.dashboard.services.places.oversized import detach_oversized_place
from urbanlens.dashboard.services.places.scope import effective_pin_type

#: User B's pin, in a courtyard on a campus.
B_POINT = (41.73266, -73.92736)
#: User A's campus pin, elsewhere on the same parcel.
A_POINT = (41.73328, -73.92812)

WIKIPEDIA_MATCH = {
    "title": "Hudson River State Hospital",
    "url": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital",
    "extract": "<p>A former psychiatric hospital.</p>",
}


def snapshot(pin: Pin) -> dict[str, Any]:
    """Every column of the pin, and every row that points at it, as stored."""
    pin = Pin.objects.get(pk=pin.pk)
    state: dict[str, Any] = {"pin": {field.attname: getattr(pin, field.attname) for field in Pin._meta.concrete_fields}}
    for relation in Pin._meta.related_objects:
        accessor = relation.get_accessor_name()
        if accessor is None or not (relation.one_to_many or relation.one_to_one):
            continue
        rows = relation.related_model._default_manager.filter(**{relation.field.name: pin}).order_by("pk")
        state[accessor] = list(rows.values())
    for field in Pin._meta.many_to_many:
        state[field.name] = sorted(getattr(pin, field.name).values_list("pk", flat=True))
    return state


class _TwoAccountsOnOneCampus(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.a = baker.make(User).profile
        self.b = baker.make(User).profile
        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL, building_child_count=5)
        a_location = baker.make(Location, latitude=A_POINT[0], longitude=A_POINT[1], place=self.parcel)
        self.b_location = baker.make(Location, latitude=B_POINT[0], longitude=B_POINT[1], place=self.parcel)
        self.a_pin = baker.make(
            Pin,
            profile=self.a,
            location=a_location,
            parent_pin=None,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=False,
        )
        self.b_pin = baker.make(
            Pin,
            profile=self.b,
            location=self.b_location,
            parent_pin=None,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=False,
        )
        self.before = snapshot(self.b_pin)

    def assertBsPinUnchanged(self) -> None:
        self.assertEqual(snapshot(self.b_pin), self.before)


class SharedPlaceDataNeverRetypesAnotherAccountsPinTests(_TwoAccountsOnOneCampus):
    def test_a_building_footprint_found_under_the_pin(self) -> None:
        building = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel, domain_root=self.parcel)
        Location.objects.filter(pk=self.b_location.pk).update(place=building)

        reclassify_markers_on_place(building)

        self.assertBsPinUnchanged()

    def test_an_oversized_place_detached_from_the_pin(self) -> None:
        detach_oversized_place(self.parcel)

        self.assertBsPinUnchanged()


class AnotherAccountsCampusSweepTests(_TwoAccountsOnOneCampus):
    """A's sweep mirrors a building whose point is B's coordinate (P181)."""

    def setUp(self) -> None:
        super().setUp()
        self.buildings = [
            {
                "ref": "osm:relation/10813427",
                "name": "Kirkbride (Admin Building)",
                "latitude": B_POINT[0],
                "longitude": B_POINT[1],
                "is_on_property": True,
            },
            {
                "ref": "cris:45",
                "name": "MORTUARY & LAB",
                "latitude": A_POINT[0] + 0.0005,
                "longitude": A_POINT[1],
                "is_on_property": True,
            },
        ]

    def test_mirroring_the_buildings_wikis(self) -> None:
        pin_restructure.mirror_buildings_to_wiki(self.a_pin, self.buildings, self.a)

        self.assertBsPinUnchanged()

    def test_the_shared_location_keeps_the_place_containment_gives_it(self) -> None:
        pin_restructure.mirror_buildings_to_wiki(self.a_pin, self.buildings, self.a)

        self.b_location.refresh_from_db()
        self.assertEqual(
            self.b_location.place_id, self.parcel.pk, "the sweep pointed B's coordinate at a place by fiat"
        )

    def test_creating_the_campus_owners_child_pins(self) -> None:
        nester = pin_restructure.BuildingNester.for_pin(self.a_pin, fallback=self.buildings)
        nester.create_pins(set(range(len(nester.clusters))))

        self.assertBsPinUnchanged()
        self.b_location.refresh_from_db()
        self.assertEqual(self.b_location.place_id, self.parcel.pk)

    def test_the_campus_owners_building_pins_still_read_as_buildings(self) -> None:
        nester = pin_restructure.BuildingNester.for_pin(self.a_pin, fallback=self.buildings)

        created = nester.create_pins(set(range(len(nester.clusters))))

        self.assertTrue(created)
        for pin in Pin.objects.filter(pk__in=[pin.pk for pin in created.values()]).select_related("location__place"):
            self.assertEqual((pin.pin_type, effective_pin_type(pin)), (PinType.BUILDING, PinType.BUILDING))


class TheOwnersVisitRederivesTheTypeTests(_TwoAccountsOnOneCampus):
    def test_opening_the_pin_page_takes_the_type_its_place_implies(self) -> None:
        building = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel, domain_root=self.parcel)
        Location.objects.filter(pk=self.b_location.pk).update(place=building)
        self.client.force_login(self.b.user)

        self.client.get(f"/dashboard/map/pin/{self.b_pin.slug}/")

        self.b_pin.refresh_from_db()
        self.assertEqual(self.b_pin.pin_type, PinType.BUILDING)

    def test_a_type_the_owner_chose_is_kept(self) -> None:
        Pin.objects.filter(pk=self.b_pin.pk).update(pin_type=PinType.PARCEL, pin_type_is_user_provided=True)
        building = baker.make(Place, kind=PlaceKind.BUILDING, parent=self.parcel, domain_root=self.parcel)
        Location.objects.filter(pk=self.b_location.pk).update(place=building)
        self.client.force_login(self.b.user)

        self.client.get(f"/dashboard/map/pin/{self.b_pin.slug}/")

        self.b_pin.refresh_from_db()
        self.assertEqual(self.b_pin.pin_type, PinType.PARCEL)


class AnotherAccountsLookupsTests(_TwoAccountsOnOneCampus):
    """A's lookups cache names and articles on B's shared Location."""

    def test_a_wikipedia_match_first_found_by_another_account(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(self.b_location, "wikipedia", WIKIPEDIA_MATCH, query_key="theirs")

        self.assertBsPinUnchanged()

    def test_a_name_refresh_another_account_triggered(self) -> None:
        LocationCache.set(self.b_location, "wikipedia", WIKIPEDIA_MATCH, query_key="theirs")
        self.before = snapshot(self.b_pin)

        update_location_name_from_external_sources(self.b_location, profile=self.a)

        self.assertBsPinUnchanged()

    def test_a_name_refresh_another_account_triggered_prunes_nothing(self) -> None:
        PinAlias.objects.create(pin=self.b_pin, name="Elm Street", kind=AliasType.OFFICIAL, source="nominatim")
        LocationCache.set(
            self.b_location,
            "nominatim",
            {"name": "Elm Street", "category": "highway", "type": "residential"},
            query_key="theirs",
        )
        self.before = snapshot(self.b_pin)

        update_location_name_from_external_sources(self.b_location, profile=self.a)

        self.assertBsPinUnchanged()

    def test_another_account_opening_the_wikis_alias_panel(self) -> None:
        baker.make(Pin, profile=self.a, location=self.b_location, parent_pin=self.a_pin, pin_type=PinType.BUILDING)
        Wiki.objects.get_or_create_for_location(self.b_location)
        LocationCache.set(self.b_location, "wikipedia", WIKIPEDIA_MATCH, query_key="theirs")
        self.before = snapshot(self.b_pin)
        self.client.force_login(self.a.user)

        response = self.client.get(reverse("location.wiki.aliases", args=[self.b_location.ensure_slug()]))

        self.assertEqual(response.status_code, 200)
        self.assertBsPinUnchanged()


class TheOwnersVisitCatchesUpTests(_TwoAccountsOnOneCampus):
    """What A's lookups found reaches B's pin when B opens it."""

    def setUp(self) -> None:
        super().setUp()
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(self.b_location, "wikipedia", WIKIPEDIA_MATCH, query_key="theirs")
        self.client.force_login(self.b.user)

    def test_the_alias_panel_takes_the_official_names(self) -> None:
        self.client.get(reverse("pin.aliases", args=[self.b_pin.slug]))

        self.assertTrue(
            PinAlias.objects.filter(pin=self.b_pin, name=WIKIPEDIA_MATCH["title"], kind=AliasType.OFFICIAL).exists()
        )

    def test_the_pin_page_takes_the_wikipedia_link_and_article(self) -> None:
        self.client.get(f"/dashboard/map/pin/{self.b_pin.slug}/")

        self.assertTrue(PinLink.objects.filter(pin=self.b_pin, url=WIKIPEDIA_MATCH["url"]).exists())
        self.assertTrue(Article.objects.filter(pin=self.b_pin).exists())

    def test_a_wikipedia_link_the_owner_removed_stays_removed(self) -> None:
        self.client.get(f"/dashboard/map/pin/{self.b_pin.slug}/")
        link = PinLink.objects.get(pin=self.b_pin, url=WIKIPEDIA_MATCH["url"])
        self.client.delete(reverse("pin.link.delete", args=[self.b_pin.slug, link.pk]))

        self.client.get(f"/dashboard/map/pin/{self.b_pin.slug}/")

        self.assertFalse(PinLink.objects.filter(pin=self.b_pin, url=WIKIPEDIA_MATCH["url"]).exists())
