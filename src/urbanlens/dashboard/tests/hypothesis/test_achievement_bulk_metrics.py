"""Bulk metric computation must agree exactly with per-profile computation.

The nightly sweep prices a whole chunk of profiles at a constant number of
grouped queries via ``Metric.compute_bulk``. Every bulk form must return the
same numbers ``Metric.compute`` would have, profile by profile - a bulk form
that drifts silently grants (or withholds) awards for everyone at once. These
tests pin the agreement for every builtin metric, the tricky groupings
(pair-table friendships, distinct visit counting), the missing-pk-reads-zero
contract, and the per-profile fallback for metrics without (or with a broken)
bulk form.
"""

from __future__ import annotations

import datetime
import itertools
from types import SimpleNamespace
from typing import TYPE_CHECKING

from django.utils import timezone
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.achievements.meta import ActivityKind, streak_metric_key
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import ImageSource
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import TripMembership
from urbanlens.dashboard.services.achievements.metrics import Metric, all_metrics, get_metric

if TYPE_CHECKING:
    from collections.abc import Callable

#: Distinct coordinates per baked Location - the table is unique on (lat, lon).
_COORDINATES = itertools.count()


def _profile() -> Profile:
    """Create a fresh profile through the user signal, like production does."""
    return Profile.objects.get(user=baker.make("auth.User"))


def _location():
    coordinate = next(_COORDINATES)
    return baker.make(
        "dashboard.Location",
        latitude=f"{40 + coordinate * 0.0001:.6f}",
        longitude=f"{-73 - coordinate * 0.0001:.6f}",
    )


# ---------------------------------------------------------------------------
# One seeder per builtin metric: make the metric read exactly *value* for
# *profile*, without moving any other profile's number for the same metric.
# ---------------------------------------------------------------------------


def _seed_pins(profile: Profile, value: int) -> None:
    if value:
        baker.make(Pin, profile=profile, _quantity=value)


def _seed_wiki_edits(profile: Profile, value: int) -> None:
    for _ in range(value):
        wiki = baker.make("dashboard.Wiki", location=_location())
        baker.make("dashboard.WikiEdit", editor=profile, wiki=wiki, reverted=False)


def _seed_photos(profile: Profile, value: int) -> None:
    if value:
        baker.make("dashboard.Image", profile=profile, source=ImageSource.UPLOAD, _quantity=value)


def _seed_markup_maps(profile: Profile, value: int) -> None:
    if value:
        baker.make("dashboard.MarkupMap", profile=profile, _quantity=value)


def _seed_visits(profile: Profile, value: int) -> None:
    # One distinct pin per counted visit; repeat visits are covered separately.
    for _ in range(value):
        baker.make("dashboard.PinVisit", pin=baker.make(Pin, profile=profile), tentative=False)


def _seed_reviews(profile: Profile, value: int) -> None:
    host = _profile()
    for _ in range(value):
        baker.make("dashboard.Review", profile=profile, pin=baker.make(Pin, profile=host), rating=4)


def _seed_vulnerability(profile: Profile, value: int) -> None:
    if value:
        baker.make(Pin, profile=profile, vulnerability=3, _quantity=value)


def _seed_danger(profile: Profile, value: int) -> None:
    if value:
        baker.make(Pin, profile=profile, danger=2, _quantity=value)


def _seed_trips_planned(profile: Profile, value: int) -> None:
    if value:
        baker.make("dashboard.Trip", creator=profile, _quantity=value)


def _seed_trips_attended(profile: Profile, value: int) -> None:
    yesterday = timezone.localdate() - datetime.timedelta(days=1)
    host = _profile()
    for _ in range(value):
        trip = baker.make(
            "dashboard.Trip",
            creator=host,
            start_date=yesterday - datetime.timedelta(days=1),
            end_date=yesterday,
        )
        baker.make(
            "dashboard.TripMembership",
            trip=trip,
            profile=profile,
            status=TripMembership.STATUS_JOINED,
            rsvp=TripMembership.RSVP_YES,
        )


def _seed_comments(profile: Profile, value: int) -> None:
    # The metric sums two tables, so seed both.
    host = _profile()
    for index in range(value):
        if index % 2 == 0:
            baker.make("dashboard.Comment", profile=profile, pin=baker.make(Pin, profile=host))
        else:
            baker.make("dashboard.TripComment", author=profile, trip=baker.make("dashboard.Trip", creator=host))


def _seed_friends(profile: Profile, value: int) -> None:
    # Alternate which side of the pair's single row the profile sits on.
    for index in range(value):
        other = _profile()
        from_profile, to_profile = (profile, other) if index % 2 == 0 else (other, profile)
        Friendship.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )


def _seed_invitations(profile: Profile, value: int) -> None:
    if value:
        baker.make("dashboard.FriendInvitation", inviter=profile, accepted_at=timezone.now(), _quantity=value)


def _seed_streak(kind: str) -> Callable[[Profile, int], None]:
    def seed(profile: Profile, value: int) -> None:
        if value:
            baker.make(
                "dashboard.ProfileStreak",
                profile=profile,
                kind=kind,
                current_length=value,
                longest_length=value,
                last_day=timezone.localdate(),
            )

    return seed


SEEDERS: dict[str, Callable[[Profile, int], None]] = {
    "pins_created": _seed_pins,
    "wiki_edits": _seed_wiki_edits,
    "photos_uploaded": _seed_photos,
    "markup_maps_created": _seed_markup_maps,
    "places_visited": _seed_visits,
    "places_rated": _seed_reviews,
    "places_vulnerability_rated": _seed_vulnerability,
    "places_danger_rated": _seed_danger,
    "trips_planned": _seed_trips_planned,
    "trips_attended": _seed_trips_attended,
    "comments_written": _seed_comments,
    "friends": _seed_friends,
    "people_invited": _seed_invitations,
    **{streak_metric_key(kind): _seed_streak(kind) for kind in ActivityKind.values},
}


