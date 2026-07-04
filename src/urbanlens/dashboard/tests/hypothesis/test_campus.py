"""Tests for Campus model properties and CampusQuerySet / CampusManager.

Campus defines the spatial boundary for a Location (wiki/default) or Pin.

- is_default / __str__: testable via unsaved instances.
- effective_polygon: tested with a saved Campus + Location (PostGIS).
- QuerySet / Manager: DB-backed with baker.
- CampusController: unit-tested with RequestFactory; boundary_as_multipolygon patched.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.campus.model import Campus
from urbanlens.dashboard.models.location.model import Location

# -- is_default ----------------------------------------------------------------


class CampusIsDefaultTests(TestCase):
    """is_default is True only when both profile_id and pin_id are None."""

    def _campus(self, profile_id, pin_id=None) -> Campus:
        c = Campus()
        c.profile_id = profile_id
        c.pin_id = pin_id
        return c

    def test_none_profile_none_pin_is_default(self) -> None:
        self.assertTrue(self._campus(None, None).is_default)

    def test_profile_set_is_not_default(self) -> None:
        self.assertFalse(self._campus(42, None).is_default)

    def test_pin_set_is_not_default(self) -> None:
        self.assertFalse(self._campus(None, 7).is_default)

    def test_both_set_is_not_default(self) -> None:
        self.assertFalse(self._campus(42, 7).is_default)


# -- __str__ -------------------------------------------------------------------

class CampusStrTests(TestCase):
    """__str__ encodes the campus type (location default or pin-scoped)."""

    def _campus(self, location_id, profile_id, pin_id=None) -> Campus:
        c = Campus()
        c.location_id = location_id
        c.profile_id = profile_id
        c.pin_id = pin_id
        return c

    def test_default_campus_str_contains_default(self) -> None:
        result = str(self._campus(location_id=5, profile_id=None, pin_id=None))
        self.assertIn("default", result)
        self.assertIn("5", result)

    def test_pin_campus_str_contains_pin_id(self) -> None:
        result = str(self._campus(location_id=5, profile_id=3, pin_id=9))
        self.assertIn("pin=9", result)
        self.assertIn("profile=3", result)


# -- effective_polygon ---------------------------------------------------------

class CampusEffectivePolygonTests(TestCase):
    """effective_polygon returns polygon → generated_polygon → circle."""

    def _make_campus(self, polygon=None, generated_polygon=None):
        location = baker.make(
            "dashboard.Location", latitude="40.000000", longitude="-74.000000",
        )
        campus = baker.make(
            "dashboard.Campus",
            location=location,
            profile=None,
            pin=None,
            polygon=polygon,
            generated_polygon=generated_polygon,
            default_radius_meters=50,
        )
        return Campus.objects.select_related("location").get(pk=campus.pk)

    def test_returns_polygon_when_set(self) -> None:
        coords = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0))
        mp = MultiPolygon(Polygon(coords, srid=4326), srid=4326)
        campus = self._make_campus(polygon=mp)
        self.assertIsNotNone(campus.effective_polygon)

    def test_falls_back_to_generated_polygon_when_polygon_none(self) -> None:
        coords = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0))
        mp = MultiPolygon(Polygon(coords, srid=4326), srid=4326)
        campus = self._make_campus(polygon=None, generated_polygon=mp)
        result = campus.effective_polygon
        self.assertIsNotNone(result)
        self.assertFalse(result.empty)

    def test_generates_circle_when_both_none(self) -> None:
        campus = self._make_campus(polygon=None, generated_polygon=None)
        result = campus.effective_polygon
        self.assertIsNotNone(result)
        self.assertFalse(result.empty)

    def test_polygon_takes_precedence_over_generated_polygon(self) -> None:
        coords_small = ((0.0, 0.0), (0.0, 0.1), (0.1, 0.1), (0.1, 0.0), (0.0, 0.0))
        coords_large = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0))
        mp_small = MultiPolygon(Polygon(coords_small, srid=4326), srid=4326)
        mp_large = MultiPolygon(Polygon(coords_large, srid=4326), srid=4326)
        campus = self._make_campus(polygon=mp_small, generated_polygon=mp_large)
        self.assertEqual(campus.effective_polygon.wkt, mp_small.wkt)


# -- CampusQuerySet.defaults ---------------------------------------------------

class CampusQuerySetDefaultsTests(TestCase):
    """defaults() returns only location-default campuses (profile=None, pin=None)."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.user = baker.make("auth.User")
        self.pin = baker.make(
            "dashboard.Pin", profile=self.user.profile, location=self.location,
            latitude=None, longitude=None,
        )
        self.default_campus = baker.make(
            "dashboard.Campus", location=self.location, profile=None, pin=None,
        )
        self.pin_campus = baker.make(
            "dashboard.Campus", location=self.location,
            profile=self.user.profile, pin=self.pin,
        )

    def test_defaults_includes_location_default(self) -> None:
        self.assertIn(self.default_campus, Campus.objects.defaults())

    def test_defaults_excludes_pin_campus(self) -> None:
        self.assertNotIn(self.pin_campus, Campus.objects.defaults())


