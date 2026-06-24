"""Regression tests for inline pin editing."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.pin.model import Pin


class PinEditCategoryUpdateTests(TestCase):
	"""PinEditView must only replace categories when categories are submitted."""

	def setUp(self) -> None:
		"""Create an authenticated user with one categorized pin."""
		super().setUp()
		self.user = baker.make(User)
		self.profile = self.user.profile
		self.client.force_login(self.user)
		self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
		self.pin = baker.make(Pin, profile=self.profile, location=self.location, priority=1)
		self.category = baker.make(Badge, name="Factory", kind="category", profile=self.profile)
		self.pin.categories.add(self.category)

	def test_partial_priority_update_preserves_existing_categories(self) -> None:
		"""Star-only HTMX updates must not clear pin category assignments."""
		response = self.client.post(
			reverse("pin.edit", kwargs={"pin_uuid": self.pin.uuid}),
			data=json.dumps({"priority": 4}),
			content_type="application/json",
		)

		self.assertEqual(response.status_code, 200)
		self.pin.refresh_from_db()
		self.assertEqual(self.pin.priority, 4)
		self.assertIn(self.category, self.pin.categories.all())

	def test_explicit_category_update_uses_owner_categories(self) -> None:
		"""Submitted category names should resolve to the pin owner's badges."""
		other_user = baker.make(User)
		other_category = baker.make(Badge, name="Factory", kind="category", profile=other_user.profile)

		response = self.client.post(
			reverse("pin.edit", kwargs={"pin_uuid": self.pin.uuid}),
			data=json.dumps({"categories": "factory"}),
			content_type="application/json",
		)

		self.assertEqual(response.status_code, 200)
		self.pin.refresh_from_db()
		self.assertIn(self.category, self.pin.categories.all())
		self.assertNotIn(other_category, self.pin.categories.all())
		self.assertFalse(Badge.objects.filter(name__iexact="factory", kind="category", profile__isnull=True).exists())