class BulkAgreementExhaustiveTests(TestCase):
    """Every builtin metric: bulk == per-profile == the seeded ground truth."""

    def test_every_builtin_metric_has_a_bulk_form(self) -> None:
        missing = [key for key in SEEDERS if (metric := get_metric(key)) and metric.compute_bulk is None]
        self.assertEqual(missing, [], "builtin metrics must all carry a bulk form for the sweep")

    def test_bulk_agrees_with_per_profile_for_every_builtin_metric(self) -> None:
        counts = (0, 1, 3)
        for key, seed in SEEDERS.items():
            with self.subTest(metric=key):
                metric = get_metric(key)
                self.assertIsNotNone(metric)
                profiles = [_profile() for _ in counts]
                for profile, count in zip(profiles, counts, strict=True):
                    seed(profile, count)
                expected = {profile.pk: count for profile, count in zip(profiles, counts, strict=True)}

                per_profile = {profile.pk: metric.value_for(profile) for profile in profiles}
                self.assertEqual(per_profile, expected, "seeded ground truth via compute")
                self.assertEqual(metric.values_for_many(profiles), expected, "bulk must agree with compute")


class BulkEdgeCaseTests(TestCase):
    """The groupings that are easy to get wrong in SQL."""

    def test_friendship_between_two_chunk_profiles_credits_both(self) -> None:
        """One shared row joins the pair; both endpoints in one chunk must each count it once."""
        first, second = _profile(), _profile()
        Friendship.objects.create(
            from_profile=first,
            to_profile=second,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )
        metric = get_metric("friends")

        self.assertEqual(metric.values_for_many([first, second]), {first.pk: 1, second.pk: 1})
        self.assertEqual(metric.value_for(first), 1)
        self.assertEqual(metric.value_for(second), 1)

    def test_repeat_visits_to_one_pin_count_once_in_bulk(self) -> None:
        profile = _profile()
        pin = baker.make(Pin, profile=profile)
        baker.make("dashboard.PinVisit", pin=pin, tentative=False, _quantity=3)
        metric = get_metric("places_visited")

        self.assertEqual(metric.values_for_many([profile]), {profile.pk: 1})

    def test_profile_with_no_activity_reads_zero_for_every_bulk_metric(self) -> None:
        """Grouped queries omit empty groups; the mapping must still say 0."""
        profile = _profile()
        for metric in all_metrics():
            if metric.compute_bulk is None:
                continue
            with self.subTest(metric=metric.key):
                self.assertEqual(metric.values_for_many([profile]), {profile.pk: 0})


class BulkAgreementPropertyTests(TestCase):
    """Random small contribution shapes: bulk and per-profile always agree."""

    @given(
        shapes=st.lists(
            st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=2)),
            min_size=1,
            max_size=3,
        ),
    )
    @hypothesis_settings(max_examples=10, deadline=None)
    def test_bulk_agrees_on_random_contribution_shapes(self, shapes: list[tuple[int, int]]) -> None:
        host = _profile()
        host_trip = baker.make("dashboard.Trip", creator=host)

        profiles: list[Profile] = []
        expectations: list[tuple[int, int]] = []
        for pin_count, comment_count in shapes:
            profile = _profile()
            profiles.append(profile)
            expectations.append((pin_count, comment_count))
            if pin_count:
                baker.make(Pin, profile=profile, _quantity=pin_count)
            for index in range(comment_count):
                if index % 2 == 0:
                    baker.make("dashboard.Comment", profile=profile, pin=baker.make(Pin, profile=host))
                else:
                    baker.make("dashboard.TripComment", author=profile, trip=host_trip)

        for key, position in (("pins_created", 0), ("comments_written", 1)):
            metric = get_metric(key)
            expected = {profile.pk: shape[position] for profile, shape in zip(profiles, expectations, strict=True)}
            self.assertEqual(metric.values_for_many(profiles), expected, f"bulk {key}")
            self.assertEqual({p.pk: metric.value_for(p) for p in profiles}, expected, f"per-profile {key}")


class ValuesForManyFallbackTests(SimpleTestCase):
    """Metrics without (or with a broken) bulk form fall back per profile."""

    def _metric(self, **overrides) -> Metric:
        parameters: dict = {"key": "fallback_test", "label": "Fallback", "unit": "x", "description": ""}
        parameters.update(overrides)
        return Metric(**parameters)

    def test_metric_without_bulk_computes_per_profile(self) -> None:
        metric = self._metric(compute=lambda profile: profile.pk * 10)
        profiles = [SimpleNamespace(pk=1), SimpleNamespace(pk=2)]

        self.assertEqual(metric.values_for_many(profiles), {1: 10, 2: 20})

    def test_bulk_failure_falls_back_to_per_profile_instead_of_raising(self) -> None:
        """A broken bulk form must degrade exactly like a broken compute: contained."""

        def explode(_profile_ids):
            raise RuntimeError("boom")

        metric = self._metric(compute=lambda _profile: 5, compute_bulk=explode)

        self.assertEqual(metric.values_for_many([SimpleNamespace(pk=3)]), {3: 5})

    def test_pks_missing_from_the_bulk_result_read_zero(self) -> None:
        metric = self._metric(compute=lambda _profile: 0, compute_bulk=lambda _profile_ids: {})

        self.assertEqual(metric.values_for_many([SimpleNamespace(pk=9)]), {9: 0})
