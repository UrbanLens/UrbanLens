"""Tests for Campus model properties and CampusQuerySet / CampusManager.

Campus defines the spatial boundary for a Location.
- is_default / __str__: testable via unsaved instances.
- effective_polygon: tested with a saved Campus + Location (PostGIS).
- QuerySet / Manager: DB-backed with baker.
"""
from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon
from urbanlens.core.tests.testcase import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.location.model import Location


# ── is_default ────────────────────────────────────────────────────────────────

class CampusIsDefaultTests(TestCase):
	"""is_default is True when profile_id is None (admin-owned campus)."""

	def _campus(self, profile_id) -> Campus:
		c = Campus()
		c.profile_id = profile_id
		return c

	def test_none_profile_id_is_default(self) -> None:
		self.assertTrue(self._campus(None).is_default)

	def test_non_none_profile_id_is_not_default(self) -> None:
		self.assertFalse(self._campus(42).is_default)

	def test_zero_profile_id_is_not_default(self) -> None:
		# 0 is truthy enough to indicate a set FK (unusual but safe to test)
		self.assertFalse(self._campus(0).is_default)


# ── __str__ ───────────────────────────────────────────────────────────────────

class CampusStrTests(TestCase):
	"""__str__ encodes location_id and either 'default' or 'profile <id>'."""

	def _campus(self, location_id, profile_id) -> Campus:
		c = Campus()
		c.location_id = location_id
		c.profile_id = profile_id
		return c

	def test_default_campus_str_contains_default(self) -> None:
		result = str(self._campus(location_id=5, profile_id=None))
		self.assertIn("default", result)
		self.assertIn("5", result)

	def test_user_campus_str_contains_profile_id(self) -> None:
		result = str(self._campus(location_id=5, profile_id=3))
		self.assertIn("profile 3", result)
		self.assertIn("5", result)


# ── effective_polygon ─────────────────────────────────────────────────────────

class CampusEffectivePolygonTests(TestCase):
	"""effective_polygon returns the stored polygon or generates a circle fallback."""

	def _make_campus(self, polygon=None):
		location = baker.make(
			"dashboard.Location", latitude="40.000000", longitude="-74.000000"
		)
		campus = baker.make(
			"dashboard.Campus",
			location=location,
			profile=None,
			polygon=polygon,
			default_radius_meters=50,
		)
		return Campus.objects.select_related("location").get(pk=campus.pk)

	def test_returns_stored_polygon_when_set(self) -> None:
		coords = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0))
		poly = Polygon(coords, srid=4326)
		mp = MultiPolygon(poly, srid=4326)
		campus = self._make_campus(polygon=mp)
		result = campus.effective_polygon
		self.assertIsNotNone(result)

	def test_generates_circle_when_polygon_is_none(self) -> None:
		campus = self._make_campus(polygon=None)
		result = campus.effective_polygon
		self.assertIsNotNone(result)

	def test_generated_circle_is_not_empty(self) -> None:
		campus = self._make_campus(polygon=None)
		result = campus.effective_polygon
		self.assertFalse(result.empty)


# ── CampusQuerySet ────────────────────────────────────────────────────────────

class CampusQuerySetDefaultsTests(TestCase):
	"""defaults() returns only admin-defined campuses (profile=None)."""

	def setUp(self):
		self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
		self.user = baker.make("auth.User")
		self.default_campus = baker.make(
			"dashboard.Campus", location=self.location, profile=None
		)
		self.user_campus = baker.make(
			"dashboard.Campus", location=self.location, profile=self.user.profile
		)

	def test_defaults_includes_admin_campus(self) -> None:
		qs = Campus.objects.defaults()
		self.assertIn(self.default_campus, qs)

	def test_defaults_excludes_user_campus(self) -> None:
		qs = Campus.objects.defaults()
		self.assertNotIn(self.user_campus, qs)


class CampusQuerySetForProfileTests(TestCase):
	"""for_profile() returns only the campuses owned by a given profile."""

	def setUp(self):
		self.u1 = baker.make("auth.User")
		self.u2 = baker.make("auth.User")
		self.location = baker.make("dashboard.Location", latitude="41.0", longitude="-73.0")
		self.c1 = baker.make("dashboard.Campus", location=self.location, profile=self.u1.profile)

	def test_returns_matching_campus(self) -> None:
		qs = Campus.objects.for_profile(self.u1.profile)
		self.assertIn(self.c1, qs)

	def test_excludes_other_users_campus(self) -> None:
		baker.make("dashboard.Campus", location=baker.make("dashboard.Location", latitude="42.0", longitude="-72.0"), profile=self.u2.profile)
		qs = Campus.objects.for_profile(self.u1.profile)
		for campus in qs:
			self.assertEqual(campus.profile_id, self.u1.profile.pk)


