"""Tests for model signal handlers that enqueue Celery work."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.signals import suggest_and_add_categories
from urbanlens.dashboard.models.pin.signals import enqueue_location_creation


class _Pin:
    pk = 10
    # A saved Pin always references a Location (RESTRICT, non-null FK) now
    # that Location/Wiki have been split - location_id=20 represents that.
    location_id = 20
    is_private = False
    parent_pin_id = None
    effective_latitude = 40.0
    effective_longitude = -74.0


class _Wiki:
    pk = 20


class PinLocationCreationSignalTests(TestCase):
    """New public root pins enqueue background wiki/campus-boundary creation for their Location."""

    def test_enqueues_after_commit_for_eligible_new_pin(self) -> None:
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
        ):
            enqueue_location_creation(sender=object, instance=_Pin(), created=True)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], _Pin.pk)

    def test_skips_pin_without_location(self) -> None:
        pin = _Pin()
        pin.location_id = None
        with mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit") as on_commit:
            enqueue_location_creation(sender=object, instance=pin, created=True)
        on_commit.assert_not_called()

    def test_skips_existing_or_ineligible_pin(self) -> None:
        pin = _Pin()
        pin.is_private = True
        with mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit") as on_commit:
            enqueue_location_creation(sender=object, instance=pin, created=True)
        on_commit.assert_not_called()


class WikiCategorySignalTests(TestCase):
    """New Wikis enqueue category suggestion after commit.

    Category auto-tagging moved from Location to Wiki in the wiki split (see
    urbanlens.dashboard.models.wiki.signals); location.signals is now an
    intentionally-empty stub.
    """

    def test_enqueues_wiki_category_suggestion_after_commit(self) -> None:
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.wiki.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
        ):
            suggest_and_add_categories(sender=object, instance=_Wiki(), created=True)
            callbacks[0]()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], _Wiki.pk)

    def test_skips_existing_wiki(self) -> None:
        with mock.patch("urbanlens.dashboard.models.wiki.signals.transaction.on_commit") as on_commit:
            suggest_and_add_categories(sender=object, instance=_Wiki(), created=False)
        on_commit.assert_not_called()
