"""Tests for model signal handlers that enqueue Celery work."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.signals import suggest_and_add_categories
from urbanlens.dashboard.models.pin.signals import enqueue_location_creation


class _Pin:
    pk = 10
    location_id = None
    is_private = False
    parent_pin_id = None
    parent_wiki_id = None
    effective_latitude = 40.0
    effective_longitude = -74.0


class _Location:
    pk = 20


class PinLocationCreationSignalTests(TestCase):
    """New public root pins with coordinates enqueue background Location creation."""

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

    def test_skips_existing_or_ineligible_pin(self) -> None:
        pin = _Pin()
        pin.is_private = True
        with mock.patch("urbanlens.dashboard.models.pin.signals.transaction.on_commit") as on_commit:
            enqueue_location_creation(sender=object, instance=pin, created=True)
        on_commit.assert_not_called()


class LocationCategorySignalTests(TestCase):
    """New Locations enqueue category suggestion after commit."""

    def test_enqueues_location_category_suggestion_after_commit(self) -> None:
        callbacks = []
        with (
            mock.patch("urbanlens.dashboard.models.location.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue,
        ):
            suggest_and_add_categories(sender=object, instance=_Location(), created=True)
            callbacks[0]()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], _Location.pk)

    def test_skips_existing_location(self) -> None:
        with mock.patch("urbanlens.dashboard.models.location.signals.transaction.on_commit") as on_commit:
            suggest_and_add_categories(sender=object, instance=_Location(), created=False)
        on_commit.assert_not_called()
