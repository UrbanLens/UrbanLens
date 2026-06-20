"""Tests for PinQuerySet filter methods and PinManager.get_nearby_or_create.

Covers structural pin-type filters (root_pins, detail_pins, location_detail_pins),
temporal visit filters (never_visited), rating filters (rated/rated_over/rated_under),
tag hierarchy traversal (by_tag), and the proximity-based manager method.

All tests require the database.
"""
from __future__ import annotations

import math
from datetime import date

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reviews.model import Review


# ── root_pins / detail_pins / location_detail_pins ───────────────────────────

class PinQuerySetStructureTests(TestCase):
	"""root_pins / detail_pins / location_detail_pins partition pins by parent FK."""

	def setUp(self):
		self.profile = baker.make("auth.User").profile
		self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
		# Root: no parent_pin, no parent_location
		self.root = baker.make(
			Pin, profile=self.profile, location=self.location,
			parent_pin=None, parent_location=None,
		)
		# Detail: parent_pin set
		self.detail = baker.make(
			Pin, profile=self.profile, location=self.location,
			parent_pin=self.root, parent_location=None,
		)
		# Location-detail: parent_location set, no parent_pin
		self.loc_detail = baker.make(
			Pin, profile=self.profile, location=self.location,
			parent_location=self.location, parent_pin=None,
		)

	def _qs(self):
		return Pin.objects.filter(profile=self.profile)

	def test_root_pins_includes_root(self) -> None:
		self.assertIn(self.root, self._qs().root_pins())

	def test_root_pins_excludes_detail_pin(self) -> None:
		self.assertNotIn(self.detail, self._qs().root_pins())

	def test_root_pins_excludes_location_detail_pin(self) -> None:
		self.assertNotIn(self.loc_detail, self._qs().root_pins())

	def test_detail_pins_includes_detail(self) -> None:
		self.assertIn(self.detail, self._qs().detail_pins())

	def test_detail_pins_excludes_root(self) -> None:
		self.assertNotIn(self.root, self._qs().detail_pins())

	def test_detail_pins_excludes_location_detail(self) -> None:
		self.assertNotIn(self.loc_detail, self._qs().detail_pins())

	def test_location_detail_pins_includes_loc_detail(self) -> None:
		self.assertIn(self.loc_detail, self._qs().location_detail_pins())

	def test_location_detail_pins_excludes_root(self) -> None:
		self.assertNotIn(self.root, self._qs().location_detail_pins())

	def test_location_detail_pins_excludes_detail_pin(self) -> None:
		self.assertNotIn(self.detail, self._qs().location_detail_pins())


# ── never_visited ─────────────────────────────────────────────────────────────

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


# ── rated / rated_over / rated_under ─────────────────────────────────────────

class PinQuerySetRatingTests(TestCase):
	"""rated() / rated_over() / rated_under() filter by linked review score."""

	def setUp(self):
		self.user = baker.make("auth.User")
		self.profile = self.user.profile
		self.pin_3 = baker.make(Pin, profile=self.profile)
		self.pin_5 = baker.make(Pin, profile=self.profile)
		baker.make(Review, user=self.user, pin=self.pin_3, rating=3)
		baker.make(Review, user=self.user, pin=self.pin_5, rating=5)

	def _qs(self):
		return Pin.objects.filter(profile=self.profile)

	def test_rated_finds_exact_match(self) -> None:
		qs = self._qs().rated(3)
		self.assertIn(self.pin_3, qs)
		self.assertNotIn(self.pin_5, qs)

	def test_rated_excludes_non_matching_score(self) -> None:
		self.assertFalse(self._qs().rated(4).filter(pk__in=[self.pin_3.pk, self.pin_5.pk]).exists())

	def test_rated_over_includes_equal_rating(self) -> None:
		self.assertIn(self.pin_3, self._qs().rated_over(3))

	def test_rated_over_includes_higher_rating(self) -> None:
		self.assertIn(self.pin_5, self._qs().rated_over(3))

	def test_rated_over_excludes_lower_rating(self) -> None:
		self.assertNotIn(self.pin_3, self._qs().rated_over(4))

	def test_rated_under_includes_equal_rating(self) -> None:
		self.assertIn(self.pin_3, self._qs().rated_under(3))

	def test_rated_under_excludes_higher_rating(self) -> None:
		self.assertNotIn(self.pin_5, self._qs().rated_under(3))


# ── by_tag ────────────────────────────────────────────────────────────────────

class PinQuerySetByTagTests(TestCase):
	"""by_tag() traverses the Badge parents M2M to include descendant tags."""

	def setUp(self):
		self.profile = baker.make("auth.User").profile
		self.parent_tag = baker.make("dashboard.Badge", kind="tag", profile=None)
		self.child_tag = baker.make("dashboard.Badge", kind="tag", profile=None)
		self.child_tag.parents.add(self.parent_tag)
		self.other_tag = baker.make("dashboard.Badge", kind="tag", profile=None)

		loc = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
		self.pin_parent = baker.make(Pin, profile=self.profile, location=loc)
		self.pin_parent.tags.add(self.parent_tag)

		self.pin_child = baker.make(Pin, profile=self.profile, location=loc)
		self.pin_child.tags.add(self.child_tag)

		self.pin_other = baker.make(Pin, profile=self.profile, location=loc)
		self.pin_other.tags.add(self.other_tag)

		self.pin_none = baker.make(Pin, profile=self.profile, location=loc)

	def _qs(self):
		return Pin.objects.filter(profile=self.profile)

	def test_by_parent_tag_includes_directly_tagged_pin(self) -> None:
		qs = self._qs().by_tag(self.parent_tag.id)
		self.assertIn(self.pin_parent, qs)

	def test_by_parent_tag_includes_pin_tagged_with_descendant(self) -> None:
		qs = self._qs().by_tag(self.parent_tag.id)
		self.assertIn(self.pin_child, qs)

	def test_by_parent_tag_excludes_unrelated_tag(self) -> None:
		qs = self._qs().by_tag(self.parent_tag.id)
		self.assertNotIn(self.pin_other, qs)

	def test_by_parent_tag_excludes_untagged_pin(self) -> None:
		qs = self._qs().by_tag(self.parent_tag.id)
		self.assertNotIn(self.pin_none, qs)

	def test_by_child_tag_excludes_pin_with_only_parent_tag(self) -> None:
		# Ancestry is one-way — child tag does not imply parent tag
		qs = self._qs().by_tag(self.child_tag.id)
		self.assertNotIn(self.pin_parent, qs)


# ── PinManager.get_nearby_or_create ───────────────────────────────────────────

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
			40.0, -74.0, self.profile,
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
			51.5, -0.1, self.profile, defaults={"location": loc}
		)
		self.assertTrue(created)
		self.assertNotEqual(pin.pk, self.existing.pk)

	def test_created_pin_is_persisted(self) -> None:
		loc: Location = baker.make(Location, latitude="51.5", longitude="-0.1")
		pin, created = Pin.objects.get_nearby_or_create(
			51.5, -0.1, self.profile, defaults={"location": loc}
		)
		self.assertTrue(created)
		self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())

	def test_pin_is_scoped_to_requesting_profile(self) -> None:
		# A pin for a different profile at the same location should not be returned.
		other_profile: Profile = baker.make(User).profile
		pin, created = Pin.objects.get_nearby_or_create(40.0, -74.0, other_profile)
		self.assertTrue(created)
		self.assertNotEqual(pin.pk, self.existing.pk)
