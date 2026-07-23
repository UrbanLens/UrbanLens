"""Tests for the "organize this property?" suggestion (services/controllers pin_restructure).

Covers the two halves it offers together - creating a child pin per unpinned
building, and nesting the owner's existing top-level pins that stand inside the
property - plus the gating that keeps it to one dialog, once, per pin: the
account-wide setting, the permanent per-pin dismissal, and having nothing to
suggest.

Building footprints matter here: matching an existing child pin to a building
by its real polygon (not a radius from the centroid) is what stops the
suggestion from offering to re-pin buildings the user already covered.

The parcel building list is seeded straight into the LocationCache, so no
external service is contacted.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services import pin_restructure
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE, is_site_scope

_coord_counter = 0

#: A square footprint around (41.7332, -73.9304), ~110 m a side. Deliberately
#: much larger than BUILDING_MATCH_METERS so containment and proximity give
#: different answers and the tests can tell which one ran.
_SHED_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[-73.93090, 41.73270], [-73.92990, 41.73270], [-73.92990, 41.73370], [-73.93090, 41.73370], [-73.93090, 41.73270]]],
}

_BUILDINGS = [
    {"source": "cris", "name": "Tool Shed", "building_number": "154", "year_built": 1937, "latitude": 41.73320, "longitude": -73.93040, "geometry": _SHED_FOOTPRINT},
    {"source": "cris", "name": "Main Hall", "building_number": "9", "year_built": 1892, "latitude": 41.73300, "longitude": -73.93000},
    {"source": "cris", "name": "", "building_number": "22", "year_built": None, "latitude": 41.73280, "longitude": -73.92960},
]


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 41.75 + _coord_counter * 0.0005)
    kwargs.setdefault("longitude", -73.95 - _coord_counter * 0.0005)
    return baker.make(Location, google_place=None, **kwargs)


def _parcel_polygon() -> MultiPolygon:
    """A boundary comfortably containing every coordinate in ``_BUILDINGS``."""
    return MultiPolygon(
        Polygon(((-73.940, 41.725), (-73.920, 41.725), (-73.920, 41.740), (-73.940, 41.740), (-73.940, 41.725))),
        srid=4326,
    )


class BuildingFootprintTests(SimpleTestCase):
    """Parsing a building record's published geometry."""

    def test_a_polygon_geometry_is_parsed(self) -> None:
        footprint = pin_restructure.building_footprint(_BUILDINGS[0])
        assert footprint is not None
        self.assertEqual(footprint.dims, 2)

    def test_a_record_without_geometry_has_no_footprint(self) -> None:
        self.assertIsNone(pin_restructure.building_footprint(_BUILDINGS[1]))

    def test_a_point_geometry_is_not_a_footprint(self) -> None:
        """REData sends a Point when it has no real outline - nothing to contain."""
        self.assertIsNone(pin_restructure.building_footprint({"geometry": {"type": "Point", "coordinates": [-73.93, 41.733]}}))

    def test_malformed_geometry_degrades_to_no_footprint(self) -> None:
        self.assertIsNone(pin_restructure.building_footprint({"geometry": {"type": "Polygon", "coordinates": "nonsense"}}))


