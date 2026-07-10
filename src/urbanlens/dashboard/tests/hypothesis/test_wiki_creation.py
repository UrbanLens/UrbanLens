"""Tests for the explicit, user-initiated wiki creation flow.

Wikis are never auto-created: ``WikiCreationService.create_for_pin`` is the
single creation entry point, invoked by the pin detail page's "Create wiki"
button. The user chooses which pin fields to seed the new wiki with; nothing
is copied unless explicitly selected, and an existing wiki is never
overwritten with personal data.
"""

from __future__ import annotations

import datetime
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.creation import WikiCreationService, seedable_field_values


class WikiCreationServiceTests(TestCase):
    """create_for_pin seeds only chosen fields and links the pin."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000", official_name="Old Mill")
        self.pin = baker.make(
            "dashboard.Pin",
            location=self.location,
            name="My secret mill",
            description="Personal notes about access",
            date_abandoned=datetime.date(1999, 5, 1),
            fences="some",
        )

    def _create(self, include: set[str] | None = None) -> tuple[Wiki, bool]:
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            return WikiCreationService().create_for_pin(self.pin, include_fields=include)

    def test_creates_wiki_without_seeding_by_default(self) -> None:
        wiki, created = self._create()

        self.assertTrue(created)
        self.assertEqual(wiki.location_id, self.location.pk)
        # No personal data copied: name falls back to the location's official name.
        self.assertEqual(wiki.name, "Old Mill")
        self.assertFalse(wiki.description)
        self.assertIsNone(wiki.date_abandoned)
        self.assertEqual(wiki.fences, "unknown")

    def test_links_pin_to_new_wiki(self) -> None:
        wiki, _created = self._create()
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.wiki_id, wiki.pk)

    def test_seeds_only_selected_fields(self) -> None:
        wiki, created = self._create(include={"description", "date_abandoned", "security"})

        self.assertTrue(created)
        self.assertEqual(wiki.description, "Personal notes about access")
        self.assertEqual(wiki.date_abandoned, datetime.date(1999, 5, 1))
        self.assertEqual(wiki.fences, "some")
        # name was not selected - personal pin name must not leak.
        self.assertEqual(wiki.name, "Old Mill")

    def test_seeding_pin_name_requires_explicit_choice(self) -> None:
        wiki, _created = self._create(include={"name"})
        self.assertEqual(wiki.name, "My secret mill")

    def test_existing_wiki_is_never_overwritten(self) -> None:
        existing = baker.make("dashboard.Wiki", location=self.location, name="Community Name", description="Community text")

        wiki, created = self._create(include={"name", "description"})

        self.assertFalse(created)
        self.assertEqual(wiki.pk, existing.pk)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Community Name")
        self.assertEqual(wiki.description, "Community text")

    def test_requires_location(self) -> None:
        self.pin.location_id = None
        with self.assertRaises(ValueError):
            WikiCreationService().create_for_pin(self.pin)


class SeedableFieldValuesTests(TestCase):
    """The create-wiki dialog only offers fields that actually have content."""

    def test_lists_populated_fields_only(self) -> None:
        location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        pin = baker.make("dashboard.Pin", location=location, name="Named", description="")

        fields = {entry["field"] for entry in seedable_field_values(pin)}

        self.assertIn("name", fields)
        self.assertNotIn("description", fields)
        self.assertNotIn("security", fields)

    def test_security_group_offered_when_any_field_known(self) -> None:
        location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        pin = baker.make("dashboard.Pin", location=location, cameras="some")

        fields = {entry["field"] for entry in seedable_field_values(pin)}
        self.assertIn("security", fields)
