"""Phase 6 of the external API parity-polish pass: Memories timeline, on-this-day, and the
batch-scan pin-suggestion review queue.

Covers:

1. **Memories timeline** wraps ``services.memories.aggregator.get_memory_events`` with the
   external API's standard page envelope, defaulting to the trailing 90 days.
2. **On-this-day** mirrors the internal callout's past-year/this-month-day query across visits,
   routes, and photos, excluding the current year.
3. **Pin suggestions** exposes ``PinSuggestion`` - a genuinely new external surface, since the
   existing ``PinSuggestionsView``/``pin-suggestions/`` route only ever *creates* one. Accept/reject
   are owner-scoped and 404 (not 403) for another profile's suggestion or an already-handled one.
"""

from __future__ import annotations

import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import LineString
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.routes.model import Route, RouteSource
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

if TYPE_CHECKING:
    from collections.abc import Iterable


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for a raw API key."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, scopes: Iterable[ApiKeyScope]) -> str:
    """Issue an API key for *user* carrying exactly *scopes*, returning the raw key."""
    api_key, raw_key = generate_api_key(user, "Test Key")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


class MemoriesTimelineTests(TestCase):
    """GET /memories/timeline/."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ])
        self.pin = baker.make(Pin, profile=self.profile)

    def test_scope_is_required(self) -> None:
        """A key without photos:read is refused."""
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PINS_READ])
        response = self.client.get(reverse("external_api:memories.timeline"), **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_default_window_includes_recent_visit_and_excludes_old_one(self) -> None:
        """With no start/end given, only the trailing-90-day visit is returned."""
        today = timezone.now()
        PinVisit.objects.create(pin=self.pin, visited_at=today - datetime.timedelta(days=5))
        PinVisit.objects.create(pin=self.pin, visited_at=today - datetime.timedelta(days=200))

        response = self.client.get(reverse("external_api:memories.timeline"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        types = [event["type"] for event in response.json()["results"]]
        self.assertEqual(types.count("visit"), 1)

    def test_explicit_date_range_is_honored(self) -> None:
        """start/end query params override the default 90-day window."""
        far_past = timezone.now() - datetime.timedelta(days=400)
        PinVisit.objects.create(pin=self.pin, visited_at=far_past)

        response = self.client.get(
            reverse("external_api:memories.timeline"),
            {"start": (far_past - datetime.timedelta(days=1)).date().isoformat(), "end": (far_past + datetime.timedelta(days=1)).date().isoformat()},
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(len(response.json()["results"]), 1)

    def test_response_uses_the_standard_page_envelope(self) -> None:
        """count/next/previous/results, not a bespoke shape."""
        response = self.client.get(reverse("external_api:memories.timeline"), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        body = response.json()
        self.assertIn("count", body)
        self.assertIn("next", body)
        self.assertIn("previous", body)
        self.assertIn("results", body)


class MemoriesOnThisDayTests(TestCase):
    """GET /memories/on-this-day/."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ])
        self.pin = baker.make(Pin, profile=self.profile)

    def test_scope_is_required(self) -> None:
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PINS_READ])
        response = self.client.get(reverse("external_api:memories.on_this_day"), **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_past_year_same_month_day_visit_is_included(self) -> None:
        """A visit exactly one year ago today surfaces; this year's does not."""
        today = timezone.now()
        last_year = today.replace(year=today.year - 1)
        PinVisit.objects.create(pin=self.pin, visited_at=last_year, notes="last year")
        PinVisit.objects.create(pin=self.pin, visited_at=today, notes="this year")

        response = self.client.get(reverse("external_api:memories.on_this_day"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        body = response.json()
        self.assertEqual(len(body["visits"]), 1)
        self.assertEqual(body["visits"][0]["notes"], "last year")

    def test_different_month_day_is_excluded(self) -> None:
        """A visit from a different day entirely never appears."""
        different_day = timezone.now().replace(year=timezone.now().year - 1) - datetime.timedelta(days=90)
        PinVisit.objects.create(pin=self.pin, visited_at=different_day)

        response = self.client.get(reverse("external_api:memories.on_this_day"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.json()["visits"], [])

    def test_past_year_route_is_included(self) -> None:
        """A route started one year ago today surfaces, with its path as GeoJSON."""
        today = timezone.now()
        last_year = today.replace(year=today.year - 1)
        Route.objects.create(profile=self.profile, source=RouteSource.GPX_TRACK, path=LineString((-74.0, 40.7), (-74.01, 40.71)), started_at=last_year, distance_meters=123.4)

        response = self.client.get(reverse("external_api:memories.on_this_day"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        routes = response.json()["routes"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["path"]["type"], "LineString")


class PinSuggestionQueueTests(TestCase):
    """GET /suggestions/pins/."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)
        self.raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ, ApiKeyScope.PHOTOS_WRITE])

    def _make_suggestion(self, profile: Profile, **kwargs) -> PinSuggestion:
        defaults = {"profile": profile, "latitude": 40.7, "longitude": -74.0, "origin": PinSuggestionOrigin.LOCAL_SCAN, "status": PinSuggestionStatus.PENDING}
        defaults.update(kwargs)
        return PinSuggestion.objects.create(**defaults)

    def test_scope_is_required(self) -> None:
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PINS_READ])
        response = self.client.get(reverse("external_api:suggestions.pins"), **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_lists_only_the_callers_pending_suggestions(self) -> None:
        """Another profile's suggestions and non-pending ones are excluded."""
        mine = self._make_suggestion(self.profile, suggested_name="Mine")
        self._make_suggestion(self.other_profile, suggested_name="Theirs")
        self._make_suggestion(self.profile, status=PinSuggestionStatus.ACCEPTED, suggested_name="Already handled")

        response = self.client.get(reverse("external_api:suggestions.pins"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        suggestions = response.json()["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["id"], mine.pk)
        self.assertEqual(suggestions[0]["suggested_name"], "Mine")
        self.assertTrue(suggestions[0]["is_new_pin"])


class PinSuggestionActionTests(TestCase):
    """POST /suggestions/pins/{id}/{accept|reject}/."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)
        self.raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ, ApiKeyScope.PHOTOS_WRITE])

    def _make_suggestion(self, profile: Profile, **kwargs) -> PinSuggestion:
        defaults = {"profile": profile, "latitude": 40.7, "longitude": -74.0, "origin": PinSuggestionOrigin.LOCAL_SCAN, "status": PinSuggestionStatus.PENDING}
        defaults.update(kwargs)
        return PinSuggestion.objects.create(**defaults)

    def _action_url(self, suggestion_id: int, action: str) -> str:
        return reverse("external_api:suggestions.pins.action", kwargs={"suggestion_id": suggestion_id, "action": action})

    def test_scope_is_required(self) -> None:
        suggestion = self._make_suggestion(self.profile)
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ])
        response = self.client.post(self._action_url(suggestion.pk, "accept"), **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    def test_accepting_a_new_pin_suggestion_creates_a_pin(self, _mock_resolve_name) -> None:
        """A suggestion with no matched pin creates one and is marked accepted."""
        suggestion = self._make_suggestion(self.profile, suggested_name="Old Mill")

        response = self.client.post(self._action_url(suggestion.pk, "accept"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.ACCEPTED)
        self.assertIsNotNone(suggestion.pin)
        self.assertEqual(suggestion.pin.name, "Old Mill")

    def test_rejecting_a_suggestion_marks_it_rejected(self) -> None:
        suggestion = self._make_suggestion(self.profile)

        response = self.client.post(self._action_url(suggestion.pk, "reject"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.REJECTED)
        self.assertIsNone(suggestion.pin)

    def test_another_profiles_suggestion_is_404_not_403(self) -> None:
        """Anti-enumeration: someone else's suggestion id looks nonexistent, not forbidden."""
        suggestion = self._make_suggestion(self.other_profile)

        response = self.client.post(self._action_url(suggestion.pk, "accept"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_already_handled_suggestion_is_404(self) -> None:
        suggestion = self._make_suggestion(self.profile, status=PinSuggestionStatus.ACCEPTED)

        response = self.client.post(self._action_url(suggestion.pk, "reject"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_unknown_action_is_404(self) -> None:
        suggestion = self._make_suggestion(self.profile)

        response = self.client.post(self._action_url(suggestion.pk, "delete-forever"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
