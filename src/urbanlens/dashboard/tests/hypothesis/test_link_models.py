"""Tests for the PinLink/WikiLink models and their post_save Wayback-archive signals."""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.links.signals import archive_pin_link, archive_wiki_link
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class PinLinkDisplayNameTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.profile)

    def test_uses_given_name_when_present(self) -> None:
        link = baker.make(PinLink, pin=self.pin, name="Poughkeepsie Journal", url="https://example.com/a")
        self.assertEqual(link.display_name, "Poughkeepsie Journal")

    def test_falls_back_to_domain_when_name_blank(self) -> None:
        link = baker.make(PinLink, pin=self.pin, name="", url="https://example.com/a/b?c=1")
        self.assertEqual(link.display_name, "example.com")

    def test_str_returns_display_name(self) -> None:
        link = baker.make(PinLink, pin=self.pin, name="", url="https://example.com/a")
        self.assertEqual(str(link), "example.com")


class LinkNameSanitizeTests(TestCase):
    """name is sanitized on save, mirroring _AliasBase - single enforcement point."""

    def setUp(self) -> None:
        self.profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.profile)

    def test_markup_characters_are_stripped_from_name(self) -> None:
        link = PinLink.objects.create(pin=self.pin, name="<script>alert(1)</script>", url="https://example.com")
        self.assertNotIn("<", link.name)
        self.assertNotIn(">", link.name)


class LinkNeedsArchivingQuerySetTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.profile)

    def test_excludes_links_that_already_have_a_wayback_url(self) -> None:
        archived = baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url="https://web.archive.org/x")
        unarchived = baker.make(PinLink, pin=self.pin, url="https://example.com/b", wayback_url="")
        pks = set(PinLink.objects.needs_archiving().values_list("pk", flat=True))
        self.assertNotIn(archived.pk, pks)
        self.assertIn(unarchived.pk, pks)


class _FakeInstance:
    def __init__(self, pk: int, wayback_url: str = "") -> None:
        self.pk = pk
        self.wayback_url = wayback_url


class PinLinkArchiveSignalTests(TestCase):
    """A freshly created PinLink enqueues the Wayback archive task after commit."""

    def test_enqueues_after_commit_for_new_link(self) -> None:
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.links.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
        ):
            archive_pin_link(sender=object, instance=_FakeInstance(pk=7), created=True)
            callbacks[0]()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], "PinLink")
        self.assertEqual(enqueue.call_args.args[2], 7)

    def test_skips_when_not_created(self) -> None:
        with mock.patch("urbanlens.dashboard.models.links.signals.transaction.on_commit") as on_commit:
            archive_pin_link(sender=object, instance=_FakeInstance(pk=7), created=False)
        on_commit.assert_not_called()

    def test_skips_when_wayback_url_already_set(self) -> None:
        """E.g. restored via Undo History, which may carry the wayback_url along."""
        with mock.patch("urbanlens.dashboard.models.links.signals.transaction.on_commit") as on_commit:
            archive_pin_link(sender=object, instance=_FakeInstance(pk=7, wayback_url="https://web.archive.org/x"), created=True)
        on_commit.assert_not_called()


class WikiLinkArchiveSignalTests(TestCase):
    def test_enqueues_after_commit_for_new_link(self) -> None:
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.links.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
        ):
            archive_wiki_link(sender=object, instance=_FakeInstance(pk=9), created=True)
            callbacks[0]()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], "WikiLink")
        self.assertEqual(enqueue.call_args.args[2], 9)

    def test_skips_when_not_created(self) -> None:
        with mock.patch("urbanlens.dashboard.models.links.signals.transaction.on_commit") as on_commit:
            archive_wiki_link(sender=object, instance=_FakeInstance(pk=9), created=False)
        on_commit.assert_not_called()


class WikiLinkModelTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location, latitude="41.5", longitude="-73.5")
        self.wiki = baker.make("dashboard.Wiki", location=self.location)

    def test_created_by_defaults_to_none(self) -> None:
        link = baker.make(WikiLink, wiki=self.wiki, url="https://example.com")
        self.assertIsNone(link.created_by_id)
