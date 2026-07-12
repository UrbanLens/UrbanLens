"""Tests for the "overlapping pins" map filter.

A pin's footprint is its effective property boundary: a drawn/generated
polygon when one exists, else a default circle around its coordinates (see
``BoundaryManager.effective_polygon_for_pin``). ``PinQuerySet.overlapping()``
returns every pin whose footprint intersects another pin's footprint (from the
same queryset), which - since every pin resolves to *some* footprint - also
catches pins accidentally left stacked on identical/near-identical
coordinates (e.g. by the merge/child-pin coordinate bugs).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


def _pin_at(profile, name: str, latitude: float, longitude: float) -> Pin:
    location = baker.make(Location, latitude=latitude, longitude=longitude)
    return baker.make(Pin, profile=profile, location=location, name=name)


def _square_polygon(center_lat: float, center_lng: float, half_side_deg: float) -> MultiPolygon:
    ring = (
        (center_lng - half_side_deg, center_lat - half_side_deg),
        (center_lng - half_side_deg, center_lat + half_side_deg),
        (center_lng + half_side_deg, center_lat + half_side_deg),
        (center_lng + half_side_deg, center_lat - half_side_deg),
        (center_lng - half_side_deg, center_lat - half_side_deg),
    )
    return MultiPolygon(Polygon(ring), srid=4326)


class OverlappingPinsQuerySetTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_nearby_pins_without_boundaries_overlap_via_circle_fallback(self) -> None:
        # ~15m apart - well within the combined 100m (50m + 50m) default circle radius.
        a = _pin_at(self.profile, "A", 42.00000, -73.00000)
        b = _pin_at(self.profile, "B", 42.00010, -73.00010)
        result = {p.pk for p in Pin.objects.filter(profile=self.profile).overlapping()}
        self.assertEqual(result, {a.pk, b.pk})

    def test_distant_pins_do_not_overlap(self) -> None:
        _pin_at(self.profile, "A", 42.0000, -73.0000)
        _pin_at(self.profile, "B", 43.0000, -74.0000)
        result = set(Pin.objects.filter(profile=self.profile).overlapping())
        self.assertEqual(result, set())

    def test_only_the_overlapping_pair_is_returned(self) -> None:
        a = _pin_at(self.profile, "A", 42.00000, -73.00000)
        b = _pin_at(self.profile, "B", 42.00010, -73.00010)
        _pin_at(self.profile, "Far", 10.0000, 10.0000)
        result = {p.pk for p in Pin.objects.filter(profile=self.profile).overlapping()}
        self.assertEqual(result, {a.pk, b.pk})

    def test_explicit_overlapping_boundaries_are_detected_even_when_centers_are_far_apart(self) -> None:
        # Two large drawn property boundaries that overlap even though their pin
        # markers themselves sit well outside each other's default circle radius.
        a = _pin_at(self.profile, "A", 42.0000, -73.0000)
        b = _pin_at(self.profile, "B", 42.0020, -73.0020)
        Boundary.objects.create(pin=a, profile=self.profile, boundary_type=BoundaryType.PROPERTY, polygon=_square_polygon(42.0000, -73.0000, 0.003))
        Boundary.objects.create(pin=b, profile=self.profile, boundary_type=BoundaryType.PROPERTY, polygon=_square_polygon(42.0020, -73.0020, 0.003))
        result = {p.pk for p in Pin.objects.filter(profile=self.profile).overlapping()}
        self.assertEqual(result, {a.pk, b.pk})

    def test_scoped_to_the_starting_queryset(self) -> None:
        other_user = baker.make(User)
        a = _pin_at(self.profile, "Mine", 42.00000, -73.00000)
        _pin_at(other_user.profile, "TheirsNearby", 42.00010, -73.00010)
        result = {p.pk for p in Pin.objects.filter(profile=self.profile).overlapping()}
        self.assertEqual(result, set())
        self.assertNotIn(a.pk, result)


class OverlappingPinsSearchIntegrationTests(TestCase):
    """The overlapping_pins SearchForm field flows through filter_by_criteria."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_search_form_field_is_optional_boolean(self) -> None:
        form = SearchForm(data={})
        self.assertTrue(form.is_valid())
        self.assertFalse(form.cleaned_data["overlapping_pins"])

    def test_filter_by_criteria_applies_overlap_filter(self) -> None:
        a = _pin_at(self.profile, "A", 42.00000, -73.00000)
        b = _pin_at(self.profile, "B", 42.00010, -73.00010)
        _pin_at(self.profile, "Far", 10.0000, 10.0000)
        qs = Pin.objects.filter(profile=self.profile).filter_by_criteria({"overlapping_pins": True})
        self.assertEqual(set(qs), {a, b})

    def test_filter_by_criteria_skips_overlap_filter_when_unset(self) -> None:
        _pin_at(self.profile, "A", 42.0000, -73.0000)
        _pin_at(self.profile, "B", 10.0000, 10.0000)
        qs = Pin.objects.filter(profile=self.profile).filter_by_criteria({})
        self.assertEqual(qs.count(), 2)

    def test_map_search_endpoint_accepts_the_checkbox(self) -> None:
        a = _pin_at(self.profile, "A", 42.00000, -73.00000)
        b = _pin_at(self.profile, "B", 42.00010, -73.00010)
        far = _pin_at(self.profile, "Far", 10.0000, 10.0000)
        response = self.client.post(reverse("map.search"), {"overlapping_pins": "on"})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(str(a.uuid), body)
        self.assertIn(str(b.uuid), body)
        self.assertNotIn(str(far.uuid), body)
