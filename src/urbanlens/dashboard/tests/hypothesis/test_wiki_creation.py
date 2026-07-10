"""Tests for the explicit, user-initiated wiki creation flow.

Wikis are never auto-created: ``WikiCreationService.create_for_pin`` is the
single creation entry point, invoked by the pin detail page's "Create wiki"
button. The user chooses which pin fields, aliases, and photos to seed the
new wiki with; nothing is copied unless explicitly selected, and an existing
wiki is never overwritten with personal data.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatVote
from urbanlens.dashboard.services.locations.creation import (
    WikiCreationService,
    seedable_aliases,
    seedable_field_values,
    seedable_photos,
)


class WikiCreationServiceTests(TestCase):
    """create_for_pin seeds only chosen fields/aliases/photos and links the pin."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000", official_name="Old Mill")
        self.pin = baker.make(
            "dashboard.Pin",
            location=self.location,
            name="My secret mill",
            danger=4,
            vulnerability=2,
        )

    def _create(self, *, include: set[str] | None = None, alias_ids: set[int] | None = None, image_ids: set[int] | None = None) -> tuple[Wiki, bool]:
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            return WikiCreationService().create_for_pin(self.pin, include_fields=include, alias_ids=alias_ids, image_ids=image_ids)

    def test_creates_wiki_without_seeding_by_default(self) -> None:
        wiki, created = self._create()

        self.assertTrue(created)
        self.assertEqual(wiki.location_id, self.location.pk)
        # No personal data copied: name falls back to the location's official name.
        self.assertEqual(wiki.name, "Old Mill")
        self.assertEqual(WikiStatVote.objects.filter(wiki=wiki).count(), 0)

    def test_links_pin_to_new_wiki(self) -> None:
        wiki, _created = self._create()
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.wiki_id, wiki.pk)

    def test_seeding_pin_name_requires_explicit_choice(self) -> None:
        wiki, _created = self._create(include={"name"})
        self.assertEqual(wiki.name, "My secret mill")

    def test_seeds_danger_and_vulnerability_as_initial_votes(self) -> None:
        wiki, _created = self._create(include={"danger", "vulnerability"})

        danger_vote = WikiStatVote.objects.get(wiki=wiki, profile=self.pin.profile, field="danger")
        vulnerability_vote = WikiStatVote.objects.get(wiki=wiki, profile=self.pin.profile, field="vulnerability")
        self.assertEqual(danger_vote.value, 4)
        self.assertEqual(vulnerability_vote.value, 2)

    def test_unselected_stat_fields_are_not_seeded(self) -> None:
        wiki, _created = self._create(include={"danger"})
        self.assertFalse(WikiStatVote.objects.filter(wiki=wiki, field="vulnerability").exists())

    def test_official_alias_is_always_seeded(self) -> None:
        official = baker.make("dashboard.PinAlias", pin=self.pin, name="Historic Old Mill", kind=AliasType.OFFICIAL)

        wiki, _created = self._create()

        self.assertTrue(wiki.aliases.filter(name=official.name).exists())

    def test_alternate_alias_only_seeded_when_chosen(self) -> None:
        alternate = baker.make("dashboard.PinAlias", pin=self.pin, name="The Mill", kind=AliasType.ALTERNATE)

        wiki, _created = self._create(alias_ids=set())
        self.assertFalse(wiki.aliases.filter(name=alternate.name).exists())

        pin2 = baker.make("dashboard.Pin", location=baker.make("dashboard.Location", latitude="41", longitude="-75"))
        alt2 = baker.make("dashboard.PinAlias", pin=pin2, name="Chosen Alias", kind=AliasType.ALTERNATE)
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            new_wiki, created = WikiCreationService().create_for_pin(pin2, alias_ids={alt2.pk})
        self.assertTrue(created)
        self.assertTrue(new_wiki.aliases.filter(name="Chosen Alias").exists())

    def test_photos_only_seeded_when_chosen(self) -> None:
        image = baker.make("dashboard.Image", pin=self.pin)

        wiki, _created = self._create(image_ids={image.pk})
        image.refresh_from_db()
        self.assertEqual(image.wiki_id, wiki.pk)
        # Still attached to the original pin too.
        self.assertEqual(image.pin_id, self.pin.pk)

    def test_existing_wiki_is_never_overwritten(self) -> None:
        existing = baker.make("dashboard.Wiki", location=self.location, name="Community Name")

        wiki, created = self._create(include={"name", "danger"})

        self.assertFalse(created)
        self.assertEqual(wiki.pk, existing.pk)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Community Name")
        self.assertEqual(WikiStatVote.objects.filter(wiki=wiki).count(), 0)

    def test_requires_location(self) -> None:
        self.pin.location_id = None
        with self.assertRaises(ValueError):
            WikiCreationService().create_for_pin(self.pin)


class SeedableFieldValuesTests(TestCase):
    """The create-wiki dialog only offers fields that actually have content."""

    def test_lists_populated_fields_only(self) -> None:
        location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        pin = baker.make("dashboard.Pin", location=location, name="Named", danger=0, vulnerability=0)

        fields = {entry["field"] for entry in seedable_field_values(pin)}

        self.assertIn("name", fields)
        self.assertNotIn("danger", fields)
        self.assertNotIn("vulnerability", fields)

    def test_danger_and_vulnerability_offered_when_set(self) -> None:
        location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        pin = baker.make("dashboard.Pin", location=location, name="", danger=3, vulnerability=5)

        fields = {entry["field"] for entry in seedable_field_values(pin)}
        self.assertIn("danger", fields)
        self.assertIn("vulnerability", fields)


class SeedableAliasesAndPhotosTests(TestCase):
    """The create-wiki dialog's per-item alias/photo pickers list everything on the pin."""

    def setUp(self):
        self.location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make("dashboard.Pin", location=self.location)

    def test_seedable_aliases_includes_official_and_alternate(self) -> None:
        baker.make("dashboard.PinAlias", pin=self.pin, name="Official Name", kind=AliasType.OFFICIAL)
        baker.make("dashboard.PinAlias", pin=self.pin, name="Nickname", kind=AliasType.NICKNAME)

        names = {alias.name for alias in seedable_aliases(self.pin)}
        self.assertEqual(names, {"Official Name", "Nickname"})

    def test_seedable_photos_lists_pin_images(self) -> None:
        baker.make("dashboard.Image", pin=self.pin, _quantity=2)

        self.assertEqual(len(seedable_photos(self.pin)), 2)

    def test_seedable_photos_empty_when_none(self) -> None:
        self.assertEqual(seedable_photos(self.pin), [])
