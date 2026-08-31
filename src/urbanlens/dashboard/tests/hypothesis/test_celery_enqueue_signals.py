"""Tests for model signal handlers that enqueue Celery work."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.wiki.signals import suggest_and_add_categories


class _Wiki:
    pk = 20


class PinCreationExternalWorkTests(SimpleTestCase):
    """The old per-pin ``enqueue_location_creation`` signal must stay removed.

    It used to run Location enrichment synchronously off Pin creation. That
    enrichment is lazy now (default boundaries generate on first pin-detail-page
    view), and background Wiki creation is queued by a separate, narrower signal
    (``ensure_wiki_for_pin_location``, covered by ``PinEnsuresWikiSignalTests``
    below) - bulk imports rely on Pin creation itself staying cheap.
    """

    def test_location_creation_signal_removed(self) -> None:
        from urbanlens.dashboard.models.pin import signals as pin_signals

        self.assertFalse(hasattr(pin_signals, "enqueue_location_creation"))


class _Profile:
    def __init__(self, *, community_enabled: bool) -> None:
        self.community_enabled = community_enabled


class _Pin:
    def __init__(self, *, location_id: int | None = 55, community_enabled: bool = True) -> None:
        self.location_id = location_id
        self.profile = _Profile(community_enabled=community_enabled)


class PinEnsuresWikiSignalTests(SimpleTestCase):
    """New pins with a Location queue background Wiki creation.

    ``models.pin.signals.ensure_wiki_for_pin_location`` gates on three
    independent conditions - newly created, has a Location, and the pinning
    profile has community features enabled - any one of which alone must block
    the enqueue.
    """

    def test_enqueues_wiki_creation_after_commit(self) -> None:
        from urbanlens.dashboard.models.pin.signals import ensure_wiki_for_pin_location
        from urbanlens.dashboard.tasks import ensure_wiki_for_location

        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            ensure_wiki_for_pin_location(sender=object, instance=_Pin(location_id=55), created=True)
            callbacks[0]()

        enqueue.assert_called_once_with(ensure_wiki_for_location, 55)

    def test_skips_when_not_newly_created(self) -> None:
        from urbanlens.dashboard.models.pin.signals import ensure_wiki_for_pin_location

        with mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit") as on_commit:
            ensure_wiki_for_pin_location(sender=object, instance=_Pin(), created=False)
        on_commit.assert_not_called()

    def test_skips_pin_without_a_location(self) -> None:
        from urbanlens.dashboard.models.pin.signals import ensure_wiki_for_pin_location

        with mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit") as on_commit:
            ensure_wiki_for_pin_location(sender=object, instance=_Pin(location_id=None), created=True)
        on_commit.assert_not_called()

    def test_skips_when_community_features_disabled(self) -> None:
        from urbanlens.dashboard.models.pin.signals import ensure_wiki_for_pin_location

        with mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit") as on_commit:
            ensure_wiki_for_pin_location(sender=object, instance=_Pin(community_enabled=False), created=True)
        on_commit.assert_not_called()


class WikiCategorySignalTests(SimpleTestCase):
    """New Wikis enqueue category suggestion after commit.

    Category auto-tagging moved from Location to Wiki in the wiki split (see
    urbanlens.dashboard.models.wiki.signals); location.signals is now an
    intentionally-empty stub.
    """

    def test_enqueues_wiki_category_suggestion_after_commit(self) -> None:
        from urbanlens.dashboard.tasks import suggest_wiki_category

        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.wiki.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            suggest_and_add_categories(sender=object, instance=_Wiki(), created=True)
            callbacks[0]()

        enqueue.assert_called_once_with(suggest_wiki_category, _Wiki.pk)

    def test_skips_existing_wiki(self) -> None:
        with mock.patch("urbanlens.dashboard.models.wiki.signals.transaction.on_commit") as on_commit:
            suggest_and_add_categories(sender=object, instance=_Wiki(), created=False)
        on_commit.assert_not_called()
