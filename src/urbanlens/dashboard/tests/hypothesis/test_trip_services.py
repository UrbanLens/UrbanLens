"""Property-based and unit tests for the shared trip services.

These cover the parts whose correctness is structural rather than per-case:
``trip_access.can_perform``'s permission matrix, the marker-numbering rule the
trip map depends on, and ``trip_map.build_trip_map_points``'s invariants.

On ``@given`` and the database: per this repo's CLAUDE.md, Hypothesis drives
*pure logic* only - a ``@given`` method on a DB-backed ``TestCase`` re-enters
pytest's fixture finalizers once per generated example and blows up inside
``_pytest.fixtures``. So the numbering rule is property-tested here against
lightweight activity stand-ins (it is pure given an activity's coordinates,
hidden flag and status), while the DB-backed matrix and map tests enumerate
their domains exhaustively with ``subTest`` - which for a 5x3 permission
matrix is stronger than sampling anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.trips.trip_access import can_perform, get_trip_for_viewer, has_joined, is_organizer
from urbanlens.dashboard.services.trips.trip_activities import compute_activity_index_map
from urbanlens.dashboard.services.trips.trip_errors import TripNotFoundError
from urbanlens.dashboard.services.trips.trip_map import build_trip_map_points
from urbanlens.dashboard.services.trips.trip_membership import resolve_trip_member

#: Every permission level a trip's allow_* fields can hold.
_LEVELS = [Trip.PERM_NONE, Trip.PERM_ORGANIZERS, Trip.PERM_EVERYONE]

#: How a profile can stand relative to a trip.
_RELATIONSHIPS = ["creator", "organizer", "joined", "invited", "stranger"]

#: The three statuses an activity can hold.
_STATUSES = [TripActivity.STATUS_PROPOSED, TripActivity.STATUS_CONFIRMED, TripActivity.STATUS_COMPLETED]


def _expected_can_perform(relationship: str, level: str) -> bool:
    """The permission matrix, restated independently of the implementation.

    Written from the documented rules rather than by reading ``can_perform``,
    so the two have to agree for this to mean anything.

    Args:
        relationship: One of :data:`_RELATIONSHIPS`.
        level: One of :data:`_LEVELS`.

    Returns:
        Whether the profile should be allowed to act.
    """
    if relationship == "creator":
        return True
    # Anyone who has not accepted the invitation can never act, at any level.
    if relationship in {"invited", "stranger"}:
        return False
    if level == Trip.PERM_EVERYONE:
        return True
    if level == Trip.PERM_ORGANIZERS:
        return relationship == "organizer"
    return False


@dataclass
class _StubActivity:
    """The minimal shape ``compute_activity_index_map`` reads off an activity.

    Standing in for a real ``TripActivity`` keeps the numbering property test
    pure, so Hypothesis can drive it without a database (see the module
    docstring).
    """

    id: int
    lat_override: float | None
    lng_override: float | None
    location_hidden: bool
    status: str
    #: ``activity_coords`` consults these before falling back to the override.
    pin: None = None
    location: None = None


class ActivityIndexNumberingPropertyTests(SimpleTestCase):
    """The map's marker numbers are contiguous from 1, whatever gets skipped."""

    @given(
        specs=st.lists(
            st.tuples(st.booleans(), st.booleans(), st.sampled_from(_STATUSES)),
            max_size=8,
        ),
    )
    @settings(max_examples=200, deadline=None)
    def test_indices_are_contiguous_from_one(self, specs: list[tuple[bool, bool, str]]) -> None:
        """Skipped activities never leave a gap in the numbering."""
        activities = [
            _StubActivity(
                id=index,
                lat_override=40.0 + index if has_coords else None,
                lng_override=-80.0 - index if has_coords else None,
                location_hidden=hidden,
                status=status,
            )
            for index, (has_coords, hidden, status) in enumerate(specs)
        ]
        index_map = compute_activity_index_map(activities)
        self.assertEqual(sorted(index_map.values()), list(range(1, len(index_map) + 1)))

    @given(
        specs=st.lists(
            st.tuples(st.booleans(), st.booleans(), st.sampled_from(_STATUSES)),
            max_size=8,
        ),
    )
    @settings(max_examples=200, deadline=None)
    def test_only_visible_uncompleted_located_activities_are_numbered(self, specs: list[tuple[bool, bool, str]]) -> None:
        """Exactly the activities that earn a marker get one."""
        activities = [
            _StubActivity(
                id=index,
                lat_override=40.0 + index if has_coords else None,
                lng_override=-80.0 - index if has_coords else None,
                location_hidden=hidden,
                status=status,
            )
            for index, (has_coords, hidden, status) in enumerate(specs)
        ]
        index_map = compute_activity_index_map(activities)
        expected = {act.id for act in activities if act.lat_override is not None and not act.location_hidden and act.status != TripActivity.STATUS_COMPLETED}
        self.assertEqual(set(index_map), expected)

    @given(
        specs=st.lists(
            st.tuples(st.booleans(), st.booleans(), st.sampled_from(_STATUSES)),
            max_size=8,
        ),
    )
    @settings(max_examples=200, deadline=None)
    def test_numbering_follows_itinerary_order(self, specs: list[tuple[bool, bool, str]]) -> None:
        """Marker numbers ascend in the order the activities were given."""
        activities = [
            _StubActivity(
                id=index,
                lat_override=40.0 + index if has_coords else None,
                lng_override=-80.0 - index if has_coords else None,
                location_hidden=hidden,
                status=status,
            )
            for index, (has_coords, hidden, status) in enumerate(specs)
        ]
        index_map = compute_activity_index_map(activities)
        numbered_ids = [act.id for act in activities if act.id in index_map]
        self.assertEqual([index_map[act_id] for act_id in numbered_ids], list(range(1, len(numbered_ids) + 1)))


