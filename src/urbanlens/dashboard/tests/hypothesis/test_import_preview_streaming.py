"""Regression tests for UL-150: pins imported with a new "create category" badge

must actually be added to that badge, not just create it unattached.
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway


class ImportPreviewStreamingBadgeAssignmentTests(TestCase):
    """GoogleMapsGateway.import_preview_streaming() attaches badges to imported pins."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _run(self, confirmed_lists: list[dict]) -> list[dict]:
        return list(self.gateway.import_preview_streaming(confirmed_lists, self.profile, auto_tag=False))

    def test_newly_created_category_badge_is_attached_to_pin(self) -> None:
        self._run(
            [
                {
                    "stem": "Steel Mills",
                    "create_category": True,
                    "badge_ids": [],
                    "pins": [{"name": "Old Steel Mill", "lat": 40.0, "lng": -74.0, "description": ""}],
                },
            ],
        )

        badge = Badge.objects.get(profile=self.profile, name__iexact="Steel Mills", kind="category")
        self.assertEqual(badge.pins.count(), 1)
        self.assertEqual(badge.pins.first().name, "Old Steel Mill")

    def test_existing_selected_badge_is_still_attached(self) -> None:
        existing_badge = baker.make(Badge, profile=self.profile, name="Urbex", kind="tag")

        self._run(
            [
                {
                    "stem": "",
                    "create_category": False,
                    "badge_ids": [existing_badge.pk],
                    "pins": [{"name": "Old Factory", "lat": 41.0, "lng": -75.0, "description": ""}],
                },
            ],
        )

        existing_badge.refresh_from_db()
        self.assertEqual(existing_badge.pins.count(), 1)
        self.assertEqual(existing_badge.pins.first().name, "Old Factory")

    def test_both_new_category_and_existing_badge_are_attached_to_same_pin(self) -> None:
        existing_badge = baker.make(Badge, profile=self.profile, name="Urbex", kind="tag")

        self._run(
            [
                {
                    "stem": "Power Plants",
                    "create_category": True,
                    "badge_ids": [existing_badge.pk],
                    "pins": [{"name": "Old Power Plant", "lat": 42.0, "lng": -76.0, "description": ""}],
                },
            ],
        )

        category_badge = Badge.objects.get(profile=self.profile, name__iexact="Power Plants", kind="category")
        pin = category_badge.pins.first()
        self.assertIsNotNone(pin)
        self.assertIn(existing_badge, pin.badges.all())
        self.assertIn(category_badge, pin.badges.all())
