"""Tests for the Boundary model, its resolution chain, and the BoundaryController.

Boundary generalizes the old Campus model: typed (property/building) spatial
regions keyed by Location (shared defaults), Wiki (community drawings), or Pin
(personal drawings). Resolution rules under test:

- Pin property: pin row → parent pin (detail pins) → wiki row → location
  generated → circle fallback.
- Pin building: pin row → parent building only when the pin sits inside it →
  (root pins) wiki row → location generated → None (no circle).
- Point→location matching uses only ``generated_polygon`` on location-default
  rows (anti-abuse: user drawings never affect matching).
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


_BIG = _square(-74.0, 40.0, 0.003)      # property-sized
_MEDIUM = _square(-74.0, 40.0, 0.002)
_SMALL = _square(-74.0, 40.0, 0.001)    # building-sized


class BoundaryModelTests(TestCase):
    """Field semantics and effective_polygon fallbacks."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")

    def test_is_location_default(self) -> None:
        row = baker.make("dashboard.Boundary", location=self.location, wiki=None, pin=None, profile=None)
        self.assertTrue(row.is_location_default)

    def test_pin_row_is_not_location_default(self) -> None:
        pin = baker.make("dashboard.Pin", location=self.location)
        row = baker.make("dashboard.Boundary", location=self.location, pin=pin, profile=pin.profile)
        self.assertFalse(row.is_location_default)

    def test_drawn_polygon_wins_over_generated(self) -> None:
        row = baker.make("dashboard.Boundary", location=self.location, polygon=_SMALL, generated_polygon=_BIG)
        self.assertEqual(row.drawn_or_generated_polygon.wkt, _SMALL.wkt)
        self.assertEqual(row.effective_polygon.wkt, _SMALL.wkt)

    def test_generated_polygon_used_when_no_drawing(self) -> None:
        row = baker.make("dashboard.Boundary", location=self.location, polygon=None, generated_polygon=_BIG)
        self.assertEqual(row.effective_polygon.wkt, _BIG.wkt)

    def test_property_row_falls_back_to_circle(self) -> None:
        row = baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY)
        circle = row.effective_polygon
        self.assertIsNotNone(circle)
        self.assertTrue(circle.contains(Point(-74.0, 40.0, srid=4326)))

    def test_building_row_has_no_circle_fallback(self) -> None:
        row = baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.BUILDING)
        self.assertIsNone(row.effective_polygon)