class CanPerformMatrixTests(TestCase):
    """``can_perform`` must agree with the documented matrix for every combination."""

    def setUp(self) -> None:
        """Create a trip plus one profile in each possible relationship to it."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.creator = Profile.objects.get(user=baker.make(User, username="creator"))
        self.trip = Trip.objects.create(creator=self.creator, name="Matrix")
        TripMembership.objects.create(trip=self.trip, profile=self.creator, status=TripMembership.STATUS_JOINED)

        self.profiles: dict[str, Profile] = {"creator": self.creator}
        for name, status, organizer in [
            ("organizer", TripMembership.STATUS_JOINED, True),
            ("joined", TripMembership.STATUS_JOINED, False),
            ("invited", TripMembership.STATUS_INVITED, False),
        ]:
            profile = Profile.objects.get(user=baker.make(User, username=name))
            TripMembership.objects.create(trip=self.trip, profile=profile, status=status, is_organizer=organizer)
            self.profiles[name] = profile
        self.profiles["stranger"] = Profile.objects.get(user=baker.make(User, username="stranger"))

    def test_the_whole_matrix(self) -> None:
        """Every (relationship, level) pair matches the independently stated rule."""
        for relationship, level in itertools.product(_RELATIONSHIPS, _LEVELS):
            with self.subTest(relationship=relationship, level=level):
                self.assertEqual(
                    can_perform(self.profiles[relationship], self.trip, level),
                    _expected_can_perform(relationship, level),
                )

    def test_an_invited_member_never_acts_regardless_of_level(self) -> None:
        """The join gate sits above the level check, not beside it."""
        for level in _LEVELS:
            with self.subTest(level=level):
                self.assertFalse(can_perform(self.profiles["invited"], self.trip, level))

    def test_has_joined_and_is_organizer_agree_with_the_membership_rows(self) -> None:
        """The two cheaper predicates match the fixture they were built from."""
        self.assertTrue(has_joined(self.profiles["creator"], self.trip))
        self.assertTrue(has_joined(self.profiles["joined"], self.trip))
        self.assertFalse(has_joined(self.profiles["invited"], self.trip))
        self.assertFalse(has_joined(self.profiles["stranger"], self.trip))
        self.assertTrue(is_organizer(self.profiles["creator"], self.trip))
        self.assertTrue(is_organizer(self.profiles["organizer"], self.trip))
        self.assertFalse(is_organizer(self.profiles["joined"], self.trip))


class TripAccessLookupTests(TestCase):
    """``get_trip_for_viewer`` must not distinguish "missing" from "not yours"."""

    def setUp(self) -> None:
        """Create a trip owned by someone other than the viewer."""
        baker.make(User)
        self.owner = Profile.objects.get(user=baker.make(User, username="owner"))
        self.outsider = Profile.objects.get(user=baker.make(User, username="outsider"))
        self.trip = Trip.objects.create(creator=self.owner, name="Private")
        TripMembership.objects.create(trip=self.trip, profile=self.owner, status=TripMembership.STATUS_JOINED)

    def test_missing_and_forbidden_raise_the_same_error_and_message(self) -> None:
        """Regression guard for the trip-slug enumeration leak."""
        with self.assertRaises(TripNotFoundError) as missing:
            get_trip_for_viewer("no-such-slug", self.outsider)
        with self.assertRaises(TripNotFoundError) as forbidden:
            get_trip_for_viewer(self.trip.slug, self.outsider)
        self.assertEqual(missing.exception.message, forbidden.exception.message)

    def test_creator_and_members_may_see_it(self) -> None:
        """The viewing gate is membership, not contribution rights."""
        invited = Profile.objects.get(user=baker.make(User, username="invited"))
        TripMembership.objects.create(trip=self.trip, profile=invited, status=TripMembership.STATUS_INVITED)
        self.assertEqual(get_trip_for_viewer(self.trip.slug, self.owner), self.trip)
        self.assertEqual(get_trip_for_viewer(self.trip.slug, invited), self.trip)


class ResolveTripMemberTests(TestCase):
    """Member lookups are bounded by the trip's own roster."""

    def setUp(self) -> None:
        """Create a trip with one member, plus an unrelated profile."""
        baker.make(User)
        self.owner = Profile.objects.get(user=baker.make(User, username="owner"))
        self.member = Profile.objects.get(user=baker.make(User, username="member"))
        self.outsider = Profile.objects.get(user=baker.make(User, username="outsider"))
        self.trip = Trip.objects.create(creator=self.owner, name="Roster")
        TripMembership.objects.create(trip=self.trip, profile=self.owner, status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=self.trip, profile=self.member, status=TripMembership.STATUS_JOINED)

    def test_resolves_a_member_by_slug_uuid_and_id(self) -> None:
        """All three handles reach the same person."""
        self.assertEqual(resolve_trip_member(self.trip, slug=self.member.slug), self.member)
        self.assertEqual(resolve_trip_member(self.trip, slug=str(self.member.uuid)), self.member)
        self.assertEqual(resolve_trip_member(self.trip, profile_id=self.member.pk), self.member)

    def test_resolves_the_creator_even_without_a_membership_row(self) -> None:
        """The creator is always addressable, membership row or not."""
        TripMembership.objects.filter(trip=self.trip, profile=self.owner).delete()
        self.assertEqual(resolve_trip_member(self.trip, slug=self.owner.slug), self.owner)

    def test_a_real_profile_off_the_trip_is_not_found(self) -> None:
        """Regression guard for the global profile-enumeration defect."""
        for kwargs in ({"slug": self.outsider.slug}, {"profile_id": self.outsider.pk}, {"slug": str(self.outsider.uuid)}):
            with self.subTest(**kwargs), self.assertRaises(TripNotFoundError):
                resolve_trip_member(self.trip, **kwargs)

    def test_a_nonexistent_handle_is_not_found(self) -> None:
        """A missing profile and an off-trip one are indistinguishable."""
        with self.assertRaises(TripNotFoundError):
            resolve_trip_member(self.trip, slug="definitely-not-a-profile")


