"""Concurrency tests for trip activity ordering."""

from __future__ import annotations

import threading
from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.pins.pin_list_trip import copy_list_pins_to_trip
from urbanlens.dashboard.services.trips import trip_activities


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class TripActivityOrderRaceTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.trip = baker.make(
            Trip, creator=self.profile, allow_edit_activities="everyone", allow_add_activities="everyone"
        )
        self.trip.profiles.add(self.profile)
        self.activities = [
            baker.make(TripActivity, trip=self.trip, title=f"Stop {n}", order=n, scheduled_at=None) for n in range(4)
        ]

    def _run_concurrently(self, first, second) -> list[Exception]:
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def run(fn):
            def inner() -> None:
                try:
                    barrier.wait(timeout=10)
                    fn()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    connections.close_all()

            return inner

        threads = [threading.Thread(target=run(first)), threading.Thread(target=run(second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return errors

    def test_two_simultaneous_reorders_leave_a_coherent_order(self) -> None:
        ids = [activity.pk for activity in self.activities]
        forward = list(reversed(ids))
        shuffled = [ids[2], ids[0], ids[3], ids[1]]

        errors = self._run_concurrently(
            lambda: trip_activities.reorder_activities(self.trip, self.profile, forward),
            lambda: trip_activities.reorder_activities(self.trip, self.profile, shuffled),
        )
        self.assertEqual(errors, [], f"reordering raised under concurrency: {errors}")

        orders = sorted(TripActivity.objects.filter(trip=self.trip).values_list("order", flat=True))
        self.assertEqual(orders, [0, 1, 2, 3], "every activity must hold a distinct position, whichever reorder won")

    def test_two_simultaneous_adds_do_not_share_a_position(self) -> None:
        def add(title: str):
            def inner() -> None:
                trip_activities.create_activity(self.trip, self.profile, title=title)

            return inner

        errors = self._run_concurrently(add("Added A"), add("Added B"))
        self.assertEqual(errors, [], f"adding raised under concurrency: {errors}")

        added = TripActivity.objects.filter(trip=self.trip, title__startswith="Added").values_list("order", flat=True)
        self.assertEqual(len(added), 2)
        self.assertEqual(len(set(added)), 2, "two activities appended at once must not take the same position")

    def test_a_sequential_reorder_still_applies_exactly(self) -> None:
        """The ordinary uncontended path the locking must not change."""
        ids = [activity.pk for activity in self.activities]
        trip_activities.reorder_activities(self.trip, self.profile, list(reversed(ids)))

        by_id = dict(TripActivity.objects.filter(trip=self.trip).values_list("id", "order"))
        self.assertEqual([by_id[activity_id] for activity_id in ids], [3, 2, 1, 0])

    def _pin_list(self, size: int) -> PinList:
        pin_list = baker.make(PinList, profile=self.profile, name="Weekend")
        for n in range(size):
            baker.make(
                PinListItem, pin_list=pin_list, pin=baker.make(Pin, profile=self.profile, name=f"Pin {n}"), order=n
            )
        return pin_list

    def test_an_activity_added_after_deletions_sorts_after_every_existing_one(self) -> None:
        TripActivity.objects.filter(pk__in=[self.activities[0].pk, self.activities[1].pk]).delete()

        added = trip_activities.create_activity(self.trip, self.profile, title="Added")

        others = TripActivity.objects.filter(trip=self.trip).exclude(pk=added.pk).values_list("order", flat=True)
        self.assertGreater(added.order, max(others))

    def test_a_list_copy_after_deletions_appends_after_every_existing_activity(self) -> None:
        TripActivity.objects.filter(pk__in=[self.activities[0].pk, self.activities[1].pk]).delete()

        copy_list_pins_to_trip(self._pin_list(2), self.trip, self.profile)

        copied = list(TripActivity.objects.filter(trip=self.trip, pin__isnull=False).values_list("order", flat=True))
        self.assertEqual(len(copied), 2)
        self.assertGreater(min(copied), max(activity.order for activity in self.activities[2:]))
        self.assertEqual(len(set(copied)), 2)

    def test_a_list_copy_racing_an_add_does_not_share_a_position(self) -> None:
        """The add is started once the copy has read its position and before it writes, as late as a real race can."""
        pin_list = self._pin_list(2)
        add_finished = threading.Event()
        errors: list[Exception] = []

        def add() -> None:
            try:
                trip_activities.create_activity(self.trip, self.profile, title="Added")
            except Exception as exc:
                errors.append(exc)
            finally:
                add_finished.set()
                connections.close_all()

        adder = threading.Thread(target=add)
        real_bulk_create = TripActivity.objects.bulk_create

        def bulk_create_after_an_add(objs, *args, **kwargs):
            adder.start()
            # Times out while the copy holds the trip, which is what keeps the add from interleaving.
            add_finished.wait(timeout=2)
            return real_bulk_create(objs, *args, **kwargs)

        with mock.patch.object(TripActivity.objects, "bulk_create", side_effect=bulk_create_after_an_add):
            copy_list_pins_to_trip(pin_list, self.trip, self.profile)
        adder.join(timeout=30)

        self.assertEqual(errors, [])
        orders = list(TripActivity.objects.filter(trip=self.trip).values_list("order", flat=True))
        self.assertEqual(len(orders), 7)
        self.assertEqual(len(set(orders)), 7, f"two activities share a position: {sorted(orders)}")
