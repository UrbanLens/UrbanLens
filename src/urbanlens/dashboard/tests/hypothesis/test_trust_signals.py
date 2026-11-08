"""Real-user score, independence check and the nightly signals refresh (ADR35 section 5)."""

from __future__ import annotations

from datetime import timedelta
import itertools
from unittest.mock import patch

from django.db import OperationalError
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.achievements.model import ProfileActivityDay
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
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


def _weeks(profile: Profile, weeks: int, kind: str = "pin") -> None:
    """One meaningful day in each of ``weeks`` consecutive ISO weeks, ending this week."""
    today = timezone.localdate()
    for i in range(weeks):
        ProfileActivityDay.objects.get_or_create(profile=profile, kind=kind, day=today - timedelta(weeks=i))


def _visit_days(pin, source: str, count: int) -> None:
    for i in range(count):
        baker.make(
            "dashboard.PinVisit",
            pin=pin,
            source=source,
            visited_at=timezone.now() - timedelta(days=i + 1),
            tentative=False,
        )


def _metro_pins(profile: Profile, count: int, *, age_days: int = 30) -> list:
    """Pins spread over three geohash-5 cells (about 5 km each) around LA."""
    pins = []
    for i in range(3):
        pins += _pins(profile, (LA[0] + i * 0.06, LA[1]), count // 3 + (1 if i < count % 3 else 0), age_days=age_days)
    return pins


def _clear_activity(profile: Profile) -> None:
    ProfileActivityDay.objects.filter(profile=profile).delete()


def _befriend(a: Profile, b: Profile, *, age_days: int = 60) -> None:
    friendship = baker.make("dashboard.Friendship", from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)
    Friendship.objects.filter(pk=friendship.pk).update(updated=timezone.now() - timedelta(days=age_days))


class RealUserTests(TestCase):
    def test_one_metro_year_old_account_with_recorded_visits_passes(self) -> None:
        profile = _profile(age_days=365)
        pins = _metro_pins(profile, 10)
        _clear_activity(profile)
        _weeks(profile, 4)
        _visit_days(pins[0], VisitSource.MANUAL, 4)
        self.assertTrue(is_real_user(profile))
        self.assertTrue(signals_for(profile).is_real_user)

    def test_light_long_standing_user_passes_without_visits(self) -> None:
        profile = _profile(age_days=365)
        _metro_pins(profile, 15)
        _clear_activity(profile)
        _weeks(profile, 3)
        self.assertTrue(is_real_user(profile))

    def test_dormant_aged_account_with_forged_geolocation_visits_fails_on_the_floor(self) -> None:
        profile = _profile(age_days=180)
        pins = _metro_pins(profile, 10)
        _clear_activity(profile)
        _visit_days(pins[0], VisitSource.GEOLOCATION, 4)
        signals = signals_for(profile)
        self.assertEqual(signals.active_weeks_90, 0)
        self.assertFalse(is_real_user(profile))

    def test_login_only_weeks_do_not_count(self) -> None:
        profile = _profile(age_days=365)
        _metro_pins(profile, 10)
        _clear_activity(profile)
        _weeks(profile, 8, kind="login")
        self.assertFalse(is_real_user(profile))

    def test_young_account_maxed_everywhere_else_fails(self) -> None:
        profile = _profile(age_days=20)
        pins = _metro_pins(profile, 12, age_days=15)
        _clear_activity(profile)
        _weeks(profile, 6)
        _visit_days(pins[0], VisitSource.PHOTO, 5)
        signals = signals_for(profile)
        self.assertGreaterEqual(real_user_score(signals), 0.6)
        self.assertFalse(is_real_user(profile))

    def test_fresh_pins_do_not_count(self) -> None:
        profile = _profile(age_days=365)
        _metro_pins(profile, 12, age_days=1)
        self.assertEqual(signals_for(profile).pin_count, 0)

    def test_one_cluster_of_pins_does_not_fill_spread(self) -> None:
        profile = _profile(age_days=365)
        _pins(profile, LA, 12)
        self.assertEqual(signals_for(profile).spread_cells, 1)

    def test_visits_are_capped_and_claims_count_less_than_photos(self) -> None:
        base = {"account_age_days": 10, "active_weeks_90": 1}
        claims = real_user_score(ProfileTrustSignals(claim_visit_days=50, **base))
        capped = real_user_score(ProfileTrustSignals(claim_visit_days=4, **base))
        photos = real_user_score(ProfileTrustSignals(photo_visit_days=2, **base))
        self.assertEqual(claims, capped)
        self.assertEqual(capped, photos)
        self.assertLess(
            real_user_score(ProfileTrustSignals(claim_visit_days=1, **base)),
            real_user_score(ProfileTrustSignals(photo_visit_days=1, **base)),
        )

    def test_trusted_friend_needs_an_old_friendship_with_a_qualifying_base_score(self) -> None:
        me, good, young_friendship, weak = (_profile() for _ in range(4))
        now = timezone.now()
        for friend, score in ((good, 0.9), (young_friendship, 0.9), (weak, 0.2)):
            ProfileTrustSignals.objects.create(
                profile=friend, computed_at=now, base_score=score, account_age_days=400, active_weeks_90=6
            )
            baker.make("dashboard.Friendship", from_profile=me, to_profile=friend, status=FriendshipStatus.ACCEPTED)
        Friendship.objects.update(updated=now - timedelta(days=60))
        Friendship.objects.filter(to_profile=young_friendship).update(updated=now - timedelta(days=5))
        self.assertEqual(compute_signals(me).trusted_friend_count, 1)

    def test_trusted_friend_count_does_not_feed_back(self) -> None:
        now = timezone.now()
        seeds = [_profile(), _profile()]
        for seed in seeds:
            ProfileTrustSignals.objects.create(
                profile=seed, computed_at=now, base_score=0.67, account_age_days=400, active_weeks_90=6, pin_count=10
            )
        c, d = _profile(), _profile()
        _weeks(c, 6)
        for seed in seeds:
            baker.make("dashboard.Friendship", from_profile=c, to_profile=seed, status=FriendshipStatus.ACCEPTED)
        baker.make("dashboard.Friendship", from_profile=c, to_profile=d, status=FriendshipStatus.ACCEPTED)
        Friendship.objects.update(updated=now - timedelta(days=60))
        self.assertEqual(compute_signals(c).trusted_friend_count, 2)
        self.assertEqual(compute_signals(d).trusted_friend_count, 0)

    def test_signals_count_visit_days_pins_and_cells(self) -> None:
        profile = _profile()
        pins = _pins(profile, LA, 12)
        _pins(profile, LA, 3, age_days=1)
        _visit_days(pins[0], VisitSource.GEOLOCATION, 1)
        _visit_days(pins[0], VisitSource.PHOTO, 1)
        row = compute_signals(profile)
        self.assertEqual((row.claim_visit_days, row.photo_visit_days), (1, 1))
        self.assertEqual(row.pin_count, 12)
        self.assertGreaterEqual(len(row.pin_cells), 1)
        self.assertTrue(all(len(c) == 6 for c in row.pin_cells))

    def test_old_pins_fall_out_of_the_cell_window(self) -> None:
        profile = _profile()
        _pins(profile, NY, 6, age_days=900)
        _pins(profile, LA, 6, age_days=30)
        row = compute_signals(profile)
        self.assertEqual(row.pin_count, 12)
        self.assertEqual(len(row.pin_cells), 1)


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
        self.assertFalse(are_independent(a, b, exclude_cell=shared))
        _weeks(b, 3)
        compute_signals(b)
        with override_settings(UL_TRUST_NO_GEOGRAPHY_THRESHOLD=0.0):
            self.assertTrue(are_independent(a, b, exclude_cell=shared))

    def test_friendship_never_flips_pass_to_fail(self) -> None:
        for centers in ((LA, NY), (LA, LA)):
            a, b = _profile(), _profile()
            _pins(a, centers[0], 8)
            _pins(b, centers[1], 8)
            before = are_independent(a, b)
            _befriend(a, b)
            self.assertTrue(are_independent(a, b) or not before)

    def test_established_friendship_loosens_a_borderline_pair_a_new_one_does_not(self) -> None:
        a, b = _profile(), _profile()
        _pins(a, LA, 8)
        _pins(a, NY, 1)
        _pins(b, LA, 8)
        # Jaccard is 1/2 here: at the strict limit, under the friendship limit.
        self.assertFalse(are_independent(a, b))
        _befriend(a, b, age_days=5)
        self.assertFalse(are_independent(a, b))
        Friendship.objects.update(updated=timezone.now() - timedelta(days=60))
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

    def test_invitation_links_fail_only_while_the_younger_account_is_under_180_days(self) -> None:
        for ages, expected in (((400, 400), True), ((400, 100), False)):
            inviter, a, b = _profile(), _profile(age_days=ages[0]), _profile(age_days=ages[1])
            _pins(a, LA, 8)
            _pins(b, NY, 8)
            for invitee in (a, b):
                baker.make("dashboard.FriendInvitation", inviter=inviter, invitee=invitee, accepted_at=timezone.now())
            self.assertEqual(are_independent(a, b), expected)

    def test_direct_invitation_fails_a_young_pair(self) -> None:
        a, b = _profile(age_days=100), _profile(age_days=20)
        _pins(a, LA, 8)
        _pins(b, NY, 8)
        self.assertTrue(are_independent(a, b))
        baker.make("dashboard.FriendInvitation", inviter=a, invitee=b, accepted_at=timezone.now())
        self.assertFalse(are_independent(a, b))

    def test_sparse_profile_needs_the_higher_bar(self) -> None:
        sparse, other = _profile(), _profile()
        _pins(sparse, LA, 2)
        _pins(other, NY, 8)
        self.assertFalse(are_independent(sparse, other))
        _weeks(sparse, 3)
        compute_signals(sparse)
        with override_settings(UL_TRUST_NO_GEOGRAPHY_THRESHOLD=0.0):
            self.assertTrue(are_independent(sparse, other))

    def test_target_cell_only_account_is_not_zero_overlap(self) -> None:
        x, v = _profile(), _profile()
        _pins(x, LA, 8)
        _pins(v, LA, 8)
        _pins(v, NY, 1)
        target = signals_for(x).pin_cells[0]
        self.assertFalse(are_independent(x, v, exclude_cell=target))
        _weeks(x, 6)
        compute_signals(x)
        with override_settings(UL_TRUST_NO_GEOGRAPHY_THRESHOLD=0.0):
            self.assertTrue(are_independent(x, v, exclude_cell=target))

    def test_pins_older_than_the_cell_window_leave_no_geography(self) -> None:
        old, other = _profile(), _profile()
        _pins(old, LA, 8, age_days=900)
        _pins(other, NY, 8)
        self.assertEqual(signals_for(old).pin_cells, [])
        self.assertFalse(are_independent(old, other))
        _weeks(old, 3)
        compute_signals(old)
        with override_settings(UL_TRUST_NO_GEOGRAPHY_THRESHOLD=0.0):
            self.assertTrue(are_independent(old, other))

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
        total = Profile.objects.count()
        self.assertEqual(count, -(-total // 2))
        self.assertLess(count, total)
        self.assertEqual(safe.call_count, count)

    def test_range_is_idempotent(self) -> None:
        profiles = [_profile() for _ in range(3)]
        _pins(profiles[0], LA, 4)
        for profile in profiles:
            _weeks(profile, 1)
        lo, hi = min(p.pk for p in profiles), max(p.pk for p in profiles)
        fields = ("profile_id", "pin_count", "pin_cells", "active_weeks_90", "is_real_user")
        first = tasks.refresh_trust_signals_range(lo, hi)
        snapshot = list(ProfileTrustSignals.objects.order_by("profile_id").values_list(*fields))
        second = tasks.refresh_trust_signals_range(lo, hi)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, list(ProfileTrustSignals.objects.order_by("profile_id").values_list(*fields)))

    def test_dormant_row_is_rebuilt_once_the_profile_is_active_again(self) -> None:
        profile = _profile()
        ProfileTrustSignals.objects.create(profile=profile, computed_at=timezone.now(), active_weeks_90=0)
        _weeks(profile, 1)
        self.assertEqual(tasks.refresh_trust_signals_range(profile.pk, profile.pk), 1)

    def test_dormant_profile_with_a_fresh_row_is_skipped(self) -> None:
        profile = _profile()
        ProfileActivityDay.objects.filter(profile=profile).delete()
        ProfileTrustSignals.objects.create(profile=profile, computed_at=timezone.now(), active_weeks_90=0)
        self.assertEqual(tasks.refresh_trust_signals_range(profile.pk, profile.pk), 0)
        ProfileTrustSignals.objects.filter(profile=profile).update(computed_at=timezone.now() - timedelta(days=30))
        self.assertEqual(tasks.refresh_trust_signals_range(profile.pk, profile.pk), 1)

    def test_one_broken_profile_does_not_stop_the_range(self) -> None:
        profiles = [_profile() for _ in range(2)]
        lo, hi = min(p.pk for p in profiles), max(p.pk for p in profiles)
        real = compute_signals

        def flaky(profile: Profile) -> ProfileTrustSignals:
            if profile.pk == lo:
                raise ValueError("bad data")
            return real(profile)

        with patch("urbanlens.dashboard.services.trust.compute_signals", side_effect=flaky):
            self.assertEqual(tasks.refresh_trust_signals_range(lo, hi), 1)

    def test_transient_database_error_escapes_for_retry(self) -> None:
        profile = _profile()
        with (
            patch("urbanlens.dashboard.services.trust.compute_signals", side_effect=OperationalError("timeout")),
            self.assertRaises(OperationalError),
        ):
            tasks.refresh_trust_signals_range(profile.pk, profile.pk)

    def test_final_attempt_isolates_a_persistently_failing_profile(self) -> None:
        bad, good = _profile(), _profile()
        real = compute_signals

        def flaky(profile):
            if profile.pk == bad.pk:
                raise OperationalError("timeout")
            return real(profile)

        with (
            patch("urbanlens.dashboard.services.trust.compute_signals", side_effect=flaky),
            patch.object(type(tasks.refresh_trust_signals_range.request), "retries", 3, create=True),
        ):
            rebuilt = tasks.refresh_trust_signals_range(min(bad.pk, good.pk), max(bad.pk, good.pk))
        self.assertEqual(rebuilt, 1)
        self.assertTrue(ProfileTrustSignals.objects.filter(profile=good).exists())