class BuildTripMapPointsInvariantTests(TestCase):
    """Structural invariants of the shared map point set."""

    def setUp(self) -> None:
        """Create a viewer and an empty trip to hang activities on."""
        baker.make(User)
        self.viewer = Profile.objects.get(user=baker.make(User, username="viewer"))
        self.trip = Trip.objects.create(creator=self.viewer, name="Invariants")
        TripMembership.objects.create(trip=self.trip, profile=self.viewer, status=TripMembership.STATUS_JOINED)
        # Coordinates are unique per Location, so every activity a test creates
        # must sit somewhere new - reusing a point raises IntegrityError, which
        # would poison the surrounding transaction for the rest of the test.
        self._coords = itertools.count()

    def _add_activity(self, *, order: int, located: bool, hidden: bool, status: str) -> TripActivity:
        """Add one activity to the fixture trip at a never-before-used point."""
        location = None
        if located:
            offset = next(self._coords)
            location = Location.objects.create(latitude=40 + offset * 0.01, longitude=-80 - offset * 0.01, official_name=f"Place {offset}")
        return TripActivity.objects.create(
            trip=self.trip,
            location=location,
            added_by=self.viewer,
            title=f"Stop {order}",
            status=status,
            location_hidden=hidden,
            order=order,
        )

    def test_indices_stay_contiguous_across_every_skip_combination(self) -> None:
        """Exhaustive over one activity of each kind: numbering never gaps.

        The pure numbering rule is property-tested separately in
        :class:`ActivityIndexNumberingPropertyTests`; this is the end-to-end
        confirmation that the real query path produces the same thing.
        """
        for located, hidden, status in itertools.product([True, False], [True, False], _STATUSES):
            with self.subTest(located=located, hidden=hidden, status=status):
                self.trip.activities.all().delete()
                # One "ordinary" visible stop plus one of the varied kind, so a
                # skip has something to leave a gap in.
                self._add_activity(order=0, located=True, hidden=False, status=TripActivity.STATUS_PROPOSED)
                self._add_activity(order=1, located=located, hidden=hidden, status=status)
                points = build_trip_map_points(self.trip, self.viewer)
                numbered = [point["index"] for point in points if point["index"] is not None]
                self.assertEqual(numbered, list(range(1, len(numbered) + 1)))

    def test_every_numbered_point_is_draggable_and_carries_its_activity_id(self) -> None:
        """A numbered marker is one of this trip's own stops."""
        self._add_activity(order=0, located=True, hidden=False, status=TripActivity.STATUS_PROPOSED)
        self._add_activity(order=1, located=True, hidden=False, status=TripActivity.STATUS_CONFIRMED)
        for point in build_trip_map_points(self.trip, self.viewer):
            if point["index"] is not None:
                self.assertTrue(point["draggable"])
                self.assertIsNotNone(point["activity_id"])

    def test_hidden_and_uncoordinated_activities_produce_no_point(self) -> None:
        """Skipping is total - not a point with null coordinates."""
        self._add_activity(order=0, located=True, hidden=True, status=TripActivity.STATUS_PROPOSED)
        self._add_activity(order=1, located=False, hidden=False, status=TripActivity.STATUS_PROPOSED)
        self.assertEqual(build_trip_map_points(self.trip, self.viewer), [])

    def test_completed_activities_appear_only_with_include_past(self) -> None:
        """The past is opt-in, and opting in does not break the numbering."""
        self._add_activity(order=0, located=True, hidden=False, status=TripActivity.STATUS_COMPLETED)
        self._add_activity(order=1, located=True, hidden=False, status=TripActivity.STATUS_PROPOSED)
        without_past = build_trip_map_points(self.trip, self.viewer)
        with_past = build_trip_map_points(self.trip, self.viewer, include_past=True)
        self.assertEqual(len(without_past), 1)
        self.assertEqual(len(with_past), 2)
        self.assertEqual([point["index"] for point in with_past], [1, 2])

    def test_a_child_trips_stops_are_ghost_markers(self) -> None:
        """Nested trips contribute context markers, once each, never numbered."""
        child = Trip.objects.create(creator=self.viewer, name="Side quest")
        TripMembership.objects.create(trip=child, profile=self.viewer, status=TripMembership.STATUS_JOINED)
        TripActivity.objects.create(
            trip=child,
            location=Location.objects.create(latitude=41.0, longitude=-81.0, official_name="Child place"),
            added_by=self.viewer,
            title="Child stop",
        )
        TripActivity.objects.create(trip=self.trip, added_by=self.viewer, title="Nested", child_trip=child)
        # Two activities linking the same child trip must not double its markers.
        TripActivity.objects.create(trip=self.trip, added_by=self.viewer, title="Nested again", child_trip=child)

        points = build_trip_map_points(self.trip, self.viewer)
        ghosts = [point for point in points if point.get("child_trip")]
        self.assertEqual(len(ghosts), 1)
        self.assertIsNone(ghosts[0]["index"])
        self.assertIsNone(ghosts[0]["activity_id"])
        self.assertFalse(ghosts[0]["draggable"])
        self.assertIn("Side quest", ghosts[0]["label"])