# -- CampusQuerySet.for_profile ------------------------------------------------

class CampusQuerySetForProfileTests(TestCase):
    """for_profile() returns only pin-scoped campuses for a given profile."""

    def setUp(self):
        self.u1 = baker.make("auth.User")
        self.u2 = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude="41.0", longitude="-73.0")
        self.pin1 = baker.make(
            "dashboard.Pin", profile=self.u1.profile, location=self.location,
            latitude=None, longitude=None,
        )
        self.c1 = baker.make(
            "dashboard.Campus", location=self.location,
            profile=self.u1.profile, pin=self.pin1,
        )

    def test_returns_matching_pin_campus(self) -> None:
        self.assertIn(self.c1, Campus.objects.for_profile(self.u1.profile))

    def test_excludes_other_users_campus(self) -> None:
        loc2 = baker.make("dashboard.Location", latitude="42.0", longitude="-72.0")
        pin2 = baker.make(
            "dashboard.Pin", profile=self.u2.profile, location=loc2,
            latitude=None, longitude=None,
        )
        baker.make("dashboard.Campus", location=loc2, profile=self.u2.profile, pin=pin2)
        for campus in Campus.objects.for_profile(self.u1.profile):
            self.assertEqual(campus.profile_id, self.u1.profile.pk)


# -- CampusQuerySet.for_location -----------------------------------------------

class CampusQuerySetForLocationTests(TestCase):
    """for_location() returns all campuses referencing a given location."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.5", longitude="-74.5")
        self.other_location = baker.make("dashboard.Location", latitude="41.5", longitude="-73.5")
        self.campus = baker.make("dashboard.Campus", location=self.location, profile=None, pin=None)
        self.other_campus = baker.make(
            "dashboard.Campus", location=self.other_location, profile=None, pin=None,
        )

    def test_returns_campus_for_this_location(self) -> None:
        self.assertIn(self.campus, Campus.objects.for_location(self.location))

    def test_excludes_campus_for_other_location(self) -> None:
        self.assertNotIn(self.other_campus, Campus.objects.for_location(self.location))


# -- CampusQuerySet.for_pin ----------------------------------------------------

class CampusQuerySetForPinTests(TestCase):
    """for_pin() returns the campus keyed to a specific pin."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make(
            "dashboard.Pin", profile=self.user.profile, location=self.location,
            latitude=None, longitude=None,
        )
        self.campus = baker.make(
            "dashboard.Campus", location=self.location,
            profile=self.user.profile, pin=self.pin,
        )

    def test_returns_campus_for_pin(self) -> None:
        qs = Campus.objects.for_pin(self.pin)
        self.assertIn(self.campus, qs)

    def test_excludes_location_default(self) -> None:
        default = baker.make("dashboard.Campus", location=self.location, profile=None, pin=None)
        qs = Campus.objects.for_pin(self.pin)
        self.assertNotIn(default, qs)


# -- CampusManager.effective_for -----------------------------------------------

