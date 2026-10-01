"""Two overlapping ingests for one profile must merge into one pending suggestion (P49).

`ingest_location_hits` checks for an existing pending suggestion, then creates or extends it. Two
ingests at once (an Immich sweep overlapping a local-scan upload) can both miss the check and both
create, or both extend the same row and each save over the other's merged dates. Each test holds the
threads at the point between check and write, so the race is reached on every run.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import datetime
from decimal import Decimal
import threading
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.services.pins import pin_suggestions
from urbanlens.dashboard.services.pins.pin_suggestions import LocationHit, ingest_location_hits


def _hit(lat: float, lon: float, day: str) -> LocationHit:
    taken_at = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(12, 0), tzinfo=datetime.UTC)
    return LocationHit(latitude=lat, longitude=lon, taken_at=taken_at)


def _held_until_both_arrive[T](barrier: threading.Barrier, real: Callable[..., T]) -> Callable[..., T]:
    """Wrap ``real`` so each caller waits for the other before running it, or gives up after the barrier's timeout."""

    def held(*args: Any, **kwargs: Any) -> T:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return real(*args, **kwargs)

    return held


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class IngestRaceTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def _ingest_concurrently(self, first: list[LocationHit], second: list[LocationHit]) -> None:
        errors: list[BaseException] = []

        def run(hits: list[LocationHit]) -> Callable[[], None]:
            def target() -> None:
                try:
                    ingest_location_hits(self.profile, hits, origin=PinSuggestionOrigin.LOCAL_SCAN)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    connections.close_all()

            return target

        threads = [threading.Thread(target=run(first)), threading.Thread(target=run(second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])

    def test_two_ingests_of_the_same_new_place_leave_one_suggestion(self) -> None:
        barrier = threading.Barrier(2, timeout=2)
        held = _held_until_both_arrive(barrier, PinSuggestion.objects.create)

        with mock.patch.object(PinSuggestion.objects, "create", side_effect=held):
            self._ingest_concurrently([_hit(41.0, -76.0, "2024-02-01")], [_hit(41.00005, -76.00005, "2024-02-02")])

        self.assertEqual(PinSuggestion.objects.count(), 1)
        self.assertEqual(PinSuggestion.objects.get().hit_count, 2)

    def test_two_ingests_matching_the_same_pin_leave_one_suggestion(self) -> None:
        location = baker.make_recipe(
            "dashboard.location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000")
        )
        baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        barrier = threading.Barrier(2, timeout=2)
        held = _held_until_both_arrive(barrier, PinSuggestion.objects.create)

        with mock.patch.object(PinSuggestion.objects, "create", side_effect=held):
            self._ingest_concurrently([_hit(40.0001, -74.0, "2024-01-01")], [_hit(40.0001, -74.0, "2024-01-02")])

        self.assertEqual(PinSuggestion.objects.count(), 1)
        self.assertEqual(PinSuggestion.objects.get().hit_count, 2)

    def test_two_ingests_extending_one_suggestion_keep_both_dates(self) -> None:
        ingest_location_hits(self.profile, [_hit(41.0, -76.0, "2024-02-01")], origin=PinSuggestionOrigin.LOCAL_SCAN)
        barrier = threading.Barrier(2, timeout=2)
        held = _held_until_both_arrive(barrier, pin_suggestions._merge_dates)

        with mock.patch.object(pin_suggestions, "_merge_dates", side_effect=held):
            self._ingest_concurrently([_hit(41.0, -76.0, "2024-02-02")], [_hit(41.0, -76.0, "2024-02-03")])

        suggestion = PinSuggestion.objects.get()
        self.assertEqual(sorted(suggestion.visit_dates), ["2024-02-01", "2024-02-02", "2024-02-03"])
        self.assertEqual(suggestion.hit_count, 3)
