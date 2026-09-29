"""Tests for PinQuerySet filter methods and PinManager.get_nearby_or_create."""

from __future__ import annotations

from datetime import date
import math
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# -- root_pins / detail_pins / location_detail_pins ---------------------------


class PinQuerySetStructureTests(TestCase):
    """root_pins / detail_pins partition pins by the parent_pin FK."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        # Root: no parent_pin
        self.root = baker.make(
            Pin,
            profile=self.profile,
            location=self.location,
            parent_pin=None,
        )
        # Detail: parent_pin set
        self.detail = baker.make(
            Pin,
            profile=self.profile,
            location=self.location,
            parent_pin=self.root,
        )

    def _qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_root_pins_includes_root(self) -> None:
        self.assertIn(self.root, self._qs().root_pins())

    def test_root_pins_excludes_detail_pin(self) -> None:
        self.assertNotIn(self.detail, self._qs().root_pins())

    def test_detail_pins_includes_detail(self) -> None:
        self.assertIn(self.detail, self._qs().detail_pins())

    def test_detail_pins_excludes_root(self) -> None:
        self.assertNotIn(self.root, self._qs().detail_pins())


class PinQuerySetWithinBoundsTests(TestCase):
    """within_bounds() scopes pins to a lat/lng bounding box (used by the map's pin-list sidebar)."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.inside = baker.make(
            Pin, profile=self.profile, location=baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        )
        self.outside = baker.make(
            Pin, profile=self.profile, location=baker.make("dashboard.Location", latitude="45.0", longitude="-80.0")
        )

    def _qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_includes_pin_inside_the_box(self) -> None:
        result = self._qs().within_bounds(south=39.0, west=-75.0, north=41.0, east=-73.0)
        self.assertIn(self.inside, result)

    def test_excludes_pin_outside_the_box(self) -> None:
        result = self._qs().within_bounds(south=39.0, west=-75.0, north=41.0, east=-73.0)
        self.assertNotIn(self.outside, result)

    def test_point_just_inside_the_edge_is_included(self) -> None:
        # PostGIS __within uses strict OGC "within" semantics - a point exactly ON the
        # boundary is not guaranteed to count (it's boundary, not interior), so this
        # checks a point just inside the edge rather than asserting exact-edge inclusion.
        near_edge_location = baker.make("dashboard.Location", latitude="40.999", longitude="-73.001")
        near_edge_pin = baker.make(Pin, profile=self.profile, location=near_edge_location)
        result = self._qs().within_bounds(south=39.0, west=-75.0, north=41.0, east=-73.0)
        self.assertIn(near_edge_pin, result)

    def test_empty_box_excludes_everything(self) -> None:
        result = self._qs().within_bounds(south=10.0, west=10.0, north=11.0, east=11.0)
        self.assertNotIn(self.inside, result)
        self.assertNotIn(self.outside, result)


class WikiQuerySetStructureTests(TestCase):
    """child_wikis() selects wikis nested under a parent via the parent_wiki FK."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.child_location = baker.make("dashboard.Location", latitude="40.001", longitude="-74.001")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, parent_wiki=None)
        self.child = baker.make(
            "dashboard.Wiki",
            location=self.child_location,
            parent_wiki=self.wiki,
        )

    def _qs(self):
        from urbanlens.dashboard.models.wiki.model import Wiki

        return Wiki.objects.filter(pk__in=[self.wiki.pk, self.child.pk])

    def test_child_wikis_includes_child(self) -> None:
        self.assertIn(self.child, self._qs().child_wikis())

    def test_child_wikis_excludes_root(self) -> None:
        self.assertNotIn(self.wiki, self._qs().child_wikis())


# -- never_visited -------------------------------------------------------------


class PinQuerySetNeverVisitedTests(TestCase):
    """never_visited() returns only pins with last_visited == None."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.visited = baker.make(Pin, profile=self.profile, last_visited=date.today())
        self.unvisited = baker.make(Pin, profile=self.profile, last_visited=None)

    def _qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_includes_unvisited_pin(self) -> None:
        self.assertIn(self.unvisited, self._qs().never_visited())

    def test_excludes_visited_pin(self) -> None:
        self.assertNotIn(self.visited, self._qs().never_visited())


