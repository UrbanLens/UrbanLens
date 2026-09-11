"""Bulk writes skip ``post_save``, so the work those receivers do must be done by hand.

``bulk_update``/``bulk_create`` issue raw SQL and never call ``save()``, so no
``post_save`` fires. Where a receiver maintains derived state, every bulk path has to
reproduce it or that state silently rots - which is exactly what had happened here.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarSyncDirection,
    GoogleCalendarAccount,
    TripCalendarLink,
)
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.pins.pin_list_trip import copy_list_pins_to_trip


class LabelBulkUpdateTouchesCarryingPinsTests(TestCase):
    """A label's order decides what its pins draw, and reordering is a ``bulk_update``.

    ``Pin.icon_source_label`` sorts by ``-label.order``, so reordering labels changes
    which one supplies a pin's icon and colour. ``bulk_update`` fires no ``post_save``
    and never writes an ``auto_now`` column, so the reorder has to move ``Pin.updated``
    itself; otherwise the client polls an unchanged timestamp and keeps drawing the old
    icon (P106).
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="bulk-label-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.location = Location.objects.create(latitude=40.0, longitude=-73.0)
        self.pin = Pin.objects.create(profile=self.profile, location=self.location, name="Cached pin")
        self.label_a = ensure_label(profile=self.profile, name="Alpha", kind="tag", order=1, icon="star")
        self.label_b = ensure_label(profile=self.profile, name="Beta", kind="tag", order=2, icon="bolt")
        self.pin.labels.add(self.label_a, self.label_b)

    def _updated(self, pin: Pin):
        """The stored ``updated`` stamp, read fresh.

        Args:
            pin: Whose stamp to read.

        Returns:
            The timestamp the client's poll is derived from.
        """
        return Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

    def _reorder_via_organize(self) -> None:
        """Swap the two labels' order through the endpoint that does it in bulk."""
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("organize.priority.save"),
            data={"items": [{"id": self.label_b.pk}, {"id": self.label_a.pk}]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_reordering_labels_actually_changes_which_icon_a_pin_draws(self):
        # Establishes the premise: without this, saying so would be pointless.
        self.assertEqual(self.pin.icon_source_label(), self.label_b)

        Label.objects.filter(pk=self.label_a.pk).update(order=99)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.icon_source_label(), self.label_a)

    def test_reordering_labels_tells_the_client_about_the_affected_pin(self):
        before = self._updated(self.pin)

        self._reorder_via_organize()

        self.assertGreater(self._updated(self.pin), before)

    def test_a_pin_carrying_only_one_of_the_reordered_labels_is_still_told(self):
        # self.pin carries both labels, which would also pass a buggy AND-style
        # filter (require every reordered label) instead of the intended
        # "carries any of them" match - this pin only carries one, so it
        # distinguishes the two.
        only_beta = Pin.objects.create(
            profile=self.profile,
            location=Location.objects.create(latitude=43.0, longitude=-70.0),
            name="Beta only",
        )
        only_beta.labels.add(self.label_b)
        before = self._updated(only_beta)

        self._reorder_via_organize()

        self.assertGreater(self._updated(only_beta), before)

    def test_a_pin_without_the_reordered_labels_is_left_alone(self):
        # A profile may hold only one pin per location, so this needs its own.
        other = Pin.objects.create(
            profile=self.profile,
            location=Location.objects.create(latitude=42.0, longitude=-71.0),
            name="Untouched",
        )
        before = self._updated(other)

        self._reorder_via_organize()

        self.assertEqual(self._updated(other), before)


class TripActivityBulkCreateQueuesCalendarPushTests(TestCase):
    """Copying a pin list into a trip must reach an auto-synced calendar.

    ``sync_trip_on_activity_save`` pushes the trip whenever an activity is saved, but
    ``copy_list_pins_to_trip`` uses ``bulk_create`` - so a list copied into an
    auto-synced trip never reached the user's calendar.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="bulk-trip-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        self.trip = Trip.objects.create(
            name="Bulk trip",
            creator=self.profile,
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2026, 11, 2),
        )
        self.pin_list = PinList.objects.create(profile=self.profile, name="Places")
        for index in range(3):
            location = Location.objects.create(latitude=41.0 + index, longitude=-72.0 - index)
            pin = Pin.objects.create(profile=self.profile, location=location, name=f"Pin {index}")
            PinListItem.objects.create(pin_list=self.pin_list, pin=pin, order=index)

    def _copy(self):
        callbacks: list = []
        with (
            mock.patch("urbanlens.dashboard.models.trips.signals.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            created = copy_list_pins_to_trip(self.pin_list, self.trip, self.profile)
            for callback in callbacks:
                callback()
        return created, enqueue

    def test_the_activities_are_created(self):
        created, _ = self._copy()
        self.assertEqual(created, 3)
        activities = list(TripActivity.objects.filter(trip=self.trip).order_by("order"))
        self.assertEqual([activity.pin.name for activity in activities], ["Pin 0", "Pin 1", "Pin 2"])
        self.assertEqual([activity.order for activity in activities], [0, 1, 2])
        self.assertTrue(all(activity.location_id == activity.pin.location_id for activity in activities))
        self.assertTrue(all(activity.added_by_id == self.profile.pk for activity in activities))

    def test_activities_are_appended_after_the_trips_existing_ones(self):
        existing = TripActivity.objects.create(
            trip=self.trip,
            location=Location.objects.create(latitude=50.0, longitude=-80.0),
            added_by=self.profile,
            order=0,
        )

        created, _ = self._copy()

        self.assertEqual(created, 3)
        new_orders = list(
            TripActivity.objects.filter(trip=self.trip)
            .exclude(pk=existing.pk)
            .order_by("order")
            .values_list("order", flat=True),
        )
        self.assertEqual(new_orders, [1, 2, 3])

    def test_an_auto_synced_trip_gets_a_calendar_push(self):
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-bulk",
            direction=CalendarSyncDirection.IMPORTED,
            auto_sync=True,
        )

        _, enqueue = self._copy()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.trip.pk)

    def test_a_trip_with_no_auto_sync_link_does_not_enqueue(self):
        _, enqueue = self._copy()
        enqueue.assert_not_called()

    def test_copying_an_empty_list_does_not_enqueue(self):
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-empty",
            direction=CalendarSyncDirection.IMPORTED,
            auto_sync=True,
        )
        self.pin_list.items.all().delete()

        _, enqueue = self._copy()
        enqueue.assert_not_called()
