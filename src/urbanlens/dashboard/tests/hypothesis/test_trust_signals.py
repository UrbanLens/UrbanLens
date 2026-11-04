"""Real-user score, independence check and the nightly signals refresh (ADR35 section 5)."""

from __future__ import annotations

from datetime import timedelta
import itertools
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.achievements.model import ProfileActivityDay
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals
from urbanlens.dashboard.models.visits.model import VisitSource
from urbanlens.dashboard.services.trust import are_independent, compute_signals, is_real_user, signals_for

_offsets = itertools.count(1)
LA = (34.05, -118.24)
NY = (40.71, -74.00)


def _profile(*, age_days: int = 0) -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(created=timezone.now() - timedelta(days=age_days))
    return Profile.objects.get(pk=profile.pk)


def _pins(profile: Profile, center: tuple[float, float], count: int) -> list:
    pins = []
    for i in range(count):
        location = baker.make(
            "dashboard.Location",
            latitude=center[0] + next(_offsets) * 0.0001,
            longitude=center[1] + next(_offsets) * 0.0001,
        )
        pins.append(baker.make("dashboard.Pin", profile=profile, location=location))
    return pins


def _active(profile: Profile, days: int) -> None:
    today = timezone.localdate()
    for i in range(days):
        ProfileActivityDay.objects.get_or_create(profile=profile, kind="pin", day=today - timedelta(days=i))


def _established(center: tuple[float, float]) -> Profile:
    profile = _profile(age_days=400)
    pins = _pins(profile, center, 12)
    _active(profile, 30)
    for source in (VisitSource.GEOLOCATION, VisitSource.PHOTO):
        baker.make(
            "dashboard.PinVisit",
            pin=pins[0],
            source=source,
            visited_at=timezone.now() - timedelta(days=3),
            tentative=False,
        )
    return profile


class RealUserTests(TestCase):
    def test_fresh_account_fails(self) -> None:
        profile = _profile(age_days=1)
        _pins(profile, LA, 3)
        _active(profile, 2)
        self.assertFalse(is_real_user(profile))

    def test_established_account_passes(self) -> None:
        self.assertTrue(is_real_user(_established(LA)))

    def test_recorded_visits_alone_cannot_carry_the_score(self) -> None:
        profile = _profile(age_days=400)
        pins = _pins(profile, LA, 12)
        _active(profile, 30)
        for i in range(20):
            baker.make(
                "dashboard.PinVisit",
                pin=pins[0],
                source=VisitSource.MANUAL,
                visited_at=timezone.now() - timedelta(days=i),
                tentative=False,
            )
        self.assertFalse(is_real_user(profile))

    def test_signals_count_visits_by_kind_and_cells(self) -> None:
        profile = _established(LA)
        row = compute_signals(profile)
        self.assertEqual((row.device_track_visits, row.photo_visits, row.recorded_visits), (1, 1, 0))
        self.assertEqual(row.pin_count, 12)
        self.assertEqual(len(row.pin_cells), 1)
        self.assertEqual(row.active_days_90, 30)


@override_settings(UL_TRUST_MIN_PINS_FOR_GEOGRAPHY=5)
class IndependenceTests(TestCase):
    def test_la_and_ny_are_independent(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, NY, 8)
        self.assertTrue(are_independent(a, b, exclude_cell=None))

    def test_near_identical_cells_are_not(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, LA, 8)
        self.assertFalse(are_independent(a, b, exclude_cell=None))

    def test_excluded_cell_is_ignored(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, LA, 8)
        _pins(a, NY, 1)
        _pins(b, (47.6, -122.3), 1)
        shared = signals_for(a).pin_cells[0]
        self.assertFalse(are_independent(a, b, exclude_cell=None))
        self.assertTrue(are_independent(a, b, exclude_cell=shared))

    def test_friendship_never_flips_pass_to_fail(self) -> None:
        for pair_center in ((LA, NY), (LA, LA)):
            a, b = _profile(), _profile()
            _pins(a, pair_center[0], 8)
            _pins(b, pair_center[1], 8)
            before = are_independent(a, b, exclude_cell=None)
            baker.make("dashboard.Friendship", from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)
            after = are_independent(a, b, exclude_cell=None)
            self.assertTrue(after or not before)

    def test_friendship_can_loosen_a_borderline_pair(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(a, NY, 1)
        _pins(b, LA, 8)
        _pins(b, (47.6, -122.3), 1)
        # Jaccard is 1/3 here: above the strict bar, below it plus the friendship bonus.
        with override_settings(UL_TRUST_MAX_CELL_JACCARD=0.3, UL_TRUST_FRIENDSHIP_JACCARD_BONUS=0.1):
            self.assertFalse(are_independent(a, b, exclude_cell=None))
            baker.make("dashboard.Friendship", from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)
            self.assertTrue(are_independent(a, b, exclude_cell=None))

    def test_sparse_profile_needs_the_higher_bar(self) -> None:
        sparse, other = _profile(age_days=1), _profile()
        _pins(sparse, LA, 2)
        _pins(other, NY, 8)
        self.assertFalse(are_independent(sparse, other, exclude_cell=None))
        with override_settings(UL_TRUST_NO_GEOGRAPHY_THRESHOLD=0.0):
            self.assertTrue(are_independent(sparse, other, exclude_cell=None))

    def test_a_profile_is_not_independent_of_itself(self) -> None:
        a = _profile()
        _pins(a, LA, 8)
        self.assertFalse(are_independent(a, a, exclude_cell=None))


class SignalsCacheTests(TestCase):
    def test_stale_or_missing_row_is_recomputed_fresh_row_is_not(self) -> None:
        profile = _profile()
        with patch("urbanlens.dashboard.services.trust.signals.compute_signals", wraps=compute_signals) as spy:
            signals_for(profile)
            self.assertEqual(spy.call_count, 1)
            signals_for(profile)
            self.assertEqual(spy.call_count, 1)
            ProfileTrustSignals.objects.filter(profile=profile).update(computed_at=timezone.now() - timedelta(days=2))
            signals_for(profile)
            self.assertEqual(spy.call_count, 2)


class NightlyRefreshTests(TestCase):
    def test_dispatch_is_chunked(self) -> None:
        for _ in range(5):
            _profile()
        with (
            patch.object(tasks.refresh_trust_signals_range, "apply_async") as enqueue,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", return_value="id") as safe,
        ):
            count = tasks.refresh_trust_signals(chunk_size=2)
        self.assertGreaterEqual(count, 3)
        self.assertEqual(safe.call_count, count)
        enqueue.assert_not_called()

    def test_range_is_idempotent(self) -> None:
        profiles = [_profile() for _ in range(3)]
        _pins(profiles[0], LA, 4)
        lo, hi = min(p.pk for p in profiles), max(p.pk for p in profiles)
        first = tasks.refresh_trust_signals_range(lo, hi)
        snapshot = list(
            ProfileTrustSignals.objects.order_by("profile_id").values_list(
                "profile_id", "pin_count", "pin_cells", "active_days_90"
            )
        )
        second = tasks.refresh_trust_signals_range(lo, hi)
        self.assertEqual(first, second)
        self.assertEqual(
            snapshot,
            list(
                ProfileTrustSignals.objects.order_by("profile_id").values_list(
                    "profile_id", "pin_count", "pin_cells", "active_days_90"
                )
            ),
        )
        self.assertEqual(ProfileTrustSignals.objects.filter(profile__in=profiles).count(), 3)
