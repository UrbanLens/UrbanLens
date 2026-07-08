"""Tests for creating PinVisit rows from browser geolocation fixes."""

from __future__ import annotations

import datetime

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits import record_geolocation_pin_visits


def _square_around(lng: float, lat: float, delta: float = 0.001) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    polygon = Polygon(ring, srid=4326)
    return MultiPolygon(polygon, srid=4326)


class RecordGeolocationPinVisitsTests(TestCase):
    """record_geolocation_pin_visits() creates one same-day visit per containing pin."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=self.location,
        )
        baker.make(
            "dashboard.Campus",
            location=self.location,
            pin=self.pin,
            profile=self.profile,
            generated_polygon=_square_around(-74.0, 40.0),
        )

    def test_creates_geolocation_visit_when_point_is_inside_pin_boundary(self):
        visited_at = timezone.make_aware(datetime.datetime(2026, 7, 6, 15, 30, 0))

        visits = record_geolocation_pin_visits(self.profile, latitude=40.0002, longitude=-74.0002, visited_at=visited_at)

        self.assertEqual(len(visits), 1)
        visit = PinVisit.objects.get(pin=self.pin)
        self.assertEqual(visit.source, VisitSource.GEOLOCATION)
        self.assertEqual(visit.visited_at, visited_at)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, visited_at)

    def test_does_not_create_duplicate_visit_for_same_pin_on_same_day(self):
        existing_at = timezone.make_aware(datetime.datetime(2026, 7, 6, 8, 0, 0))
        baker.make(PinVisit, pin=self.pin, source=VisitSource.MANUAL, visited_at=existing_at)

        visits = record_geolocation_pin_visits(
            self.profile,
            latitude=40.0002,
            longitude=-74.0002,
            visited_at=timezone.make_aware(datetime.datetime(2026, 7, 6, 20, 0, 0)),
        )

        self.assertEqual(visits, [])
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 1)

    def test_endpoint_records_created_visits_for_current_user(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("map.geolocation.visits"),
            data={"latitude": 40.0002, "longitude": -74.0002},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)
        self.assertTrue(PinVisit.objects.filter(pin=self.pin, source=VisitSource.GEOLOCATION).exists())