class MarkerCoversBuildingTests(TestCase):
    """Containment first, centroid radius only as the fallback."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")

    def _pin_at(self, latitude: float, longitude: float) -> Pin:
        return baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=latitude, longitude=longitude, google_place=None))

    def test_a_pin_inside_the_footprint_counts_however_far_from_the_centroid(self) -> None:
        """A pin at the far corner of a large building is still on that building."""
        corner = self._pin_at(41.733650, -73.930850)
        self.assertTrue(pin_restructure.marker_covers_building(_BUILDINGS[0], corner))

    def test_a_pin_just_outside_the_footprint_does_not_count(self) -> None:
        outside = self._pin_at(41.734200, -73.930400)
        self.assertFalse(pin_restructure.marker_covers_building(_BUILDINGS[0], outside))

    def test_without_a_footprint_the_centroid_radius_decides(self) -> None:
        near = self._pin_at(41.733010, -73.930000)
        far = self._pin_at(41.734000, -73.930000)
        self.assertTrue(pin_restructure.marker_covers_building(_BUILDINGS[1], near))
        self.assertFalse(pin_restructure.marker_covers_building(_BUILDINGS[1], far))

    def test_containment_beats_proximity_when_they_disagree(self) -> None:
        """The regression this exists for: a pin the old radius check would have missed."""
        corner = self._pin_at(41.733650, -73.930850)
        self.assertGreater(
            pin_restructure.site_scope.meters_between(41.733650, -73.930850, 41.73320, -73.93040),
            pin_restructure.site_scope.BUILDING_MATCH_METERS,
        )
        self.assertTrue(pin_restructure.marker_covers_building(_BUILDINGS[0], corner))

    def test_one_marker_is_consumed_by_one_building(self) -> None:
        pin = self._pin_at(41.733200, -73.930400)
        missing = pin_restructure.unmatched_buildings(_BUILDINGS, [pin])
        self.assertEqual([b["building_number"] for b in missing], ["9", "22"])

    def test_buildings_without_coordinates_are_skipped_entirely(self) -> None:
        self.assertEqual(pin_restructure.unmatched_buildings([{"name": "Unlocated"}], []), [])


class NestableRootPinTests(TestCase):
    """Finding the owner's existing top-level pins that stand inside this property."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, slug="campus")
        Boundary.objects.create(location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_parcel_polygon())

    def _root_pin_at(self, latitude: float, longitude: float, **kwargs) -> Pin:
        return baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=latitude, longitude=longitude, google_place=None), **kwargs)

    def test_a_top_level_pin_inside_the_boundary_is_nestable(self) -> None:
        inside = self._root_pin_at(41.7330, -73.9300, name="Old Powerhouse")
        self.assertEqual(pin_restructure.nestable_root_pins(self.pin), [inside])

    def test_a_pin_outside_the_boundary_is_not(self) -> None:
        self._root_pin_at(41.9000, -73.8000, name="Somewhere else")
        self.assertEqual(pin_restructure.nestable_root_pins(self.pin), [])

    def test_the_pin_itself_is_never_nestable_under_itself(self) -> None:
        self.assertNotIn(self.pin, pin_restructure.nestable_root_pins(self.pin))

    def test_existing_child_pins_are_not_offered_again(self) -> None:
        baker.make(Pin, profile=self.profile, parent_pin=self.pin, location=baker.make(Location, latitude=41.7331, longitude=-73.9302, google_place=None))
        self.assertEqual(pin_restructure.nestable_root_pins(self.pin), [])

    def test_another_users_pin_is_never_nestable(self) -> None:
        other_profile = baker.make(User).profile
        baker.make(Pin, profile=other_profile, location=baker.make(Location, latitude=41.7333, longitude=-73.9303, google_place=None))
        self.assertEqual(pin_restructure.nestable_root_pins(self.pin), [])

    def test_a_circle_fallback_boundary_never_drives_nesting(self) -> None:
        """Otherwise every pin within 50 m of a house would look like it belongs to it."""
        bare = baker.make(Pin, profile=self.profile, location=_make_location(), slug="no-boundary")
        self._root_pin_at(float(bare.location.latitude) + 0.0001, float(bare.location.longitude), name="Neighbour")
        self.assertEqual(pin_restructure.nestable_root_pins(bare), [])

    def test_an_ancestor_is_never_nested_under_its_own_descendant(self) -> None:
        parent = self._root_pin_at(41.7335, -73.9305, name="Ancestor")
        Pin.objects.filter(pk=self.pin.pk).update(parent_pin=parent)
        self.assertNotIn(parent, pin_restructure.nestable_root_pins(Pin.objects.get(pk=self.pin.pk)))