class BoundaryQuerySetTests(TestCase):
    """Typed/keyed filters."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make("dashboard.Pin", location=self.location)
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        self.default_row = baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY)
        self.wiki_row = baker.make("dashboard.Boundary", wiki=self.wiki, location=self.location, boundary_type=BoundaryType.PROPERTY)
        self.pin_row = baker.make("dashboard.Boundary", pin=self.pin, profile=self.pin.profile, location=self.location, boundary_type=BoundaryType.BUILDING)

    def test_location_defaults_excludes_wiki_and_pin_rows(self) -> None:
        defaults = list(Boundary.objects.location_defaults())
        self.assertIn(self.default_row, defaults)
        self.assertNotIn(self.wiki_row, defaults)
        self.assertNotIn(self.pin_row, defaults)

    def test_of_type_filters_by_boundary_type(self) -> None:
        self.assertIn(self.pin_row, Boundary.objects.of_type(BoundaryType.BUILDING))
        self.assertNotIn(self.default_row, Boundary.objects.of_type(BoundaryType.BUILDING))

    def test_row_lookups(self) -> None:
        self.assertEqual(Boundary.objects.row_for_location(self.location, BoundaryType.PROPERTY), self.default_row)
        self.assertEqual(Boundary.objects.row_for_wiki(self.wiki, BoundaryType.PROPERTY), self.wiki_row)
        self.assertEqual(Boundary.objects.row_for_pin(self.pin, BoundaryType.BUILDING), self.pin_row)
        self.assertIsNone(Boundary.objects.row_for_pin(self.pin, BoundaryType.PROPERTY))


class PinPropertyResolutionTests(TestCase):
    """resolve_for_pin property order: pin → wiki → location generated → circle."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make("dashboard.Pin", location=self.location)

    def test_circle_fallback_when_nothing_exists(self) -> None:
        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)
        self.assertEqual(source, "circle")
        self.assertTrue(polygon.contains(Point(-74.0, 40.0, srid=4326)))

    def test_location_generated_beats_circle(self) -> None:
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_BIG)
        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)
        self.assertEqual(source, "generated")
        self.assertEqual(polygon.wkt, _BIG.wkt)

    def test_wiki_drawing_beats_location_generated(self) -> None:
        wiki = baker.make("dashboard.Wiki", location=self.location)
        self.pin.wiki = wiki
        self.pin.save(update_fields=["wiki"])
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_BIG)
        baker.make("dashboard.Boundary", wiki=wiki, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_MEDIUM)

        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)
        self.assertEqual(source, "wiki")
        self.assertEqual(polygon.wkt, _MEDIUM.wkt)

    def test_locations_wiki_used_when_pin_never_linked(self) -> None:
        """Imported pins have no pin.wiki; the location's wiki still applies."""
        wiki = baker.make("dashboard.Wiki", location=self.location)
        baker.make("dashboard.Boundary", wiki=wiki, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_MEDIUM)

        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)
        self.assertEqual(source, "wiki")

    def test_pin_drawing_beats_everything(self) -> None:
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_BIG)
        baker.make("dashboard.Boundary", pin=self.pin, profile=self.pin.profile, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_SMALL)

        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)
        self.assertEqual(source, "pin")
        self.assertEqual(polygon.wkt, _SMALL.wkt)

    def test_detail_pin_inherits_parent_property(self) -> None:
        baker.make("dashboard.Boundary", pin=self.pin, profile=self.pin.profile, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_BIG)
        child_location = baker.make("dashboard.Location", latitude="40.000500", longitude="-74.000500")
        child = baker.make("dashboard.Pin", profile=self.pin.profile, location=child_location, parent_pin=self.pin)

        polygon, source = Boundary.objects.resolve_for_pin(child, BoundaryType.PROPERTY)
        self.assertEqual(source, "inherited")
        self.assertEqual(polygon.wkt, _BIG.wkt)

    def test_detail_pin_own_drawing_beats_inheritance(self) -> None:
        baker.make("dashboard.Boundary", pin=self.pin, profile=self.pin.profile, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_BIG)
        child_location = baker.make("dashboard.Location", latitude="40.000500", longitude="-74.000500")
        child = baker.make("dashboard.Pin", profile=self.pin.profile, location=child_location, parent_pin=self.pin)
        baker.make("dashboard.Boundary", pin=child, profile=child.profile, location=child_location, boundary_type=BoundaryType.PROPERTY, polygon=_SMALL)

        polygon, source = Boundary.objects.resolve_for_pin(child, BoundaryType.PROPERTY)
        self.assertEqual(source, "pin")
        self.assertEqual(polygon.wkt, _SMALL.wkt)


