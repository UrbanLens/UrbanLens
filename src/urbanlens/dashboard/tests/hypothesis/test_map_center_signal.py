"""Tests for the pin_invalidate_map_center post_save signal."""

from __future__ import annotations

import datetime
import decimal
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from hypothesis import HealthCheck, given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import MAP_CENTRE_RECLAIM_AFTER, Profile

_db_settings = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_CACHED_LAT = decimal.Decimal("42.650000")
_CACHED_LNG = decimal.Decimal("-73.750000")

#: Where every caller binds the enqueue from, so patching it here catches the signal's own call.
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


def _set_cached_centroid(profile: Profile) -> None:
    Profile.objects.filter(pk=profile.pk).update(
        map_center_latitude=_CACHED_LAT,
        map_center_longitude=_CACHED_LNG,
        map_center_stale_since=None,
    )


def _queued(enqueue: mock.Mock) -> list:
    """The tasks an enqueue mock was handed.

    Args:
        enqueue: The patched ``safely_enqueue_task``.

    Returns:
        One entry per call, in order."""
    return [call.args[0] for call in enqueue.call_args_list if call.args]


class NewPinQueuesARecomputeTests(TestCase):
    """A new pin outdates the centroid, and must say so without throwing away the one being served.

    Clearing it would hand the next visitor a read of every pin the account owns, to move an opening map
    position by less than a pixel."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        _set_cached_centroid(self.profile)

    def test_new_pin_keeps_the_cached_latitude(self) -> None:
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.map_center_latitude, _CACHED_LAT)

    def test_new_pin_keeps_the_cached_longitude(self) -> None:
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.map_center_longitude, _CACHED_LNG)

    def test_new_pin_marks_the_centre_stale(self) -> None:
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_stale_since)

    def test_new_pin_queues_the_recompute(self) -> None:
        from urbanlens.dashboard.tasks import refresh_profile_map_center

        with mock.patch(_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile)

        self.assertIn(refresh_profile_map_center, _queued(enqueue))

    def test_a_second_pin_does_not_queue_a_second_recompute(self) -> None:
        """The claim is the whole point: an import must cost one recompute, not one per row."""
        from urbanlens.dashboard.tasks import refresh_profile_map_center

        with mock.patch(_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile)
            baker.make(Pin, profile=self.profile)

        self.assertEqual(_queued(enqueue).count(refresh_profile_map_center), 1)

    @given(n=st.integers(min_value=2, max_value=6))
    @_db_settings
    def test_any_number_of_pins_queues_one_recompute(self, n: int) -> None:
        from urbanlens.dashboard.tasks import refresh_profile_map_center

        _set_cached_centroid(self.profile)
        with mock.patch(_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            for _ in range(n):
                baker.make(Pin, profile=self.profile)

        self.assertEqual(_queued(enqueue).count(refresh_profile_map_center), 1)

    def test_a_claim_nothing_ever_finished_is_retaken(self) -> None:
        """``safely_enqueue_task`` reports an unreachable broker only in the log, so a claim can outlive the
        work it was taken for. Without this, one lost enqueue would wedge the centre stale permanently."""
        from urbanlens.dashboard.tasks import refresh_profile_map_center

        baker.make(Pin, profile=self.profile)
        abandoned = timezone.now() - MAP_CENTRE_RECLAIM_AFTER - datetime.timedelta(minutes=1)
        Profile.objects.filter(pk=self.profile.pk).update(map_center_stale_since=abandoned)

        with mock.patch(_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile)

        self.assertIn(refresh_profile_map_center, _queued(enqueue))


class RecomputingReleasesTheClaimTests(TestCase):
    """The recompute clears the claim, so the next pin can take it again."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_refreshing_clears_the_claim(self) -> None:
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_stale_since)

        self.profile.refresh_map_center()

        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_stale_since)

    def test_the_claim_is_released_before_the_pins_are_read(self) -> None:
        """A pin created while the coordinates are being read has to leave a claim behind for another pass.
        Releasing afterwards would overwrite it, and that pin would never reach the stored centre."""
        seen: list[datetime.datetime | None] = []
        real_compute = Profile.compute_map_center

        def recording(instance: Profile) -> tuple[float, float] | None:
            seen.append(Profile.objects.filter(pk=instance.pk).values_list("map_center_stale_since", flat=True).first())
            return real_compute(instance)

        baker.make(Pin, profile=self.profile)
        with mock.patch.object(Profile, "compute_map_center", recording):
            self.profile.refresh_map_center()

        self.assertEqual(seen, [None])


class UpdatingAPinLeavesTheCentreAloneTests(TestCase):
    """Updating an existing pin moves nothing, so it must not queue anything either."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile)
        _set_cached_centroid(self.profile)

    def test_saving_existing_pin_does_not_clear_cache(self) -> None:
        self.pin.name = "Updated name"
        self.pin.save()
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)
        self.assertIsNotNone(self.profile.map_center_longitude)

    def test_saving_existing_pin_does_not_mark_the_centre_stale(self) -> None:
        self.pin.name = "Updated name"
        self.pin.save()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_stale_since)

    def test_multiple_updates_do_not_clear_cache(self) -> None:
        for i in range(3):
            self.pin.name = f"Update {i}"
            self.pin.save()
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)


class InvalidateMapCenterNoProfileTests(TestCase):
    """A pin saved without a profile_id must not raise and must not touch any profile."""

    def test_pin_without_profile_does_not_raise(self) -> None:
        from urbanlens.dashboard.models.pin.signals import invalidate_profile_map_center

        other_profile: Profile = baker.make(User).profile
        _set_cached_centroid(other_profile)

        fake_pin = Pin()
        fake_pin.profile_id = None

        invalidate_profile_map_center(sender=Pin, instance=fake_pin, created=True)

        other_profile.refresh_from_db()
        self.assertIsNotNone(other_profile.map_center_latitude)
        self.assertIsNone(other_profile.map_center_stale_since)