# -- PinManager.get_nearby_or_create -------------------------------------------


class PinManagerGetNearbyOrCreateGuardTests(TestCase):
    """get_nearby_or_create() returns (None, False) for invalid inputs."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_none_latitude_returns_none_false(self) -> None:
        self.assertEqual(Pin.objects.get_nearby_or_create(None, -74.0, self.profile), (None, False))

    def test_none_longitude_returns_none_false(self) -> None:
        self.assertEqual(Pin.objects.get_nearby_or_create(40.0, None, self.profile), (None, False))

    def test_non_numeric_latitude_returns_none_false(self) -> None:
        self.assertEqual(Pin.objects.get_nearby_or_create("abc", -74.0, self.profile), (None, False))

    def test_nan_latitude_returns_none_false(self) -> None:
        self.assertEqual(Pin.objects.get_nearby_or_create(math.nan, -74.0, self.profile), (None, False))

    def test_inf_longitude_returns_none_false(self) -> None:
        self.assertEqual(Pin.objects.get_nearby_or_create(40.0, math.inf, self.profile), (None, False))


class PinManagerGetNearbyOrCreateProximityTests(TestCase):
    """get_nearby_or_create() finds nearby pins or creates new ones."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        loc = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        # Seed an existing pin at a known coordinate using the manager.
        self.existing, _ = Pin.objects.get_nearby_or_create(
            40.0,
            -74.0,
            self.profile,
            threshold_meters=100,
            defaults={"location": loc},
        )

    def test_same_point_returns_existing_pin(self) -> None:
        pin, created = Pin.objects.get_nearby_or_create(40.0, -74.0, self.profile)
        self.assertFalse(created)
        self.assertEqual(pin.pk, self.existing.pk)

    def test_distant_point_creates_new_pin(self) -> None:
        loc: Location = baker.make(Location, latitude="51.5", longitude="-0.1")
        pin, created = Pin.objects.get_nearby_or_create(
            51.5,
            -0.1,
            self.profile,
            defaults={"location": loc},
        )
        self.assertTrue(created)
        self.assertNotEqual(pin.pk, self.existing.pk)

    def test_created_pin_is_persisted(self) -> None:
        loc: Location = baker.make(Location, latitude="51.5", longitude="-0.1")
        pin, created = Pin.objects.get_nearby_or_create(
            51.5,
            -0.1,
            self.profile,
            defaults={"location": loc},
        )
        self.assertTrue(created)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())

    def test_pin_is_scoped_to_requesting_profile(self) -> None:
        # A pin for a different profile at the same location should not be returned.
        other_profile: Profile = baker.make(User).profile
        pin, created = Pin.objects.get_nearby_or_create(40.0, -74.0, other_profile)
        self.assertTrue(created)
        self.assertNotEqual(pin.pk, self.existing.pk)


class PinManagerGetNearbyOrCreateChildPinTests(TestCase):
    """get_nearby_or_create() must also dedupe against an existing *child* pin.

    Import previously only looked for a root pin (parent_pin__isnull=True) at the Location, so importing a
    placemark that matched an existing child/child pin's coordinates silently created a brand-new, disconnected
    root pin instead of merging into it."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.loc = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")

    def test_finds_existing_child_pin_when_no_root_pin_exists(self) -> None:
        parent = baker.make(Pin, profile=self.profile)
        child = baker.make(Pin, profile=self.profile, location=self.loc, parent_pin=parent)
        pin, created = Pin.objects.get_nearby_or_create(40.0, -74.0, self.profile, threshold_meters=100)
        self.assertFalse(created)
        self.assertEqual(pin.pk, child.pk)

    def test_prefers_root_pin_over_child_pin_when_both_exist_at_same_location(self) -> None:
        root = baker.make(Pin, profile=self.profile, location=self.loc)
        other_parent = baker.make(Pin, profile=self.profile)
        baker.make(Pin, profile=self.profile, location=self.loc, parent_pin=other_parent)
        pin, created = Pin.objects.get_nearby_or_create(40.0, -74.0, self.profile, threshold_meters=100)
        self.assertFalse(created)
        self.assertEqual(pin.pk, root.pk)
