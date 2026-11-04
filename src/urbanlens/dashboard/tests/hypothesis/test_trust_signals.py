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
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals
from urbanlens.dashboard.models.visits.model import VisitSource
from urbanlens.dashboard.services.trust import (
    are_independent,
    compute_signals,
    is_real_user,
    real_user_score,
    signals_for,
)

_offsets = itertools.count(1)
LA = (34.0522, -118.2437)
NY = (40.7128, -74.0060)
SEATTLE = (47.6062, -122.3321)
CHICAGO = (41.8781, -87.6298)


def _profile(*, age_days: int = 400) -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(created=timezone.now() - timedelta(days=age_days))
    return Profile.objects.get(pk=profile.pk)


def _pins(profile: Profile, center: tuple[float, float], count: int, *, age_days: int = 30) -> list:
    pins = []
    for _ in range(count):
        step = next(_offsets) * 0.000001
        location = baker.make("dashboard.Location", latitude=center[0] + step, longitude=center[1] + step)
        pin = baker.make("dashboard.Pin", profile=profile, location=location)
        pins.append(pin)
    Pin.objects.filter(pk__in=[p.pk for p in pins]).update(created=timezone.now() - timedelta(days=age_days))
    return pins


def _active(profile: Profile, days: int, kind: str = "pin") -> None:
    today = timezone.localdate()
    for i in range(days):
        ProfileActivityDay.objects.get_or_create(profile=profile, kind=kind, day=today - timedelta(days=i))


def _visits(pin, source: str, count: int) -> None:
    for i in range(count):
        baker.make(
            "dashboard.PinVisit",
            pin=pin,
            source=source,
            visited_at=timezone.now() - timedelta(days=i + 1),
            tentative=False,
        )


class RealUserTests(TestCase):
    def test_regional_explorer_passes_without_visits(self) -> None:
        profile = _profile(age_days=365)
        _pins(profile, LA, 25)
        _pins(profile, (34.5, -118.0), 25)
        ProfileActivityDay.objects.filter(profile=profile).delete()
        _active(profile, 12)
        self.assertTrue(is_real_user(profile))
        self.assertTrue(signals_for(profile).is_real_user)

    def test_dormant_account_with_login_days_and_fresh_pins_fails(self) -> None:
        profile = _profile(age_days=180)
        _pins(profile, LA, 5, age_days=1)
        _pins(profile, NY, 5, age_days=1)
        ProfileActivityDay.objects.filter(profile=profile).delete()
        _active(profile, 30, kind="login")
        self.assertFalse(is_real_user(profile))

    def test_one_day_account_with_maxed_visits_fails(self) -> None:
        profile = _profile(age_days=1)
        pins = _pins(profile, LA, 12)
        _active(profile, 1)
        for source in (VisitSource.GEOLOCATION, VisitSource.PHOTO, VisitSource.MANUAL):
            _visits(pins[0], source, 10)
        self.assertFalse(is_real_user(profile))

    def test_fresh_account_fails(self) -> None:
        profile = _profile(age_days=1)
        _pins(profile, LA, 3, age_days=0)
        _active(profile, 2)
        self.assertFalse(is_real_user(profile))

    def test_visits_can_qualify_a_younger_account(self) -> None:
        profile = _profile(age_days=120)
        pins = _pins(profile, LA, 12)
        _pins(profile, NY, 2)
        _active(profile, 12)
        _visits(pins[0], VisitSource.GEOLOCATION, 2)
        self.assertTrue(is_real_user(profile))

    def test_recorded_visits_are_capped_below_a_device_visit(self) -> None:
        base = {"account_age_days": 10, "active_days_90": 5, "pin_count": 5, "region_count": 1}
        many_recorded = real_user_score(ProfileTrustSignals(recorded_visits=50, **base))
        one_device = real_user_score(ProfileTrustSignals(device_track_visits=1, **base))
        self.assertLess(many_recorded, one_device)

    def test_vouching_counts_only_friends_stored_as_real(self) -> None:
        real, fake, me = _profile(), _profile(), _profile()
        ProfileTrustSignals.objects.create(profile=real, computed_at=timezone.now(), is_real_user=True)
        ProfileTrustSignals.objects.create(profile=fake, computed_at=timezone.now(), is_real_user=False)
        for friend in (real, fake):
            baker.make("dashboard.Friendship", from_profile=me, to_profile=friend, status=FriendshipStatus.ACCEPTED)
        self.assertEqual(compute_signals(me).vouch_count, 1)

    def test_signals_count_visits_pins_and_cells(self) -> None:
        profile = _profile()
        pins = _pins(profile, LA, 12)
        _pins(profile, LA, 3, age_days=1)
        _visits(pins[0], VisitSource.GEOLOCATION, 1)
        _visits(pins[0], VisitSource.PHOTO, 1)
        row = compute_signals(profile)
        self.assertEqual((row.device_track_visits, row.photo_visits, row.recorded_visits), (1, 1, 0))
        self.assertEqual(row.pin_count, 12)
        self.assertEqual(row.region_count, 1)
        self.assertGreaterEqual(len(row.pin_cells), 1)
        self.assertTrue(all(len(c) == 6 for c in row.pin_cells))


