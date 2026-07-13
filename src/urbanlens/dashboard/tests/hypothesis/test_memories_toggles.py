"""Tests for the History toggles (track_pin_visits/track_routes/track_geolocation).

Covers the guard functions in services.visits and the call sites that must
create zero rows when disabled - including the "no exceptions, even for
explicit imports" requirement for GPX route import.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.geos import LineString
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.visits import VisitHistoryView
from urbanlens.dashboard.models.routes.model import Route, RouteSource
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.apis.locations.route_import import import_routes_streaming
from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute
from urbanlens.dashboard.services.visits import (
    geolocation_tracking_allowed,
    record_geolocation_pin_visits,
    route_import_allowed,
    visit_logging_allowed,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class GuardFunctionTests(TestCase):
    """The three guard functions mirror their Profile fields directly."""

    def test_visit_logging_allowed_matches_field(self) -> None:
        pin: Pin = baker.make_recipe("dashboard.pin")
        pin.profile.track_pin_visits = False
        self.assertFalse(visit_logging_allowed(pin.profile))
        pin.profile.track_pin_visits = True
        self.assertTrue(visit_logging_allowed(pin.profile))

    def test_route_import_allowed_matches_field(self) -> None:
        pin: Pin = baker.make_recipe("dashboard.pin")
        pin.profile.track_routes = False
        self.assertFalse(route_import_allowed(pin.profile))

    def test_geolocation_tracking_allowed_matches_field(self) -> None:
        pin: Pin = baker.make_recipe("dashboard.pin")
        pin.profile.track_geolocation = False
        self.assertFalse(geolocation_tracking_allowed(pin.profile))


class ManualVisitLoggingBlockedTests(TestCase):
    """VisitHistoryView.post refuses to log a manual visit when tracking is off."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.pin: Pin = baker.make_recipe("dashboard.pin")

    def _post(self):
        request = self.factory.post(reverse("pin.visits", args=[self.pin.slug]), {"visited_date": "2026-01-01"})
        request.user = self.pin.profile.user
        return VisitHistoryView.as_view()(request, pin_slug=self.pin.slug)

    def test_blocked_when_track_pin_visits_disabled(self) -> None:
        self.pin.profile.track_pin_visits = False
        self.pin.profile.save(update_fields=["track_pin_visits"])
        response = self._post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 0)

    def test_allowed_when_track_pin_visits_enabled(self) -> None:
        self.assertTrue(self.pin.profile.track_pin_visits)
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 1)


class GeolocationTrackingBlockedTests(TestCase):
    """record_geolocation_pin_visits creates zero rows when track_geolocation is off."""

    def test_no_visits_created_when_disabled(self) -> None:
        pin: Pin = baker.make_recipe("dashboard.pin")
        pin.profile.track_geolocation = False
        pin.profile.save(update_fields=["track_geolocation"])
        lat, lng = pin.location.latitude, pin.location.longitude

        created = record_geolocation_pin_visits(pin.profile, latitude=lat, longitude=lng)

        self.assertEqual(created, [])
        self.assertEqual(PinVisit.objects.filter(pin=pin).count(), 0)


class RouteImportBlockedTests(TestCase):
    """import_routes_streaming creates zero Route/PinVisit rows when track_routes is off - no exceptions for explicit imports."""

    def _parsed_route(self, profile) -> ParsedRoute:
        route = Route(
            profile=profile,
            source=RouteSource.GPX_TRACK,
            path=LineString((-74.0, 40.7), (-74.01, 40.71)),
            raw_point_count=2,
            simplified_point_count=2,
        )
        return ParsedRoute(route=route, raw_points=[])

    def test_no_route_created_when_track_routes_disabled(self) -> None:
        profile = baker.make_recipe("dashboard.pin").profile
        profile.track_routes = False
        profile.save(update_fields=["track_routes"])

        events = list(import_routes_streaming([self._parsed_route(profile)], profile))

        self.assertEqual(Route.objects.filter(profile=profile).count(), 0)
        self.assertTrue(any("routes_disabled" in event for event in events))

    def test_route_created_when_track_routes_enabled(self) -> None:
        profile = baker.make_recipe("dashboard.pin").profile
        self.assertTrue(profile.track_routes)

        list(import_routes_streaming([self._parsed_route(profile)], profile))

        self.assertEqual(Route.objects.filter(profile=profile).count(), 1)
