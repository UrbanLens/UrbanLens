"""Tests for case-insensitive alias uniqueness and the auto-removal tombstone system.

Covers docs/prompts/todo.md's two related asks: (1) alias uniqueness must be
case-insensitive for both manual and automatic creation, and (2) a user
deleting an auto-added alias/link/label/owner must stick - automatic creation
code (external name-provider syncs, AI extraction, keyword/AI auto-tagging)
must not silently recreate it.
"""

from __future__ import annotations

from unittest.mock import patch

from django.db import IntegrityError
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval, WikiAutoRemoval
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.model import PinOwner


class AliasCaseInsensitiveUniquenessTests(TestCase):
    """PinAlias/WikiAlias uniqueness must ignore case."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Current Name", name_is_user_provided=True)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_db_constraint_rejects_case_variant_pin_alias(self) -> None:
        PinAlias.objects.create(pin=self.pin, name="Main Street")
        with self.assertRaises(IntegrityError):
            PinAlias.objects.create(pin=self.pin, name="main street")

    def test_db_constraint_rejects_case_variant_wiki_alias(self) -> None:
        WikiAlias.objects.create(wiki=self.wiki, name="Main Street")
        with self.assertRaises(IntegrityError):
            WikiAlias.objects.create(wiki=self.wiki, name="MAIN STREET")

    def test_add_pin_alias_view_rejects_case_variant(self) -> None:
        PinAlias.objects.create(pin=self.pin, name="Grain Mill")
        response = self.client.post(reverse("pin.aliases", args=[self.pin.slug]), {"name": "grain mill"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.pin.aliases.filter(name__iexact="grain mill").count(), 1)

    def test_add_wiki_alias_view_rejects_case_variant(self) -> None:
        WikiAlias.objects.create(wiki=self.wiki, name="Grain Mill")
        response = self.client.post(reverse("location.wiki.aliases", args=[self.location.slug]), {"name": "GRAIN MILL"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wiki.aliases.filter(name__iexact="grain mill").count(), 1)

    def test_different_names_still_allowed(self) -> None:
        response = self.client.post(reverse("pin.aliases", args=[self.pin.slug]), {"name": "Totally Different"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.pin.aliases.filter(name="Totally Different").exists())


class AliasDeletionTombstoneTests(TestCase):
    """Deleting an alias must record a tombstone and prevent auto-recreation."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Curated Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def _candidates(self, name: str):
        from urbanlens.dashboard.services.locations.name_resolution import NameCandidate

        return [NameCandidate(name=name, source="nominatim")]

    def test_deleting_pin_alias_records_tombstone(self) -> None:
        alias = PinAlias.objects.create(pin=self.pin, name="External Name")
        response = self.client.delete(reverse("pin.alias.delete", args=[self.pin.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PinAutoRemoval.objects.was_removed(pin=self.pin, kind=AutoRemovalKind.ALIAS, value="External Name"))

    def test_deleted_pin_alias_is_not_recreated_by_backfill(self) -> None:
        from urbanlens.dashboard.services.locations.naming import persist_official_aliases_for_location

        alias = PinAlias.objects.create(pin=self.pin, name="External Name")
        self.client.delete(reverse("pin.alias.delete", args=[self.pin.slug, alias.id]))

        with patch("urbanlens.dashboard.services.locations.naming.external_name_candidates_for_location", return_value=self._candidates("External Name")):
            persist_official_aliases_for_location(self.location)

        self.assertFalse(self.pin.aliases.filter(name__iexact="External Name").exists())

    def test_deleted_wiki_alias_is_not_recreated_by_backfill_even_with_different_case(self) -> None:
        from urbanlens.dashboard.services.locations.naming import persist_official_aliases_for_location

        alias = WikiAlias.objects.create(wiki=self.wiki, name="External Name")
        self.client.delete(reverse("location.wiki.alias.delete", args=[self.location.slug, alias.id]))

        with patch("urbanlens.dashboard.services.locations.naming.external_name_candidates_for_location", return_value=self._candidates("EXTERNAL NAME")):
            persist_official_aliases_for_location(self.location)

        self.assertFalse(self.wiki.aliases.filter(name__iexact="External Name").exists())

    def test_user_can_still_manually_recreate_a_deleted_alias(self) -> None:
        """The tombstone only suppresses automation - the user's own deliberate re-add always works."""
        alias = PinAlias.objects.create(pin=self.pin, name="External Name")
        self.client.delete(reverse("pin.alias.delete", args=[self.pin.slug, alias.id]))

        response = self.client.post(reverse("pin.aliases", args=[self.pin.slug]), {"name": "External Name"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.pin.aliases.filter(name="External Name").exists())


class LinkDeletionTombstoneTests(TestCase):
    """Deleting a link must record a tombstone and prevent plugin auto-recreation."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Curated Mill")
        self.client.force_login(self.user)

    def test_deleting_pin_link_records_tombstone(self) -> None:
        link = PinLink.objects.create(pin=self.pin, name="OpenStreetMap", url="https://osm.org/way/123")
        response = self.client.delete(reverse("pin.link.delete", args=[self.pin.slug, link.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PinAutoRemoval.objects.was_removed(pin=self.pin, kind=AutoRemovalKind.LINK, value="https://osm.org/way/123"))

    def test_deleted_link_is_not_recreated_by_nominatim_auto_add(self) -> None:
        from urbanlens.dashboard.plugins.builtin.nominatim import NominatimPanelSource

        link = PinLink.objects.create(pin=self.pin, name="OpenStreetMap", url="https://osm.org/way/123")
        self.client.delete(reverse("pin.link.delete", args=[self.pin.slug, link.id]))

        NominatimPanelSource._add_osm_link(self.pin, self.location, "https://osm.org/way/123")

        self.assertFalse(self.pin.links.filter(url="https://osm.org/way/123").exists())
        # The wiki side is a separate tombstone scope - unaffected by the pin-side deletion.
        self.assertTrue(self.wiki.links.filter(url="https://osm.org/way/123").exists())

    def test_deleting_wiki_link_prevents_epa_auto_readd(self) -> None:
        from urbanlens.dashboard.plugins.builtin.epa_echo import EpaEchoDetailPanelSource

        link = WikiLink.objects.create(wiki=self.wiki, name="EPA Compliance Report", url="https://echo.epa.gov/detailed-facility-report?fid=123")
        response = self.client.delete(reverse("location.wiki.link.delete", args=[self.location.slug, link.id]))
        self.assertEqual(response.status_code, 200)

        EpaEchoDetailPanelSource._add_echo_report_link(self.pin, self.location, "123")

        self.assertFalse(self.wiki.links.filter(url="https://echo.epa.gov/detailed-facility-report?fid=123").exists())


class OwnerDeletionTombstoneTests(TestCase):
    """Deleting a PinOwner must record a tombstone and prevent AI-extraction auto-recreation."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Owned Building")
        self.client.force_login(self.user)

    def test_deleting_owner_records_tombstone(self) -> None:
        owner = PinOwner.objects.create(pin=self.pin, name="Jane Landlord")
        response = self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PinAutoRemoval.objects.was_removed(pin=self.pin, kind=AutoRemovalKind.OWNER, value="Jane Landlord"))

    def test_deleted_owner_is_not_recreated_by_ai_extraction(self) -> None:
        from urbanlens.dashboard.services.ai.link_extraction import _apply_owner_name

        owner = PinOwner.objects.create(pin=self.pin, name="Jane Landlord")
        self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))

        applied, message = _apply_owner_name(self.pin, "Jane Landlord", {})

        self.assertFalse(applied)
        self.assertEqual(message, "Skipped - this owner was previously removed.")
        self.assertFalse(self.pin.owners.filter(name="Jane Landlord").exists())


