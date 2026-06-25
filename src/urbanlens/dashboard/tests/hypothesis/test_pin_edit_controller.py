"""Tests for PinEditView category update logic."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_edit import PinEditView
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin


class PinEditCategoryUpdateTests(TestCase):
    """Regression tests for partial updates and category scoping."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)
        self.existing_cat = baker.make(
            Badge, name="existing", kind="category", profile=self.profile,
        )
        self.pin.categories.add(self.existing_cat)

    def _post(self, body: dict) -> object:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        with patch("urbanlens.dashboard.controllers.pin_edit._ensure_location_address"):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def test_partial_priority_update_preserves_existing_categories(self) -> None:
        """Submitting only priority must not clear the pin's categories."""
        response = self._post({"priority": 3})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        category_ids = list(self.pin.categories.values_list("id", flat=True))
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
        cats = list(self.pin.categories.all())
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0].name, new_cat_name)
        # Must be owned by the pin's profile, not global
        self.assertEqual(cats[0].profile_id, self.profile.pk)

    def test_explicit_empty_categories_clears_all(self) -> None:
        """Submitting an empty categories string explicitly clears all categories."""
        response = self._post({"categories": ""})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.categories.count(), 0)

    def test_duplicate_category_names_are_deduplicated(self) -> None:
        """Comma-separated list with duplicates should not create two badges."""
        response = self._post({"categories": "nature,nature,Nature"})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.categories.count(), 1)
