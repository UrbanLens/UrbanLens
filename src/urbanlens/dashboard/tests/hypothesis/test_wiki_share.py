"""Tests for the explicit, user-initiated wiki creation flow.

Wikis are never auto-created: ``WikiShareService.share_from_pin`` is the
single creation entry point, invoked by the Private Pin page's "Create wiki"
button. The user chooses which pin fields, aliases, and photos to seed the
new wiki with; nothing is copied unless explicitly selected, and an existing
wiki is never overwritten with personal data.
"""

from __future__ import annotations

from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatVote
from urbanlens.dashboard.services.wiki.wiki_share import (
    WikiShareService,
    seedable_aliases,
    seedable_field_values,
    seedable_photos,
)


class WikiShareServiceTests(TestCase):
    """share_from_pin seeds only chosen fields/aliases/photos and links the pin."""

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
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            return WikiShareService().share_from_pin(self.pin, include_fields=include, alias_ids=alias_ids, image_ids=image_ids)

    def test_sharing_nothing_contributes_nothing(self) -> None:
        wiki, shared = self._create()

        self.assertFalse(shared)
        self.assertEqual(wiki.location_id, self.location.pk)
        # No personal data copied: name falls back to the location's official name.
        self.assertEqual(wiki.name, "Old Mill")
        self.assertEqual(WikiStatVote.objects.filter(wiki=wiki).count(), 0)

    def test_links_pin_to_new_wiki(self) -> None:
        wiki, _created = self._create()
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.wiki_id, wiki.pk)

    def test_pin_name_is_never_seeded_onto_wiki(self) -> None:
        # "name" isn't a seedable field at all - the wiki's name comes from
        # external place data, and the pin's name already surfaces as an alias.
        wiki, _created = self._create(include={"name"})
        self.assertEqual(wiki.name, "Old Mill")

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
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            new_wiki, created = WikiShareService().share_from_pin(pin2, alias_ids={alt2.pk})
        self.assertTrue(created)
        self.assertTrue(new_wiki.aliases.filter(name="Chosen Alias").exists())

    def test_photos_only_seeded_when_chosen(self) -> None:
        image = baker.make("dashboard.Image", pin=self.pin, upload_processed_at=timezone.now())

        wiki, _created = self._create(image_ids={image.pk})
        image.refresh_from_db()
        self.assertEqual(image.wiki_id, wiki.pk)
        # Still attached to the original pin too.
        self.assertEqual(image.pin_id, self.pin.pk)

    def test_unprocessed_photo_is_processed_before_it_is_shared(self) -> None:
        image = baker.make("dashboard.Image", pin=self.pin, upload_processed_at=None)

        def mark_processed(image_id: int) -> bool:
            self.assertEqual(image_id, image.pk)
            type(image).objects.filter(pk=image_id).update(upload_processed_at=timezone.now())
            return True

        with mock.patch("urbanlens.dashboard.tasks.process_image_upload", side_effect=mark_processed) as process:
            wiki, shared = WikiShareService().share_from_pin(self.pin, image_ids={image.pk})

        image.refresh_from_db()
        self.assertTrue(shared)
        self.assertEqual(image.wiki_id, wiki.pk)
        process.assert_called_once_with(image.pk)

    def test_unprocessed_photo_is_not_shared_when_processing_fails(self) -> None:
        image = baker.make("dashboard.Image", pin=self.pin, upload_processed_at=None)

        with mock.patch("urbanlens.dashboard.tasks.process_image_upload", return_value=False):
            wiki, shared = WikiShareService().share_from_pin(self.pin, image_ids={image.pk})

        image.refresh_from_db()
        self.assertFalse(shared)
        self.assertEqual(wiki.location_id, self.location.pk)
        self.assertIsNone(image.wiki_id)

    def test_sharing_to_an_existing_wiki_contributes_without_renaming_it(self) -> None:
        """The case that used to be impossible.

        Seeding only ran when the click created the page, so sharing to a page
        that already existed did nothing at all. Contributing is now something
        a person does to a page that is already there - but naming stays a
        creation-time act, since renaming a page other people read because
        somebody shared a stat to it is a side effect nobody asked for.
        """
        existing = baker.make("dashboard.Wiki", location=self.location, name="Community Name")

        wiki, shared = self._create(include={"danger"})

        self.assertTrue(shared)
        self.assertEqual(wiki.pk, existing.pk)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Community Name")
        self.assertEqual(WikiStatVote.objects.filter(wiki=wiki, field="danger").count(), 1)

    def test_requires_location(self) -> None:
        self.pin.location_id = None
        with self.assertRaises(ValueError):
            WikiShareService().share_from_pin(self.pin)


class SeedableFieldValuesTests(TestCase):
    """The create-wiki dialog only offers fields that actually have content."""

    def test_lists_populated_fields_only(self) -> None:
        location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        pin = baker.make("dashboard.Pin", location=location, name="Named", danger=0, vulnerability=0)

        fields = {entry["field"] for entry in seedable_field_values(pin)}

        # Name is never offered - see SEEDABLE_FIELDS.
        self.assertNotIn("name", fields)
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