@override_settings(UL_TRUST_MIN_PINS_FOR_GEOGRAPHY=5)
class IndependenceTests(TestCase):
    def test_la_and_ny_are_independent(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, NY, 8)
        self.assertTrue(are_independent(a, b))

    def test_near_identical_cells_are_not(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, LA, 8)
        self.assertFalse(are_independent(a, b))

    def test_same_metro_explorers_with_mostly_distinct_cells_pass(self) -> None:
        a, b = _profile(), _profile()
        for i in range(6):
            _pins(a, (LA[0] + i * 0.02, LA[1]), 2)
            _pins(b, (LA[0] + i * 0.02, LA[1] + 0.2), 2)
        self.assertTrue(are_independent(a, b))

    def test_excluded_cell_is_ignored(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, LA, 8)
        _pins(a, NY, 1)
        shared = signals_for(a).pin_cells[0]
        self.assertFalse(are_independent(a, b))
        self.assertTrue(are_independent(a, b, exclude_cell=shared))

    def test_friendship_never_flips_pass_to_fail(self) -> None:
        for centers in ((LA, NY), (LA, LA)):
            a, b = _profile(), _profile()
            _pins(a, centers[0], 8)
            _pins(b, centers[1], 8)
            before = are_independent(a, b)
            baker.make("dashboard.Friendship", from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)
            self.assertTrue(are_independent(a, b) or not before)

    def test_friendship_can_loosen_a_borderline_pair(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(a, NY, 1)
        _pins(b, LA, 8)
        _pins(b, SEATTLE, 1)
        with override_settings(UL_TRUST_MAX_CELL_JACCARD=0.3, UL_TRUST_FRIENDSHIP_JACCARD_BONUS=0.1):
            self.assertFalse(are_independent(a, b))
            baker.make("dashboard.Friendship", from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)
            self.assertTrue(are_independent(a, b))

    def test_accounts_registered_days_apart_fail_unless_both_established(self) -> None:
        a, b = _profile(age_days=100), _profile(age_days=98)
        _pins(a, LA, 8)
        _pins(b, NY, 8)
        self.assertFalse(are_independent(a, b))
        old_a, old_b = _profile(age_days=400), _profile(age_days=398)
        _pins(old_a, LA, 8)
        _pins(old_b, NY, 8)
        self.assertTrue(are_independent(old_a, old_b))
        far_a, far_b = _profile(age_days=100), _profile(age_days=20)
        _pins(far_a, LA, 8)
        _pins(far_b, NY, 8)
        self.assertTrue(are_independent(far_a, far_b))

    def test_shared_invitation_chain_fails(self) -> None:
        inviter, a, b = _profile(), _profile(), _profile()
        _pins(a, LA, 8)
        _pins(b, NY, 8)
        self.assertTrue(are_independent(a, b))
        for invitee in (a, b):
            baker.make("dashboard.FriendInvitation", inviter=inviter, invitee=invitee, accepted_at=timezone.now())
        self.assertFalse(are_independent(a, b))

    def test_sparse_profile_needs_the_higher_bar(self) -> None:
        sparse, other = _profile(), _profile()
        _pins(sparse, LA, 2)
        _pins(other, NY, 8)
        self.assertFalse(are_independent(sparse, other))
        with override_settings(UL_TRUST_NO_GEOGRAPHY_THRESHOLD=0.0):
            self.assertTrue(are_independent(sparse, other))

    def test_a_profile_is_not_independent_of_itself(self) -> None:
        a = _profile()
        _pins(a, LA, 8)
        self.assertFalse(are_independent(a, a))


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
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", return_value="id") as safe:
            count = tasks.refresh_trust_signals(chunk_size=2)
        self.assertGreaterEqual(count, 3)
        self.assertEqual(safe.call_count, count)

    def test_range_is_idempotent(self) -> None:
        profiles = [_profile() for _ in range(3)]
        _pins(profiles[0], LA, 4)
        for profile in profiles:
            _active(profile, 1)
        lo, hi = min(p.pk for p in profiles), max(p.pk for p in profiles)
        fields = ("profile_id", "pin_count", "pin_cells", "active_days_90", "is_real_user")
        first = tasks.refresh_trust_signals_range(lo, hi)
        snapshot = list(ProfileTrustSignals.objects.order_by("profile_id").values_list(*fields))
        second = tasks.refresh_trust_signals_range(lo, hi)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, list(ProfileTrustSignals.objects.order_by("profile_id").values_list(*fields)))

    def test_dormant_profile_with_a_fresh_row_is_skipped(self) -> None:
        profile = _profile()
        ProfileActivityDay.objects.filter(profile=profile).delete()
        ProfileTrustSignals.objects.create(profile=profile, computed_at=timezone.now(), active_days_90=0)
        self.assertEqual(tasks.refresh_trust_signals_range(profile.pk, profile.pk), 0)
        ProfileTrustSignals.objects.filter(profile=profile).update(computed_at=timezone.now() - timedelta(days=30))
        self.assertEqual(tasks.refresh_trust_signals_range(profile.pk, profile.pk), 1)