class RestructureOfferGatingTests(TestCase):
    """When the suggestion appears at all."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        self.url = reverse("pin.restructure.offer", kwargs={"pin_slug": self.pin.slug})

    def test_a_multi_building_property_is_offered_on_first_view(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 building")
        self.assertContains(response, "Tool Shed")

    def test_offered_even_when_only_one_building_is_unpinned(self) -> None:
        """Any building this would create and the user doesn't have is worth offering."""
        for building in _BUILDINGS[1:]:
            baker.make(
                Pin,
                profile=self.user.profile,
                parent_pin=self.pin,
                pin_type=PinType.BUILDING,
                location=baker.make(Location, latitude=building["latitude"], longitude=building["longitude"], google_place=None),
            )
        self.assertContains(self.client.get(self.url), "1 building")

    def test_a_fully_pinned_property_is_not_offered(self) -> None:
        for building in _BUILDINGS:
            baker.make(
                Pin,
                profile=self.user.profile,
                parent_pin=self.pin,
                pin_type=PinType.BUILDING,
                location=baker.make(Location, latitude=building["latitude"], longitude=building["longitude"], google_place=None),
            )
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_a_single_building_place_is_never_offered(self) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS[:1], "provider": "redata"})
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_a_dismissed_pin_is_never_offered_again(self) -> None:
        Pin.objects.filter(pk=self.pin.pk).update(restructure_offer_dismissed=True)
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_the_account_setting_silences_it_everywhere(self) -> None:
        profile = self.user.profile
        profile.suggest_pin_restructure = False
        profile.save(update_fields=["suggest_pin_restructure"])
        self.assertEqual(self.client.get(self.url).status_code, 204)

    def test_a_child_pin_is_never_offered(self) -> None:
        child = baker.make(Pin, profile=self.user.profile, parent_pin=self.pin, location=_make_location(), slug="a-building")
        self.assertEqual(self.client.get(reverse("pin.restructure.offer", kwargs={"pin_slug": child.slug})).status_code, 204)

    def test_an_uncached_parcel_polls_instead_of_blocking(self) -> None:
        pin = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="unknown-parcel")
        with patch("urbanlens.dashboard.services.external_data.schedule_panel_fetch", return_value=True):
            response = self.client.get(reverse("pin.restructure.offer", kwargs={"pin_slug": pin.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "attempt=1")

    def test_a_spent_poll_budget_still_offers_whatever_is_known(self) -> None:
        """Nesting doesn't depend on the building lookup, so a slow REData mustn't hide it."""
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS

        pin = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="slow-parcel")
        Boundary.objects.create(location=pin.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_parcel_polygon())
        baker.make(Pin, profile=self.user.profile, location=baker.make(Location, latitude=41.7330, longitude=-73.9300, google_place=None), name="Inside")

        response = self.client.get(reverse("pin.restructure.offer", kwargs={"pin_slug": pin.slug}), {"attempt": str(MAX_POLL_ATTEMPTS)})
        self.assertContains(response, "Inside")

    def test_nothing_to_suggest_yields_204(self) -> None:
        pin = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="ordinary-house")
        LocationCache.set(pin.location, PARCEL_BUILDINGS_CACHE_SOURCE, {})
        self.assertEqual(self.client.get(reverse("pin.restructure.offer", kwargs={"pin_slug": pin.slug})).status_code, 204)

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        self.assertEqual(self.client.get(reverse("pin.restructure.offer", kwargs={"pin_slug": other.slug})).status_code, 404)


class RestructureOfferContentTests(TestCase):
    """One dialog covers both halves - never two prompts."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        Boundary.objects.create(location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_parcel_polygon())
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        baker.make(Pin, profile=self.user.profile, location=baker.make(Location, latitude=41.7338, longitude=-73.9306, google_place=None), name="Gatehouse")
        self.url = reverse("pin.restructure.offer", kwargs={"pin_slug": self.pin.slug})

    def test_both_halves_appear_in_a_single_card(self) -> None:
        response = self.client.get(self.url)
        body = response.content.decode()
        self.assertEqual(body.count('id="pin-restructure-offer"'), 1)
        self.assertIn("3 unpinned building", body)
        self.assertIn("Gatehouse", body)

    def test_all_three_choices_are_offered(self) -> None:
        response = self.client.get(self.url)
        self.assertContains(response, reverse("pin.restructure.apply", kwargs={"pin_slug": self.pin.slug}))
        self.assertContains(response, reverse("pin.restructure.dismiss", kwargs={"pin_slug": self.pin.slug}))
        self.assertContains(response, "scope=all")


class RestructureDismissTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="campus")
        self.url = reverse("pin.restructure.dismiss", kwargs={"pin_slug": self.pin.slug})

    def test_no_dismisses_this_pin_only(self) -> None:
        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.pin.refresh_from_db()
        self.user.profile.refresh_from_db()
        self.assertTrue(self.pin.restructure_offer_dismissed)
        self.assertTrue(self.user.profile.suggest_pin_restructure, "one 'no' must not silence every other pin")

    def test_dont_show_again_turns_the_account_setting_off(self) -> None:
        response = self.client.post(f"{self.url}?scope=all")
        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.pin.refresh_from_db()
        self.assertFalse(self.user.profile.suggest_pin_restructure)
        self.assertTrue(self.pin.restructure_offer_dismissed, "turning the setting back on must not revive this pin's prompt")
        self.assertIn("Settings", response["HX-Trigger"])


class RestructureApplyTests(TestCase):
    """Accepting does both halves in one go."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        Boundary.objects.create(location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_parcel_polygon())
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        self.stray = baker.make(
            Pin,
            profile=self.user.profile,
            location=baker.make(Location, latitude=41.7338, longitude=-73.9306, google_place=None),
            name="Gatehouse",
        )
        self.url = reverse("pin.restructure.apply", kwargs={"pin_slug": self.pin.slug})

    def test_creates_building_pins_and_nests_the_stray(self) -> None:
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.stray.refresh_from_db()
        self.assertEqual(self.stray.parent_pin_id, self.pin.pk)
        self.assertEqual(self.pin.detail_pins.filter(pin_type=PinType.BUILDING).count(), 3)

    def test_a_nested_pin_keeps_everything_about_itself(self) -> None:
        """Nesting re-parents; it never merges or renames."""
        self.client.post(self.url)
        self.stray.refresh_from_db()
        self.assertEqual(self.stray.name, "Gatehouse")
        self.assertEqual(self.stray.location.latitude, baker.prepare(Location, latitude=self.stray.location.latitude).latitude)

    def test_the_property_becomes_parcel_scope(self) -> None:
        self.client.post(self.url)
        self.assertTrue(is_site_scope(Pin.objects.get(pk=self.pin.pk)))

    def test_the_toast_reports_both_halves(self) -> None:
        response = self.client.post(self.url)
        trigger = response["HX-Trigger"]
        self.assertIn("building pin", trigger)
        self.assertIn("Nested", trigger)

    def test_running_twice_changes_nothing_the_second_time(self) -> None:
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertEqual(self.pin.detail_pins.filter(pin_type=PinType.BUILDING).count(), 3)
        self.assertIn("already organized", response["HX-Trigger"])

    def test_a_building_already_pinned_by_hand_is_not_duplicated(self) -> None:
        baker.make(
            Pin,
            profile=self.user.profile,
            parent_pin=self.pin,
            pin_type=PinType.BUILDING,
            name="My Tool Shed",
            # Inside the Tool Shed footprint but well outside the centroid radius.
            location=baker.make(Location, latitude="41.733650", longitude="-73.930850", google_place=None),
        )
        self.client.post(self.url)
        self.assertEqual(self.pin.detail_pins.filter(name="Tool Shed").count(), 0)
        self.assertTrue(self.pin.detail_pins.filter(name="My Tool Shed").exists())

    def test_a_nameless_building_is_named_by_its_number(self) -> None:
        self.client.post(self.url)
        self.assertTrue(self.pin.detail_pins.filter(name="Building 22").exists())

    def test_children_get_their_own_distinct_coordinates(self) -> None:
        self.client.post(self.url)
        created = self.pin.detail_pins.filter(pin_type=PinType.BUILDING).select_related("location")
        self.assertEqual(len({(str(c.location.latitude), str(c.location.longitude)) for c in created}), 3)

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        self.assertEqual(self.client.post(reverse("pin.restructure.apply", kwargs={"pin_slug": other.slug})).status_code, 404)


