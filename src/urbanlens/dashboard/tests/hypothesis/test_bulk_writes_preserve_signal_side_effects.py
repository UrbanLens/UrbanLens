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
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.pins.pin_list_trip import copy_list_pins_to_trip

CACHE_TARGET = "urbanlens.dashboard.models.pin.signals._refresh_cached_pin"


class LabelBulkUpdateRefreshesMapPinCacheTests(TestCase):
    """A label's order/icon/color decides what its pins draw on the map.

    ``Pin.icon_source_label`` sorts by ``-label.order``, so reordering labels changes
    which one supplies a pin's icon and colour. The server-side pin cache bakes those
    in, and ``refresh_map_pin_cache_for_label`` exists to invalidate it - but it is a
    ``post_save`` receiver, so the bulk paths went straight past it and pins kept
    drawing the old icon until the cache TTL lapsed.
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

    def _reorder_via_organize(self) -> mock.MagicMock:
        self.client.force_login(self.user)
        with mock.patch(CACHE_TARGET) as refresh:
            response = self.client.post(
                reverse("organize.priority.save"),
                data={"items": [{"id": self.label_b.pk}, {"id": self.label_a.pk}]},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        return refresh

    def test_reordering_labels_actually_changes_which_icon_a_pin_draws(self):
        # Establishes the premise: without this, refreshing the cache would be pointless.
        self.assertEqual(self.pin.icon_source_label(), self.label_b)

        Label.objects.filter(pk=self.label_a.pk).update(order=99)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.icon_source_label(), self.label_a)

    def test_reordering_labels_refreshes_the_cache_for_affected_pins(self):
        refresh = self._reorder_via_organize()

        refreshed = {call.args[0] for call in refresh.call_args_list}
        self.assertIn(self.pin.pk, refreshed)

    def test_a_pin_without_the_reordered_labels_is_not_refreshed(self):
        # A profile may hold only one pin per location, so this needs its own.
        other = Pin.objects.create(
            profile=self.profile, location=Location.objects.create(latitude=42.0, longitude=-71.0), name="Untouched",
        )

        refresh = self._reorder_via_organize()

        refreshed = {call.args[0] for call in refresh.call_args_list}
        self.assertNotIn(other.pk, refreshed)

    def test_each_affected_pin_is_refreshed_once_even_when_it_carries_both_labels(self):
        refresh = self._reorder_via_organize()

        refreshed = [call.args[0] for call in refresh.call_args_list if call.args[0] == self.pin.pk]
        self.assertEqual(len(refreshed), 1)


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
            name="Bulk trip", creator=self.profile, start_date=datetime.date(2026, 11, 1), end_date=datetime.date(2026, 11, 2),
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
        self.assertEqual(TripActivity.objects.filter(trip=self.trip).count(), 3)

    def test_an_auto_synced_trip_gets_a_calendar_push(self):
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-bulk", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )

        _, enqueue = self._copy()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.trip.pk)

    def test_a_trip_with_no_auto_sync_link_does_not_enqueue(self):
        _, enqueue = self._copy()
        enqueue.assert_not_called()

    def test_copying_an_empty_list_does_not_enqueue(self):
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-empty", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )
        self.pin_list.items.all().delete()

        _, enqueue = self._copy()
        enqueue.assert_not_called()