class CampusManagerEffectiveForTests(TestCase):
    """effective_for() returns the location-default campus (profile=None, pin=None)."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.user = baker.make("auth.User")
        self.admin_campus = baker.make(
            "dashboard.Campus", location=self.location, profile=None, pin=None,
        )

    def test_returns_location_default(self) -> None:
        result = Campus.objects.effective_for(self.location)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.admin_campus.pk)

    def test_returns_location_default_ignoring_profile_arg(self) -> None:
        # profile parameter is ignored; only location-default campuses are returned.
        result = Campus.objects.effective_for(self.location, profile=self.user.profile)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.admin_campus.pk)

    def test_returns_none_when_no_campus_exists(self) -> None:
        empty_loc: Location = baker.make(Location, latitude="50.0", longitude="-80.0")
        self.assertIsNone(Campus.objects.effective_for(empty_loc))


# -- CampusManager.effective_for_pin ------------------------------------------

class CampusManagerEffectiveForPinTests(TestCase):
    """effective_for_pin() resolves pin campus → location default → None."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.user = baker.make("auth.User")
        self.other = baker.make("auth.User")
        self.pin = baker.make(
            "dashboard.Pin", profile=self.user.profile, location=self.location,
            latitude=None, longitude=None,
        )
        self.other_pin = baker.make(
            "dashboard.Pin", profile=self.other.profile, location=self.location,
            latitude=None, longitude=None,
        )
        self.location_default = baker.make(
            "dashboard.Campus", location=self.location, profile=None, pin=None,
        )
        self.pin_campus = baker.make(
            "dashboard.Campus", location=self.location,
            profile=self.user.profile, pin=self.pin,
        )

    def test_returns_pin_campus_when_exists(self) -> None:
        result = Campus.objects.effective_for_pin(self.pin)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.pin_campus.pk)

    def test_falls_back_to_location_default_when_no_pin_campus(self) -> None:
        result = Campus.objects.effective_for_pin(self.other_pin)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.location_default.pk)

    def test_returns_none_when_no_campus_at_all(self) -> None:
        empty_loc = baker.make("dashboard.Location", latitude="50.0", longitude="-80.0")
        empty_pin = baker.make(
            "dashboard.Pin", profile=self.user.profile, location=empty_loc,
            latitude=None, longitude=None,
        )
        self.assertIsNone(Campus.objects.effective_for_pin(empty_pin))


# -- CampusQuerySet.with_location ----------------------------------------------