class LabelDeletionTombstoneTests(TestCase):
    """Removing a label from a pin/wiki must record a tombstone and stop auto-tagging from reattaching it."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Factory")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Old Factory")
        self.label = baker.make(Label, kind=KIND_CATEGORY, name="Factory", profile=None)
        self.client.force_login(self.user)

    def test_removing_pin_label_records_tombstone(self) -> None:
        self.pin.labels.add(self.label)
        response = self.client.post(
            reverse("label.pin", kwargs={"label_kind": "categories", "pin_slug": self.pin.slug}),
            data={"label_id": self.label.id, "action": "remove"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.pin.labels.filter(pk=self.label.pk).exists())
        self.assertTrue(PinAutoRemoval.objects.was_removed(pin=self.pin, kind=AutoRemovalKind.LABEL, value=str(self.label.pk)))

    def test_auto_tag_does_not_reattach_a_removed_label(self) -> None:
        from urbanlens.dashboard.services.auto_tag import AutoTagService

        self.pin.labels.add(self.label)
        self.client.post(
            reverse("label.pin", kwargs={"label_kind": "categories", "pin_slug": self.pin.slug}),
            data={"label_id": self.label.id, "action": "remove"},
        )

        with patch.object(AutoTagService, "_match", return_value=[self.label]):
            AutoTagService(kinds=["category"]).suggest_for_pin(self.pin, apply=True)

        self.assertFalse(self.pin.labels.filter(pk=self.label.pk).exists())

    def test_removing_wiki_label_records_tombstone_and_blocks_reattach(self) -> None:
        from urbanlens.dashboard.services.auto_tag import AutoTagService

        self.wiki.labels.add(self.label)
        response = self.client.post(
            reverse("label.location", kwargs={"label_kind": "categories", "location_slug": self.location.slug}),
            data={"label_id": self.label.id, "action": "remove"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(WikiAutoRemoval.objects.was_removed(wiki=self.wiki, kind=AutoRemovalKind.LABEL, value=str(self.label.pk)))

        with patch.object(AutoTagService, "_match", return_value=[self.label]):
            AutoTagService(kinds=["category"]).suggest_for_wiki(self.wiki, apply=True)

        self.assertFalse(self.wiki.labels.filter(pk=self.label.pk).exists())


class ExternalLinksHelperTests(TestCase):
    """services.locations.external_links - the shared add_pin_link/add_wiki_link/
    add_pin_and_wiki_link primitive used by Nominatim, EPA ECHO, and Wikipedia."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.500000", longitude="-73.500000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Curated Mill")

    def test_add_pin_link_creates_a_new_link(self) -> None:
        from urbanlens.dashboard.services.locations.external_links import add_pin_link

        created = add_pin_link(self.pin, "https://example.test/a", "Example")
        self.assertTrue(created)
        self.assertTrue(self.pin.links.filter(url="https://example.test/a", name="Example").exists())

    def test_add_pin_link_does_not_duplicate(self) -> None:
        from urbanlens.dashboard.services.locations.external_links import add_pin_link

        add_pin_link(self.pin, "https://example.test/a", "Example")
        created_again = add_pin_link(self.pin, "https://example.test/a", "Example")
        self.assertFalse(created_again)
        self.assertEqual(self.pin.links.filter(url="https://example.test/a").count(), 1)

    def test_add_pin_link_respects_tombstone(self) -> None:
        from urbanlens.dashboard.services.locations.external_links import add_pin_link

        PinAutoRemoval.objects.record(pin=self.pin, kind=AutoRemovalKind.LINK, value="https://example.test/a")
        created = add_pin_link(self.pin, "https://example.test/a", "Example")
        self.assertFalse(created)
        self.assertFalse(self.pin.links.filter(url="https://example.test/a").exists())

    def test_add_wiki_link_respects_tombstone(self) -> None:
        from urbanlens.dashboard.services.locations.external_links import add_wiki_link

        WikiAutoRemoval.objects.record(wiki=self.wiki, kind=AutoRemovalKind.LINK, value="https://example.test/a")
        created = add_wiki_link(self.wiki, "https://example.test/a", "Example")
        self.assertFalse(created)

    def test_add_pin_and_wiki_link_adds_to_both(self) -> None:
        from urbanlens.dashboard.services.locations.external_links import add_pin_and_wiki_link

        add_pin_and_wiki_link(self.pin, self.location, "https://example.test/a", "Example")
        self.assertTrue(self.pin.links.filter(url="https://example.test/a").exists())
        self.assertTrue(self.wiki.links.filter(url="https://example.test/a").exists())

    def test_add_pin_and_wiki_link_with_no_wiki_only_adds_pin_link(self) -> None:
        from urbanlens.dashboard.services.locations.external_links import add_pin_and_wiki_link

        location_no_wiki = baker.make(Location, latitude="41.510000", longitude="-73.510000")
        pin_no_wiki = baker.make(Pin, profile=self.profile, location=location_no_wiki)

        add_pin_and_wiki_link(pin_no_wiki, location_no_wiki, "https://example.test/b", "Example")

        self.assertTrue(pin_no_wiki.links.filter(url="https://example.test/b").exists())
