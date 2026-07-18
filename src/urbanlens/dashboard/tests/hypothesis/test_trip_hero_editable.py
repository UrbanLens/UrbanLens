"""Tests for the trip detail hero's click-to-edit-in-place name/description.

Covers:
- TripEditView's partial-update fix: submitting one field must not silently
  clear the other three (name/description/start_date/end_date) - previously
  every field was unconditionally overwritten from the request body even
  when the key was absent, safe only because the pre-existing "Edit Trip"
  dialog always submitted all four together.
- The trip hero renders name/description as click-to-edit-in-place for a
  joined member (including a placeholder when description is empty, rather
  than hiding it), and as plain read-only text otherwise.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripMembership

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def _make_trip(creator_profile: Profile, **kwargs) -> Trip:
    trip = Trip.objects.create(name=kwargs.pop("name", "Test Trip"), creator=creator_profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=creator_profile, defaults={"rsvp": "yes"})
    return trip


class TripEditPartialUpdateTests(TestCase):
    """A single-field POST to trips.edit must not clear the other fields."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.trip = _make_trip(
            self.profile,
            description="Original description.",
            start_date="2026-08-01",
            end_date="2026-08-05",
        )

    def _post(self, body: dict):
        return self.client.post(
            reverse("trips.edit", kwargs={"trip_slug": self.trip.slug}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_submitting_only_name_preserves_description_and_dates(self) -> None:
        response = self._post({"name": "New Name"})
        self.assertEqual(response.status_code, 200)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.name, "New Name")
        self.assertEqual(self.trip.description, "Original description.")
        self.assertEqual(str(self.trip.start_date), "2026-08-01")
        self.assertEqual(str(self.trip.end_date), "2026-08-05")

    def test_submitting_only_description_preserves_name_and_dates(self) -> None:
        response = self._post({"description": "Updated description."})
        self.assertEqual(response.status_code, 200)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.name, "Test Trip")
        self.assertEqual(self.trip.description, "Updated description.")
        self.assertEqual(str(self.trip.start_date), "2026-08-01")
        self.assertEqual(str(self.trip.end_date), "2026-08-05")

    def test_submitting_only_start_date_preserves_everything_else(self) -> None:
        response = self._post({"start_date": "2026-09-01"})
        self.assertEqual(response.status_code, 200)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.name, "Test Trip")
        self.assertEqual(self.trip.description, "Original description.")
        self.assertEqual(str(self.trip.start_date), "2026-09-01")
        self.assertEqual(str(self.trip.end_date), "2026-08-05")

    def test_explicit_empty_description_still_clears_it(self) -> None:
        """The bugfix must not break the existing "clear via empty string" behavior."""
        response = self._post({"description": ""})
        self.assertEqual(response.status_code, 200)
        self.trip.refresh_from_db()
        self.assertIsNone(self.trip.description)
        self.assertEqual(str(self.trip.start_date), "2026-08-01")

    def test_submitting_all_four_together_still_works(self) -> None:
        """Matches the existing "Edit Trip" dialog's behavior - unaffected by the fix."""
        response = self._post({"name": "New Name", "description": "New desc.", "start_date": "2026-10-01", "end_date": "2026-10-05"})
        self.assertEqual(response.status_code, 200)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.name, "New Name")
        self.assertEqual(self.trip.description, "New desc.")
        self.assertEqual(str(self.trip.start_date), "2026-10-01")
        self.assertEqual(str(self.trip.end_date), "2026-10-05")


class TripHeroEditableRenderingTests(TestCase):
    """Joined members see click-to-edit-in-place; others see plain text."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.trip = _make_trip(self.profile, description="A weekend in the mountains.")

    def _get(self):
        return self.client.get(reverse("trips.detail", kwargs={"trip_slug": self.trip.slug}))

    def test_joined_member_sees_editable_title(self) -> None:
        response = self._get()
        self.assertContains(response, "trip-title-editable")
        self.assertContains(response, f'data-raw-name="{self.trip.name}"')

    def test_joined_member_sees_editable_description_with_value(self) -> None:
        response = self._get()
        self.assertContains(response, "trip-description-editable")
        self.assertContains(response, 'data-raw-description="A weekend in the mountains."')

    def test_joined_member_sees_placeholder_for_empty_description(self) -> None:
        empty_trip = _make_trip(self.profile, name="Bare Trip")
        response = self.client.get(reverse("trips.detail", kwargs={"trip_slug": empty_trip.slug}))
        self.assertContains(response, "Add a description...")
        self.assertContains(response, 'data-raw-description=""')

    def test_invited_but_not_joined_member_sees_plain_text_not_editable(self) -> None:
        other_user = baker.make(User)
        other_profile = other_user.profile
        TripMembership.objects.create(trip=self.trip, profile=other_profile, status=TripMembership.STATUS_INVITED)
        self.client.force_login(other_user)

        response = self._get()
        self.assertContains(response, self.trip.name)
        self.assertContains(response, "A weekend in the mountains.")
        # The wiring script's `.closest('.trip-title-editable')` legitimately
        # contains this class name as inert text on every render - check the
        # actual rendered element's class list, not just "does this string
        # appear anywhere in the page source" (same caveat as the bio/pin-list
        # precedents for this exact false-positive).
        self.assertNotContains(response, 'trip-title-editable"')
        self.assertNotContains(response, 'trip-description-editable"')
