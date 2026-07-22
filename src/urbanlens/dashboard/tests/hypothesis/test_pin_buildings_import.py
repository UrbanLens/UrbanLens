"""Tests for the "add pins for the buildings here?" flow (controllers.pin_buildings).

Covers when the offer appears at all, the dismissal that silences it for good,
and the import itself: one child pin per unpinned building, mirrored to child
wikis only when the place already has a community wiki, summarized in a single
WikiEdit rather than one per building.

The buildings list is seeded straight into the LocationCache, so no external
service is contacted.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.locations.site_scope import MULTI_BUILDING_THRESHOLD, PARCEL_BUILDINGS_CACHE_SOURCE, is_site_scope

_coord_counter = 0

_BUILDINGS = [
    {"source": "cris", "name": "Tool Shed", "building_number": "154", "year_built": 1937, "latitude": 41.73320, "longitude": -73.93040},
    {"source": "cris", "name": "Main Hall", "building_number": "9", "year_built": 1892, "latitude": 41.73300, "longitude": -73.93000},
    {"source": "cris", "name": "", "building_number": "22", "year_built": None, "latitude": 41.73280, "longitude": -73.92960},
]


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 41.73 + _coord_counter * 0.0005)
    kwargs.setdefault("longitude", -73.93 - _coord_counter * 0.0005)
    return baker.make(Location, google_place=None, **kwargs)


class BuildingsOfferTests(TestCase):
    """When the prompt is shown, and when it stays quiet."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        self.url = reverse("pin.buildings.offer", kwargs={"pin_slug": self.pin.slug})

    def _cache(self, buildings) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": buildings, "provider": "redata"})

    def test_multi_building_parcel_gets_the_offer(self) -> None:
        self._cache(_BUILDINGS)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 buildings")
        self.assertContains(response, "Tool Shed")

    def test_a_single_building_place_is_never_offered(self) -> None:
        self._cache(_BUILDINGS[:1])
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_a_place_with_no_buildings_is_never_offered(self) -> None:
        self._cache([])
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_a_dismissed_pin_is_never_offered_again(self) -> None:
        self._cache(_BUILDINGS)
        Pin.objects.filter(pk=self.pin.pk).update(buildings_offer_dismissed=True)
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_an_already_modelled_parcel_is_not_re_offered(self) -> None:
        self._cache(_BUILDINGS)
        for _ in range(MULTI_BUILDING_THRESHOLD):
            baker.make(Pin, profile=self.user.profile, parent_pin=self.pin, location=_make_location(), pin_type=PinType.BUILDING)
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_a_child_pin_is_never_offered(self) -> None:
        self._cache(_BUILDINGS)
        child = baker.make(Pin, profile=self.user.profile, parent_pin=self.pin, location=_make_location(), slug="a-building")
        response = self.client.get(reverse("pin.buildings.offer", kwargs={"pin_slug": child.slug}))
        self.assertEqual(response.status_code, 204)

    def test_the_count_excludes_buildings_that_already_have_pins(self) -> None:
        self._cache(_BUILDINGS)
        baker.make(
            Pin,
            profile=self.user.profile,
            parent_pin=self.pin,
            pin_type=PinType.BUILDING,
            location=baker.make(Location, latitude="41.733200", longitude="-73.930400", google_place=None),
        )
        self.assertContains(self.client.get(self.url), "2 buildings")

    def test_an_uncached_parcel_polls_instead_of_blocking(self) -> None:
        """Pin creation must never wait on a REData round-trip."""
        with patch("urbanlens.dashboard.services.external_data.schedule_panel_fetch", return_value=True):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "attempt=1")

    def test_a_suppressed_or_unschedulable_lookup_gives_up_quietly(self) -> None:
        with patch("urbanlens.dashboard.services.external_data.schedule_panel_fetch", return_value=False):
            self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_polling_gives_up_once_the_budget_is_spent(self) -> None:
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS

        self.assertEqual(self.client.get(self.url, {"attempt": str(MAX_POLL_ATTEMPTS)}).status_code, 204)

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        self.assertEqual(self.client.get(reverse("pin.buildings.offer", kwargs={"pin_slug": other.slug})).status_code, 404)


class BuildingsDismissTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="campus")

    def test_dismissing_records_the_flag(self) -> None:
        response = self.client.post(reverse("pin.buildings.dismiss", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertTrue(self.pin.buildings_offer_dismissed)


class BuildingsImportTests(TestCase):
    """Creating one child pin per building."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        self.url = reverse("pin.buildings.import", kwargs={"pin_slug": self.pin.slug})

    def test_creates_one_child_pin_per_building(self) -> None:
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pin.detail_pins.count(), 3)

    def test_children_are_typed_as_buildings_without_claiming_a_user_choice(self) -> None:
        self.client.post(self.url)
        for child in self.pin.detail_pins.all():
            self.assertEqual(child.pin_type, PinType.BUILDING)
            self.assertFalse(child.pin_type_is_user_provided)

    def test_children_get_their_own_distinct_coordinates(self) -> None:
        """The dedup that normally snaps nearby pins together must not collapse a campus."""
        self.client.post(self.url)
        coordinates = {(str(c.location.latitude), str(c.location.longitude)) for c in self.pin.detail_pins.select_related("location")}
        self.assertEqual(len(coordinates), 3)

    def test_a_nameless_building_is_named_by_its_number(self) -> None:
        self.client.post(self.url)
        self.assertTrue(self.pin.detail_pins.filter(name="Building 22").exists())

    def test_import_makes_the_parent_a_parcel(self) -> None:
        self.client.post(self.url)
        self.assertTrue(is_site_scope(Pin.objects.get(pk=self.pin.pk)))

    def test_running_twice_does_not_duplicate_pins(self) -> None:
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertEqual(self.pin.detail_pins.count(), 3)

    def test_buildings_already_pinned_by_hand_are_skipped(self) -> None:
        baker.make(
            Pin,
            profile=self.user.profile,
            parent_pin=self.pin,
            pin_type=PinType.BUILDING,
            name="My Tool Shed",
            location=baker.make(Location, latitude="41.733200", longitude="-73.930400", google_place=None),
        )
        self.client.post(self.url)
        self.assertEqual(self.pin.detail_pins.count(), 3)
        self.assertTrue(self.pin.detail_pins.filter(name="My Tool Shed").exists())

    def test_nothing_to_import_reports_so_without_creating_anything(self) -> None:
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("already has a pin", response["HX-Trigger"])

    def test_the_response_asks_the_page_to_refresh_its_sub_pins(self) -> None:
        response = self.client.post(self.url)
        self.assertIn("pinDetailPinsChanged", response["HX-Trigger"])

    def test_no_cached_buildings_creates_nothing(self) -> None:
        pin = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="unknown-place")
        response = self.client.post(reverse("pin.buildings.import", kwargs={"pin_slug": pin.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(pin.detail_pins.count(), 0)

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        self.assertEqual(self.client.post(reverse("pin.buildings.import", kwargs={"pin_slug": other.slug})).status_code, 404)


class BuildingsImportWikiMirrorTests(TestCase):
    """Child wikis are contributed only when a community wiki already exists."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        self.url = reverse("pin.buildings.import", kwargs={"pin_slug": self.pin.slug})

    def test_no_wiki_means_no_wiki_is_created(self) -> None:
        """Wikis are never created automatically - only ever explicitly."""
        self.client.post(self.url)
        self.assertFalse(Wiki.objects.filter(location=self.location).exists())

    def test_an_existing_wiki_gets_matching_child_wikis(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        self.assertEqual(wiki.child_wikis.count(), 3)
        self.assertTrue(wiki.child_wikis.filter(pin_type=PinType.BUILDING).count() == 3)

    def test_the_import_is_one_wiki_edit_not_one_per_building(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        edits = WikiEdit.objects.filter(wiki=wiki)
        self.assertEqual(edits.count(), 1)
        self.assertIn("child_wikis_imported", edits.first().changes)

    def test_a_second_import_adds_no_further_child_wikis(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertEqual(wiki.child_wikis.count(), 3)
        self.assertEqual(WikiEdit.objects.filter(wiki=wiki).count(), 1)

    def test_the_wiki_becomes_a_parcel_too(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        self.assertTrue(is_site_scope(Wiki.objects.get(pk=wiki.pk)))
