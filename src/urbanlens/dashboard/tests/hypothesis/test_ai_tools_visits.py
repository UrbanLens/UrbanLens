"""Tests for services.ai.tools.visits - have_i_been_here, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime

from django.contrib.gis.geos import LineString
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, available_tools, execute

_LAT, _LNG = "42.5", "-73.5"


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


def _pin(profile, lat: str, lng: str, name: str) -> Pin:
    # Pin.slug is unique per-profile, not globally - a "belongs to someone else"
    # test needs an explicit, distinct name to actually exercise that.
    location = baker.make(Location, latitude=lat, longitude=lng)
    return baker.make(Pin, profile=profile, location=location, name=name, name_is_user_provided=True)


class HaveIBeenHereTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.pin = _pin(self.profile, _LAT, _LNG, "Mine")

    def test_appears_in_available_tools(self) -> None:
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("have_i_been_here", names)

    def test_no_evidence_when_nothing_matches(self) -> None:
        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))
        self.assertEqual(result.data["status"], "no_evidence")

    def test_a_logged_visit_is_confirmed(self) -> None:
        visited_at = timezone.make_aware(datetime(2026, 6, 1, 14, 0))  # noqa: DTZ001
        baker.make(PinVisit, pin=self.pin, visited_at=visited_at, tentative=False)

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "confirmed")
        self.assertEqual(result.data["visit_count"], 1)
        self.assertEqual(result.data["last_visited"], "2026-06-01")

    def test_a_tentative_visit_alone_is_not_confirmed(self) -> None:
        baker.make(PinVisit, pin=self.pin, visited_at=timezone.now(), tentative=True)

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "no_evidence")

    def test_the_visited_status_label_is_confirmed_even_without_a_visit_row(self) -> None:
        from urbanlens.dashboard.models.labels.meta import KIND_STATUS
        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, name="Visited", kind=KIND_STATUS, profile=None)
        self.pin.labels.add(label)

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "confirmed")
        self.assertIsNone(result.data["last_visited"])

    def test_a_pending_visit_suggestion_is_passed_nearby_not_confirmed(self) -> None:
        baker.make(
            VisitSuggestion,
            suggested_to=self.profile,
            location=self.pin.location,
            latitude=_LAT,
            longitude=_LNG,
            visited_at=timezone.now(),
            status=VisitSuggestionStatus.PENDING,
            from_my_activity=True,
        )

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "passed_nearby")

    def test_a_nearby_recorded_route_is_passed_nearby_not_confirmed(self) -> None:
        lng, lat = float(_LNG), float(_LAT)
        baker.make(Route, profile=self.profile, path=LineString([(lng, lat), (lng + 0.0005, lat + 0.0005)], srid=4326))

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "passed_nearby")

    def test_a_distant_route_is_no_evidence(self) -> None:
        baker.make(Route, profile=self.profile, path=LineString([(-120.0, 40.0), (-120.1, 40.1)], srid=4326))

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "no_evidence")

    def test_another_profiles_route_is_never_evidence(self) -> None:
        lng, lat = float(_LNG), float(_LAT)
        baker.make(Route, profile=self.other, path=LineString([(lng, lat), (lng + 0.0005, lat + 0.0005)], srid=4326))

        result = execute("have_i_been_here", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["status"], "no_evidence")

    def test_missing_pin_slug_is_an_error_block_not_a_raise(self) -> None:
        result = execute("have_i_been_here", {"pin_slug": ""}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_another_profiles_pin_slug_never_resolves(self) -> None:
        theirs = _pin(self.other, "44.0", "-75.0", "Theirs")
        baker.make(PinVisit, pin=theirs, visited_at=timezone.now(), tentative=False)

        result = execute("have_i_been_here", {"pin_slug": theirs.slug}, _context(self.profile))

        self.assertIn("error", result.data)
