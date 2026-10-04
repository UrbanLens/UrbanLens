"""P210: stored pin-share notifications stop naming the sender's own pin.

Until P193 the recipient's notification read "{sender} shared {the sender's pin name} with you.", a name the share
never consented to pass on, and rows written then still say it.
"""

from __future__ import annotations

import importlib

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.wiki.model import Wiki

migration = importlib.import_module("urbanlens.dashboard.migrations.0075_pin_share_notification_labels")

_HISTORICAL_APPS = None


def _historical_apps():
    """The models as the migration sees them under ``migrate``: fields only."""
    global _HISTORICAL_APPS  # noqa: PLW0603 - rendering the state takes seconds; one per test run
    if _HISTORICAL_APPS is None:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        node = ("dashboard", migration.Migration.dependencies[0][1])
        _HISTORICAL_APPS = MigrationExecutor(connection).loader.project_state(node).apps
    return _HISTORICAL_APPS


class PinShareNotificationLabelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.sender = baker.make(User).profile
        self.recipient = baker.make(User).profile

    def _share(self, message: str, *, shared_name: str | None = None, **location_fields) -> NotificationLog:
        location = baker.make(Location, latitude="42.65", longitude="-73.75", **location_fields)
        pin = baker.make(Pin, profile=self.sender, location=location, name="My Secret Spot")
        notification = NotificationLog.objects.create(
            profile=self.recipient,
            notification_type=NotificationType.PIN_SHARED,
            title="Pin shared with you",
            message=message,
        )
        baker.make(
            PinShare,
            pin=pin,
            location=location,
            from_profile=self.sender,
            to_profile=self.recipient,
            shared_name=shared_name,
            notification=notification,
            parent_share=None,
        )
        return notification

    def _run(self) -> None:
        migration.relabel_pin_share_notifications(_historical_apps(), None)

    def test_the_senders_pin_name_gives_way_to_the_places_official_name(self) -> None:
        notification = self._share(
            "Ada shared My Secret Spot with you. It comes with 2 child pins.", official_name="Powerhouse"
        )

        self._run()

        notification.refresh_from_db()
        self.assertEqual(notification.message, "Ada shared Powerhouse with you. It comes with 2 child pins.")

    def test_a_name_the_sender_chose_to_share_is_kept(self) -> None:
        notification = self._share(
            "Ada shared My Secret Spot with you.", shared_name="Old mill", official_name="Powerhouse"
        )

        self._run()

        notification.refresh_from_db()
        self.assertEqual(notification.message, "Ada shared Old mill with you.")

    def test_the_wiki_name_comes_before_the_official_one(self) -> None:
        notification = self._share("Ada shared My Secret Spot with you.", official_name="Powerhouse")
        Wiki.objects.create(location=PinShare.objects.get(notification=notification).location, name="Building 33")

        self._run()

        notification.refresh_from_db()
        self.assertEqual(notification.message, "Ada shared Building 33 with you.")

    def test_an_unnamed_place_reads_by_its_area(self) -> None:
        notification = self._share(
            "Ada shared My Secret Spot with you. You already have this location pinned.",
            official_name="",
            locality="Albany",
            administrative_area_level_1="NY",
            country="",
        )

        self._run()

        notification.refresh_from_db()
        self.assertEqual(
            notification.message,
            "Ada shared Unnamed Location in Albany, NY with you. You already have this location pinned.",
        )

    def test_a_masked_sender_stays_masked(self) -> None:
        notification = self._share("Member 2 shared My Secret Spot with you.", official_name="Powerhouse")

        self._run()

        notification.refresh_from_db()
        self.assertEqual(notification.message, "Member 2 shared Powerhouse with you.")

    def test_a_message_in_another_shape_is_left_alone(self) -> None:
        notification = self._share("Something else entirely", official_name="Powerhouse")

        self._run()

        notification.refresh_from_db()
        self.assertEqual(notification.message, "Something else entirely")