class RestructureWikiMirrorTests(TestCase):
    """Child wikis are contributed only when a community wiki already exists."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        self.url = reverse("pin.restructure.apply", kwargs={"pin_slug": self.pin.slug})

    def test_no_wiki_means_no_wiki_is_created(self) -> None:
        self.client.post(self.url)
        self.assertFalse(Wiki.objects.filter(location=self.location).exists())

    def test_an_existing_wiki_gets_matching_child_wikis(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        self.assertEqual(wiki.child_wikis.filter(pin_type=PinType.BUILDING).count(), 3)

    def test_the_import_is_one_wiki_edit_not_one_per_building(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        edits = WikiEdit.objects.filter(wiki=wiki)
        self.assertEqual(edits.count(), 1)
        self.assertIn("child_wikis_imported", edits.first().changes)

    def test_the_wiki_becomes_parcel_scope_too(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.client.post(self.url)
        self.assertTrue(is_site_scope(Wiki.objects.get(pk=wiki.pk)))


class BuildingImportPanelActionTests(TestCase):
    """The panel's own buildings-only action, unaffected by the suggestion's dismissal."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        Boundary.objects.create(location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_parcel_polygon())
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _BUILDINGS, "provider": "redata"})
        self.url = reverse("pin.buildings.import", kwargs={"pin_slug": self.pin.slug})

    def test_creates_the_building_pins(self) -> None:
        self.client.post(self.url)
        self.assertEqual(self.pin.detail_pins.filter(pin_type=PinType.BUILDING).count(), 3)

    def test_it_never_nests_existing_top_level_pins(self) -> None:
        """The panel button is about buildings; re-parenting is the dialog's job."""
        stray = baker.make(Pin, profile=self.user.profile, location=baker.make(Location, latitude=41.7338, longitude=-73.9306, google_place=None), name="Gatehouse")
        self.client.post(self.url)
        stray.refresh_from_db()
        self.assertIsNone(stray.parent_pin_id)

    def test_still_works_after_the_suggestion_was_dismissed(self) -> None:
        Pin.objects.filter(pk=self.pin.pk).update(restructure_offer_dismissed=True)
        self.client.post(self.url)
        self.assertEqual(self.pin.detail_pins.filter(pin_type=PinType.BUILDING).count(), 3)

    def test_nothing_to_import_reports_so(self) -> None:
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertIn("already has a pin", response["HX-Trigger"])