class CampusQuerySetWithLocationTests(TestCase):
    """with_location() select_relates location so effective_polygon avoids extra queries."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="42.0", longitude="-71.0")
        self.campus = baker.make("dashboard.Campus", location=self.location, profile=None, pin=None)

    def test_with_location_returns_campus_queryset(self) -> None:
        self.assertEqual(Campus.objects.filter(pk=self.campus.pk).with_location().count(), 1)

    def test_with_location_select_relates_location(self) -> None:
        campus = Campus.objects.filter(pk=self.campus.pk).with_location().get()
        self.assertIsNotNone(campus.location)
        self.assertEqual(campus.location.pk, self.location.pk)

    def test_with_location_allows_effective_polygon_without_extra_query(self) -> None:
        campus = Campus.objects.filter(pk=self.campus.pk).with_location().get()
        self.assertIsNotNone(campus.effective_polygon)


# -- CampusController pin detail -----------------------------------------------

_DEFAULT_BOUNDARY = Polygon(
    ((-74.003, 39.997), (-74.003, 40.003), (-73.997, 40.003), (-73.997, 39.997), (-74.003, 39.997)),
    srid=4326,
)
_DEFAULT_BOUNDARY_MP = MultiPolygon(_DEFAULT_BOUNDARY, srid=4326)
_PIN_BOUNDARY = MultiPolygon(
    Polygon(
        ((-74.002, 39.998), (-74.002, 40.002), (-73.998, 40.002), (-73.998, 39.998), (-74.002, 39.998)),
        srid=4326,
    ),
    srid=4326,
)
_LOCATION_BOUNDARY = MultiPolygon(
    Polygon(
        ((-74.001, 39.999), (-74.001, 40.001), (-73.999, 40.001), (-73.999, 39.999), (-74.001, 39.999)),
        srid=4326,
    ),
    srid=4326,
)


class CampusControllerBoundaryTests(TestCase):
    """CampusController uses pin-scoped Campus rows, not (location, profile) rows."""

    def setUp(self):
        import json

        from django.test import RequestFactory

        self.factory = RequestFactory()
        self.user = baker.make("auth.User")
        self.location = baker.make(
            "dashboard.Location", latitude="40.000000", longitude="-74.000000",
        )
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.user.profile,
            location=self.location,
            slug="test-pin-campus",
            latitude=None,
            longitude=None,
        )
        # Location-default campus (wiki).
        self.location_campus = baker.make(
            "dashboard.Campus",
            location=self.location,
            profile=None,
            pin=None,
            polygon=_LOCATION_BOUNDARY,
            generated_polygon=_LOCATION_BOUNDARY,
        )
        # Pin-scoped campus (user boundary).
        self.pin_campus = baker.make(
            "dashboard.Campus",
            location=self.location,
            profile=self.user.profile,
            pin=self.pin,
            polygon=_PIN_BOUNDARY,
            generated_polygon=_PIN_BOUNDARY,
        )
        self._json = json

    def _request(self, method="get", data=None):
        import json

        if method == "post":
            req = self.factory.post(
                "/campus/",
                data=json.dumps(data or {}),
                content_type="application/json",
            )
            req.data = data or {}
        else:
            req = self.factory.get("/campus/")
        req.user = self.user
        return req

    def test_get_campus_returns_pin_boundary_not_location_boundary(self) -> None:
        from urbanlens.dashboard.controllers.campus import CampusController

        response = CampusController().get_campus(self._request(), self.pin.slug)
        payload = self._json.loads(response.content)

        self.assertEqual(payload["polygon"], self._json.loads(_PIN_BOUNDARY.geojson))
        self.assertNotEqual(payload["polygon"], self._json.loads(_LOCATION_BOUNDARY.geojson))

    def test_get_campus_creates_pin_campus_and_caches_generated_polygon_when_missing(self) -> None:
        from urbanlens.dashboard.controllers.campus import CampusController

        self.pin_campus.delete()

        with patch(
            "urbanlens.dashboard.controllers.campus.boundary_as_multipolygon",
            return_value=_DEFAULT_BOUNDARY_MP,
        ):
            response = CampusController().get_campus(self._request(), self.pin.slug)

        payload = self._json.loads(response.content)
        self.assertEqual(payload["polygon"], self._json.loads(_DEFAULT_BOUNDARY_MP.geojson))
        # Campus is keyed by pin, not (location, profile).
        campus = Campus.objects.get(pin=self.pin)
        self.assertEqual(
            self._json.loads(campus.generated_polygon.geojson),
            self._json.loads(_DEFAULT_BOUNDARY_MP.geojson),
        )
        # polygon (user-drawn) is None; generated_polygon holds the cached boundary.
        self.assertIsNone(campus.polygon)

    def test_save_campus_stores_user_drawing_in_polygon(self) -> None:
        from urbanlens.dashboard.controllers.campus import CampusController

        new_polygon_geojson = self._json.loads(_DEFAULT_BOUNDARY_MP.geojson)
        response = CampusController().save_campus(
            self._request("post", {"polygon": new_polygon_geojson}), self.pin.slug,
        )
        self.assertEqual(response.status_code, 200)
        self.pin_campus.refresh_from_db()
        self.assertIsNotNone(self.pin_campus.polygon)

    def test_clear_user_drawing_resets_polygon_to_none_keeps_generated(self) -> None:
        from urbanlens.dashboard.controllers.campus import CampusController

        # pin_campus already has generated_polygon set (from setUp).
        response = CampusController().save_campus(
            self._request("post", {"polygon": None}), self.pin.slug,
        )
        self.assertEqual(response.status_code, 200)
        self.pin_campus.refresh_from_db()
        self.assertIsNone(self.pin_campus.polygon)
        # generated_polygon is preserved so no API call is needed next time.
        self.assertIsNotNone(self.pin_campus.generated_polygon)

    def test_clear_without_generated_polygon_triggers_api_and_caches_result(self) -> None:
        from urbanlens.dashboard.controllers.campus import CampusController

        self.pin_campus.generated_polygon = None
        self.pin_campus.polygon = None
        self.pin_campus.save(update_fields=["polygon", "generated_polygon"])

        with patch(
            "urbanlens.dashboard.controllers.campus.boundary_as_multipolygon",
            return_value=_DEFAULT_BOUNDARY_MP,
        ):
            response = CampusController().save_campus(
                self._request("post", {"polygon": None}), self.pin.slug,
            )

        self.assertEqual(response.status_code, 200)
        self.pin_campus.refresh_from_db()
        self.assertIsNone(self.pin_campus.polygon)
        self.assertEqual(
            self._json.loads(self.pin_campus.generated_polygon.geojson),
            self._json.loads(_DEFAULT_BOUNDARY_MP.geojson),
        )

    def test_pin_campus_survives_location_reassignment(self) -> None:
        """Campus found by pin FK even after pin.location changes."""
        from urbanlens.dashboard.controllers.campus import CampusController

        new_location = baker.make(
            "dashboard.Location", latitude="41.000000", longitude="-75.000000",
        )
        self.pin.location = new_location
        self.pin.save(update_fields=["location"])

        response = CampusController().get_campus(self._request(), self.pin.slug)
        self.assertEqual(response.status_code, 200)

        # The campus should now reference the new location.
        self.pin_campus.refresh_from_db()
        self.assertEqual(self.pin_campus.location_id, new_location.pk)
