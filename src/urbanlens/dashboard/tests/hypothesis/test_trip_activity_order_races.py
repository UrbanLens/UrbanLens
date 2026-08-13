"""Concurrency tests for trip activity ordering.

Two check-then-act sequences share the ``order`` column:

``reorder_activities`` validates that the submitted ids are an exact permutation
of the trip's non-completed activities, then applies the new positions with one
``update()`` per activity. Nothing holds between the check and the writes, so two
members dragging the itinerary at once interleave and the trip ends up in an order
neither of them asked for - with the same position written to two rows.

``create_activity`` appends at ``order=trip.activities.count()``. Two concurrent
adds both read the same count and both take that position.

A unique constraint on ``(trip, order)`` is *not* the fix, which is worth stating
because it is the obvious first idea. Reordering assigns positions one row at a
time, so a partial permutation legitimately collides mid-loop; and reordering only
covers non-completed activities, leaving completed ones holding whatever positions
they already had, which collide with the reassigned range by design. Serialising
on the parent trip is what actually matches how the data is used.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
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
        self.trip = baker.make(Trip, creator=self.profile, allow_edit_activities="everyone", allow_add_activities="everyone")
        self.trip.profiles.add(self.profile)
        self.activities = [baker.make(TripActivity, trip=self.trip, title=f"Stop {n}", order=n, scheduled_at=None) for n in range(4)]

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
