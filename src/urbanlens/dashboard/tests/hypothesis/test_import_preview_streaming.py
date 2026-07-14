"""Regression tests for UL-150: pins imported with a new "create category" label

must actually be added to that label, not just create it unattached.
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway


class ImportPreviewStreamingLabelAssignmentTests(TestCase):
    """GoogleMapsGateway.import_preview_streaming() attaches labels to imported pins."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _run(self, confirmed_lists: list[dict]) -> list[dict]:
        return list(self.gateway.import_preview_streaming(confirmed_lists, self.profile, auto_tag=False))

    def test_newly_created_category_label_is_attached_to_pin(self) -> None:
        self._run(
            [
                {
                    "stem": "Steel Mills",
                    "create_category": True,
                    "label_ids": [],
                    "pins": [{"name": "Old Steel Mill", "lat": 40.0, "lng": -74.0, "description": ""}],
                },
            ],
        )

        label = Label.objects.get(profile=self.profile, name__iexact="Steel Mills", kind="category")
        self.assertEqual(label.pins.count(), 1)
        self.assertEqual(label.pins.first().name, "Old Steel Mill")

    def test_existing_selected_label_is_still_attached(self) -> None:
        existing_label = baker.make(Label, profile=self.profile, name="Urbex", kind="tag")

        self._run(
            [
                {
                    "stem": "",
                    "create_category": False,
                    "label_ids": [existing_label.pk],
                    "pins": [{"name": "Old Factory", "lat": 41.0, "lng": -75.0, "description": ""}],
                },
            ],
        )

        existing_label.refresh_from_db()
        self.assertEqual(existing_label.pins.count(), 1)
        self.assertEqual(existing_label.pins.first().name, "Old Factory")

    def test_both_new_category_and_existing_label_are_attached_to_same_pin(self) -> None:
        existing_label = baker.make(Label, profile=self.profile, name="Urbex", kind="tag")

        self._run(
            [
                {
                    "stem": "Power Plants",
                    "create_category": True,
                    "label_ids": [existing_label.pk],
                    "pins": [{"name": "Old Power Plant", "lat": 42.0, "lng": -76.0, "description": ""}],
                },
            ],
        )

        category_label = Label.objects.get(profile=self.profile, name__iexact="Power Plants", kind="category")
        pin = category_label.pins.first()
        self.assertIsNotNone(pin)
        self.assertIn(existing_label, pin.labels.all())
        self.assertIn(category_label, pin.labels.all())
