"""Tests for SavedFilterMatchCountsView - live per-filter pin counts for the
map toolbar's icon-less filter badges (an icon-less filter shows a count
instead of a generic fallback icon, so multiple icon-less filters are
distinguishable from each other).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class SavedFilterMatchCountsViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.tagged_pin = baker.make(Pin, profile=self.profile, name="Tagged Pin", location=baker.make(Location, latitude=40.0, longitude=-74.0))
        self.other_pin = baker.make(Pin, profile=self.profile, name="Other Pin", location=baker.make(Location, latitude=41.0, longitude=-75.0))

    def _url(self, **params) -> str:
        base = reverse("saved_filters.counts")
        if not params:
            return base
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base}?{query}"

    def test_no_saved_filters_returns_empty_counts(self) -> None:
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"counts": {}})

    def test_single_filter_count_matches_its_own_criteria(self) -> None:
        saved_filter = SavedFilter.objects.create(profile=self.profile, name="Tagged Only", criteria={"name": "Tagged"})
        response = self.client.get(self._url())
        data = response.json()
        self.assertEqual(data["counts"][str(saved_filter.uuid)], 1)

    def test_count_reflects_sidebar_criteria_combined_with_the_filter(self) -> None:
        baker.make(Pin, profile=self.profile, name="Tagged Second", location=baker.make(Location, latitude=42.0, longitude=-76.0))
        saved_filter = SavedFilter.objects.create(profile=self.profile, name="Tagged Only", criteria={})
        # Sidebar's own "name" search narrows the count further.
        response = self.client.get(self._url(name="Tagged Pin"))
        data = response.json()
        self.assertEqual(data["counts"][str(saved_filter.uuid)], 1)

    def test_count_combines_with_other_active_toolbar_filters(self) -> None:
        filter_a = SavedFilter.objects.create(profile=self.profile, name="A", criteria={"name": "Tagged"})
        filter_b = SavedFilter.objects.create(profile=self.profile, name="B", criteria={})
        # filter_a is already active - filter_b's count should reflect
        # BOTH filters combined (AND), not just filter_b in isolation.
        response = self.client.get(self._url(toolbar_filter_ids=str(filter_a.uuid)))
        data = response.json()
        self.assertEqual(data["counts"][str(filter_b.uuid)], 1)
        # filter_a's own count should NOT be AND-ed against itself.
        self.assertEqual(data["counts"][str(filter_a.uuid)], 1)

    def test_active_filter_excludes_non_matching_pins(self) -> None:
        filter_a = SavedFilter.objects.create(profile=self.profile, name="A", criteria={"name": "Tagged"})
        filter_b = SavedFilter.objects.create(profile=self.profile, name="B", criteria={"name": "Other"})
        # filter_a (Tagged) active + filter_b (Other) candidate -> no pin matches both.
        response = self.client.get(self._url(toolbar_filter_ids=str(filter_a.uuid)))
        data = response.json()
        self.assertEqual(data["counts"][str(filter_b.uuid)], 0)

    def test_malformed_sidebar_criteria_falls_back_to_unfiltered(self) -> None:
        saved_filter = SavedFilter.objects.create(profile=self.profile, name="All", criteria={})
        response = self.client.get(self._url(min_rating="not-a-number"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["counts"][str(saved_filter.uuid)], 2)

    def test_other_profiles_filters_are_not_included(self) -> None:
        other_profile: Profile = baker.make(User).profile
        SavedFilter.objects.create(profile=other_profile, name="Not Mine", criteria={})
        response = self.client.get(self._url())
        self.assertEqual(response.json(), {"counts": {}})
