"""Tests for PinEditView category update logic."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_edit import PinEditView, PinOverviewView
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError


class PinEditCategoryUpdateTests(TestCase):
    """Regression tests for partial updates and category scoping."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)
        self.existing_cat = baker.make(
            Label, name="existing", kind="category", profile=self.profile,
        )
        self.pin.labels.add(self.existing_cat)

    def _post(self, body: dict) -> object:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        # The rendered overview partial checks pin.has_place_name, which
        # resolves an uncached Location's place name from Google - mock it
        # so the response render doesn't make an outbound API call.
        with (
            patch("urbanlens.dashboard.controllers.pin_edit._ensure_location_address"),
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def _categories(self):
        return self.pin.labels.filter(kind="category")

    def test_partial_priority_update_preserves_existing_categories(self) -> None:
        """Submitting only priority must not clear the pin's categories."""
        response = self._post({"priority": 3})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        category_ids = list(self._categories().values_list("id", flat=True))
        self.assertIn(
            self.existing_cat.id,
            category_ids,
            "Partial edit (priority only) must not clear categories",
        )

    def test_explicit_category_update_uses_owner_categories(self) -> None:
        """Submitting categories should resolve/create against the pin owner's profile."""
        new_cat_name = "wilderness"
        response = self._post({"categories": new_cat_name})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        cats = list(self._categories())
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0].name, new_cat_name)
        # Must be owned by the pin's profile, not global
        self.assertEqual(cats[0].profile_id, self.profile.pk)

    def test_explicit_empty_categories_clears_all(self) -> None:
        """Submitting an empty categories string explicitly clears all categories."""
        response = self._post({"categories": ""})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self._categories().count(), 0)

    def test_duplicate_category_names_are_deduplicated(self) -> None:
        """Comma-separated list with duplicates should not create two labels."""
        response = self._post({"categories": "nature,nature,Nature"})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self._categories().count(), 1)


class PinEditNameAliasTests(TestCase):
    """Renames preserve every name as an alias, including the current one."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)

    def _post(self, body: dict) -> object:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        # The rendered overview partial checks pin.has_place_name, which
        # resolves an uncached Location's place name from Google - mock it
        # so the response render doesn't make an outbound API call.
        with (
            patch("urbanlens.dashboard.controllers.pin_edit._ensure_location_address"),
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def test_renaming_pin_keeps_old_and_new_names_as_aliases(self) -> None:
        response = self._post({"name": "New Factory"})

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "New Factory")
        self.assertCountEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Old Factory", "New Factory"])

    def test_resubmitting_same_name_does_not_add_an_alias(self) -> None:
        response = self._post({"name": "Old Factory"})

        self.assertEqual(response.status_code, 200)
        # Only the creation-time alias for the current name exists.
        self.assertEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Old Factory"])

    def test_partial_update_without_name_does_not_add_an_alias(self) -> None:
        response = self._post({"priority": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Old Factory"])


class PinOverviewGeocodingFailureTests(TestCase):
    """The Details card must degrade gracefully, not 500, when geocoding fails.

    Regression test for a bug where a pin whose Location has no street data
    yet (true for any new pin, and for any pin never previously backfilled)
    re-triggers a synchronous Google Geocoding call on every /overview/ visit.
    Once the geocoding service's rate limit is exhausted or it's disabled,
    that call raises RequestCancelledError/RateLimitExceededError, which
    _ensure_location_address didn't catch - unlike every other Google Places
    call site in this codebase - so the whole request 500'd instead of just
    rendering without the freshly-geocoded address fields.
    """

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)
        self.pin.location.route = ""
        self.pin.location.save(update_fields=["route"])

    def test_rate_limit_exceeded_during_geocoding_does_not_500(self) -> None:
        req = self.factory.get(f"/map/pin/{self.pin.slug}/overview/")
        req.user = self.user
        with (
            patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "test-key"),
            patch(
                "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway.geocode_coordinates",
                side_effect=RateLimitExceededError("google_geocoding"),
            ),
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            response = PinOverviewView.as_view()(req, pin_slug=self.pin.slug)

        self.assertEqual(response.status_code, 200)