class CampusQuerySetForLocationTests(TestCase):
	"""for_location() returns all campuses (default and user) for a given location."""

	def setUp(self):
		self.location = baker.make("dashboard.Location", latitude="40.5", longitude="-74.5")
		self.other_location = baker.make("dashboard.Location", latitude="41.5", longitude="-73.5")
		self.user = baker.make("auth.User")
		self.campus = baker.make("dashboard.Campus", location=self.location, profile=None)
		self.other_campus = baker.make("dashboard.Campus", location=self.other_location, profile=None)

	def test_returns_campus_for_this_location(self) -> None:
		qs = Campus.objects.for_location(self.location)
		self.assertIn(self.campus, qs)

	def test_excludes_campus_for_other_location(self) -> None:
		qs = Campus.objects.for_location(self.location)
		self.assertNotIn(self.other_campus, qs)


# ── CampusManager.effective_for ───────────────────────────────────────────────

class CampusManagerEffectiveForTests(TestCase):
	"""effective_for() resolves user override → admin default → None."""

	def setUp(self):
		self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
		self.user = baker.make("auth.User")
		self.other = baker.make("auth.User")
		self.admin_campus = baker.make(
			"dashboard.Campus", location=self.location, profile=None
		)
		self.user_campus = baker.make(
			"dashboard.Campus", location=self.location, profile=self.user.profile
		)

	def test_returns_user_override_when_profile_given(self) -> None:
		result = Campus.objects.effective_for(self.location, profile=self.user.profile)
		self.assertIsNotNone(result)
		self.assertEqual(result.pk, self.user_campus.pk)

	def test_falls_back_to_admin_default_when_no_user_override(self) -> None:
		result = Campus.objects.effective_for(self.location, profile=self.other.profile)
		self.assertIsNotNone(result)
		self.assertEqual(result.pk, self.admin_campus.pk)

	def test_returns_admin_default_when_no_profile_given(self) -> None:
		result = Campus.objects.effective_for(self.location)
		self.assertIsNotNone(result)
		self.assertEqual(result.pk, self.admin_campus.pk)

	def test_returns_none_when_no_campus_exists(self) -> None:
		empty_loc: Location = baker.make(Location, latitude="50.0", longitude="-80.0")
		result = Campus.objects.effective_for(empty_loc)
		self.assertIsNone(result)


# ── CampusQuerySet.with_location ──────────────────────────────────────────────

class CampusQuerySetWithLocationTests(TestCase):
	"""with_location() select_relates location so effective_polygon avoids extra queries."""

	def setUp(self):
		self.location = baker.make("dashboard.Location", latitude="42.0", longitude="-71.0")
		self.campus = baker.make("dashboard.Campus", location=self.location, profile=None)

	def test_with_location_returns_campus_queryset(self) -> None:
		qs = Campus.objects.filter(pk=self.campus.pk).with_location()
		self.assertEqual(qs.count(), 1)

	def test_with_location_select_relates_location(self) -> None:
		# After with_location the location is cached - accessing it should not hit DB.
		campus = Campus.objects.filter(pk=self.campus.pk).with_location().get()
		# location should be in _state.fields_cache (select_related sets this)
		self.assertIsNotNone(campus.location)
		self.assertEqual(campus.location.pk, self.location.pk)

	def test_with_location_allows_effective_polygon_without_extra_query(self) -> None:
		# effective_polygon accesses self.location; this must not raise when
		# location is prefetched via with_location().
		campus = Campus.objects.filter(pk=self.campus.pk).with_location().get()
		result = campus.effective_polygon
		self.assertIsNotNone(result)


# ── CampusManager.effective_for with profile=None branch ─────────────────────

class CampusManagerEffectiveForProfileNoneTests(TestCase):
	"""effective_for() returns admin default when profile=None (anonymous access)."""

	def setUp(self):
		self.location = baker.make("dashboard.Location", latitude="35.0", longitude="-90.0")
		self.admin_campus = baker.make("dashboard.Campus", location=self.location, profile=None)

	def test_returns_admin_default_for_anonymous(self) -> None:
		result = Campus.objects.effective_for(self.location, profile=None)
		self.assertIsNotNone(result)
		self.assertEqual(result.pk, self.admin_campus.pk)

	def test_returns_none_for_anonymous_when_no_campus(self) -> None:
		empty_loc = baker.make("dashboard.Location", latitude="36.0", longitude="-91.0")
		result = Campus.objects.effective_for(empty_loc, profile=None)
		self.assertIsNone(result)