class PinBuildingResolutionTests(TestCase):
    """Building boundaries: containment-gated inheritance, no circle fallback."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make("dashboard.Pin", location=self.location)

    def test_no_building_means_none(self) -> None:
        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.BUILDING)
        self.assertIsNone(polygon)
        self.assertIsNone(source)

    def test_location_generated_building_applies_to_root_pin(self) -> None:
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.BUILDING, generated_polygon=_SMALL)
        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.BUILDING)
        self.assertEqual(source, "generated")

    def test_detail_pin_inside_parent_building_inherits_it(self) -> None:
        baker.make("dashboard.Boundary", pin=self.pin, profile=self.pin.profile, location=self.location, boundary_type=BoundaryType.BUILDING, polygon=_SMALL)
        # Inside the ±0.001 building square.
        child_location = baker.make("dashboard.Location", latitude="40.000400", longitude="-74.000400")
        child = baker.make("dashboard.Pin", profile=self.pin.profile, location=child_location, parent_pin=self.pin)

        polygon, source = Boundary.objects.resolve_for_pin(child, BoundaryType.BUILDING)
        self.assertEqual(source, "inherited")
        self.assertEqual(polygon.wkt, _SMALL.wkt)

    def test_detail_pin_outside_parent_building_does_not_inherit(self) -> None:
        """A detail pin for another building on the property gets no building boundary."""
        baker.make("dashboard.Boundary", pin=self.pin, profile=self.pin.profile, location=self.location, boundary_type=BoundaryType.BUILDING, polygon=_SMALL)
        # Outside the ±0.001 building square (but on the same property).
        child_location = baker.make("dashboard.Location", latitude="40.002000", longitude="-74.002000")
        child = baker.make("dashboard.Pin", profile=self.pin.profile, location=child_location, parent_pin=self.pin)

        polygon, source = Boundary.objects.resolve_for_pin(child, BoundaryType.BUILDING)
        self.assertIsNone(polygon)
        self.assertIsNone(source)


class WikiResolutionTests(TestCase):
    """resolve_for_wiki: wiki row → location generated → circle (property only)."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location)

    def test_circle_fallback_for_property(self) -> None:
        polygon, source = Boundary.objects.resolve_for_wiki(self.wiki, BoundaryType.PROPERTY)
        self.assertEqual(source, "circle")
        self.assertIsNotNone(polygon)

    def test_no_fallback_for_building(self) -> None:
        polygon, source = Boundary.objects.resolve_for_wiki(self.wiki, BoundaryType.BUILDING)
        self.assertIsNone(polygon)
        self.assertIsNone(source)

    def test_wiki_drawing_beats_location_generated(self) -> None:
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_BIG)
        baker.make("dashboard.Boundary", wiki=self.wiki, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_MEDIUM)

        polygon, source = Boundary.objects.resolve_for_wiki(self.wiki, BoundaryType.PROPERTY)
        self.assertEqual(source, "wiki")
        self.assertEqual(polygon.wkt, _MEDIUM.wkt)


