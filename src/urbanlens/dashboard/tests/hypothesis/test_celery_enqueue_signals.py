"""Tests for model signal handlers that enqueue Celery work."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.signals import suggest_and_add_categories


class _Wiki:
    pk = 20


class PinCreationExternalWorkTests(TestCase):
    """Pin creation triggers no wiki/boundary/external-API work.

    Wikis are user-created from the pin detail page and default boundaries are
    generated lazily on first view, so the old ``enqueue_location_creation``
    post_save signal must stay gone - bulk imports rely on this.
    """

    def test_location_creation_signal_removed(self) -> None:
        from urbanlens.dashboard.models.pin import signals as pin_signals

        self.assertFalse(hasattr(pin_signals, "enqueue_location_creation"))


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