class LocationMatchingTests(TestCase):
    """Point→location matching uses location-default generated polygons only."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")

    def test_generated_polygon_matches_point(self) -> None:
        from urbanlens.dashboard.models.location.model import Location

        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_BIG)
        matches = Location.objects.get_all_for_point(40.0005, -74.0005)
        self.assertIn(self.location, matches)

    def test_user_drawing_never_affects_matching(self) -> None:
        """Anti-abuse rule: an inflated community drawing must not capture pins."""
        from urbanlens.dashboard.models.location.model import Location

        wiki = baker.make("dashboard.Wiki", location=self.location)
        huge = _square(-74.0, 40.0, 0.2)
        baker.make("dashboard.Boundary", wiki=wiki, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=huge)
        # A point far outside the 50 m proximity fallback but inside the drawing.
        matches = Location.objects.get_all_for_point(40.1, -74.1)
        self.assertNotIn(self.location, matches)

    def test_proximity_fallback_when_no_generated_polygon(self) -> None:
        from urbanlens.dashboard.models.location.model import Location

        matches = Location.objects.get_all_for_point(40.0001, -74.0001)
        self.assertIn(self.location, matches)


class BoundaryControllerTests(TestCase):
    """Pin boundary endpoints: typed payloads, save/clear semantics."""

    def setUp(self):
        from django.test import RequestFactory

        self.factory = RequestFactory()
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location, slug="test-pin-boundary")
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_BIG, generated_at="2026-07-01T00:00:00Z")
        baker.make("dashboard.Boundary", location=self.location, boundary_type=BoundaryType.BUILDING, generated_polygon=_SMALL, generated_at="2026-07-01T00:00:00Z")

    def _request(self, method="get", data=None):
        if method == "post":
            req = self.factory.post("/boundary/", data=json.dumps(data or {}), content_type="application/json")
            req.data = data or {}
        else:
            req = self.factory.get("/boundary/")
        req.user = self.user
        return req

    def test_get_returns_typed_boundaries(self) -> None:
        from urbanlens.dashboard.controllers.boundary import BoundaryController

        response = BoundaryController().get_boundaries(self._request(), self.pin.slug)
        payload = json.loads(response.content)

        self.assertEqual(payload["boundaries"]["property"]["source"], "generated")
        self.assertEqual(payload["boundaries"]["building"]["source"], "generated")
        self.assertEqual(payload["boundaries"]["property"]["polygon"], json.loads(_BIG.geojson))
        self.assertFalse(payload["pending"])

    def test_get_schedules_generation_when_not_yet_ran(self) -> None:
        from urbanlens.dashboard.controllers.boundary import BoundaryController

        Boundary.objects.location_defaults().delete()
        with mock.patch("urbanlens.dashboard.controllers.boundary.schedule_panel_fetch", return_value=True) as schedule:
            response = BoundaryController().get_boundaries(self._request(), self.pin.slug)

        payload = json.loads(response.content)
        schedule.assert_called_once_with("boundary", self.pin)
        self.assertTrue(payload["pending"])
        # No generated data yet: property falls back to the default circle.
        self.assertEqual(payload["boundaries"]["property"]["source"], "circle")

    def test_save_creates_typed_pin_row(self) -> None:
        from urbanlens.dashboard.controllers.boundary import BoundaryController

        response = BoundaryController().save_boundary(
            self._request("post", {"boundary_type": "building", "polygon": json.loads(_SMALL.geojson)}),
            self.pin.slug,
        )
        payload = json.loads(response.content)

        self.assertEqual(payload["status"], "ok")
        row = Boundary.objects.get(pin=self.pin, boundary_type=BoundaryType.BUILDING)
        self.assertIsNotNone(row.polygon)
        self.assertEqual(row.profile_id, self.user.profile.pk)
        self.assertEqual(payload["boundaries"]["building"]["source"], "pin")

    def test_save_rejects_unknown_boundary_type(self) -> None:
        from urbanlens.dashboard.controllers.boundary import BoundaryController

        response = BoundaryController().save_boundary(
            self._request("post", {"boundary_type": "moat", "polygon": json.loads(_SMALL.geojson)}),
            self.pin.slug,
        )
        self.assertEqual(response.status_code, 400)

    def test_clear_deletes_pin_row_and_falls_back(self) -> None:
        from urbanlens.dashboard.controllers.boundary import BoundaryController

        baker.make("dashboard.Boundary", pin=self.pin, profile=self.user.profile, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_MEDIUM)

        response = BoundaryController().save_boundary(
            self._request("post", {"boundary_type": "property", "polygon": None}),
            self.pin.slug,
        )
        payload = json.loads(response.content)

        self.assertFalse(Boundary.objects.filter(pin=self.pin, boundary_type=BoundaryType.PROPERTY).exists())
        self.assertEqual(payload["boundaries"]["property"]["source"], "generated")

    def test_detail_buildings_included_in_payload(self) -> None:
        from urbanlens.dashboard.controllers.boundary import BoundaryController

        child_location = baker.make("dashboard.Location", latitude="40.000400", longitude="-74.000400")
        child = baker.make("dashboard.Pin", profile=self.user.profile, location=child_location, parent_pin=self.pin)
        baker.make("dashboard.Boundary", pin=child, profile=self.user.profile, location=child_location, boundary_type=BoundaryType.BUILDING, polygon=_SMALL)

        response = BoundaryController().get_boundaries(self._request(), self.pin.slug)
        payload = json.loads(response.content)

        self.assertEqual(len(payload["detail_buildings"]), 1)
        self.assertEqual(payload["detail_buildings"][0]["pin_id"], child.pk)
