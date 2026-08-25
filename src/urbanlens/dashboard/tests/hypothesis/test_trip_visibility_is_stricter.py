"""Pins the ways trip location visibility is stricter than the shared evaluator.

``services/trips/trip_visibility.py`` does not call ``Profile.visibility_permits``.
It buckets activities by the adder's ``trip_pin_location_visibility`` and resolves
each bucket with its own queries, so a whole list costs a fixed number of queries
instead of one evaluator call per activity. In doing so it answers two questions
differently from the canonical evaluator - and both answers are *more* restrictive.

Neither difference leaks anything, so neither is a bug to fix here. The hazard is
the reverse: ``visibility_permits`` describes itself as the one place these
relationship rules live, so a future cleanup could reasonably unify the two and
thereby *reveal* locations that are hidden today - widening access to other
people's data with nothing failing.

These tests exist to make that change deliberate. If the divergence is later
judged wrong, they should be updated in the same commit that loosens it, with the
product decision stated - not deleted to make a refactor pass.

"This activity's location" (divergence 2) means the real-world *Place*, not the
exact Location row - a pin on a different Location sharing the same Place still
counts, matching every other "same place" comparison in the app (see
``services.pins.common_pins.pinned_place_keys``). That is a fix, not a
loosening of the divergence: an unrelated Place is still hidden exactly as
before.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.trips.trip_visibility import viewer_hidden_activity_ids


class TripVisibilityIsStricterTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.adder = Profile.objects.get(user=baker.make("auth.User"))
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.location = baker.make(Location)
        trip = baker.make(Trip, creator=self.adder)
        self.activity = baker.make(TripActivity, trip=trip, location=self.location, added_by=self.adder, location_hidden=False)

    def _set_visibility(self, value: str) -> None:
        Profile.objects.filter(pk=self.adder.pk).update(trip_pin_location_visibility=value)
        self.adder.refresh_from_db()

    def _hidden(self) -> bool:
        return self.activity.id in viewer_hidden_activity_ids([self.activity], self.viewer)

    def _canonical(self) -> bool:
        return Profile.visibility_permits(self.adder.trip_pin_location_visibility, self.adder, self.viewer)

    def test_anyone_is_visible(self) -> None:
        """Anchors the rest: the two agree in the ordinary case."""
        self._set_visibility(VisibilityChoice.ANYONE)

        self.assertFalse(self._hidden())
        self.assertTrue(self._canonical())

    def test_no_one_is_hidden(self) -> None:
        self._set_visibility(VisibilityChoice.NO_ONE)

        self.assertTrue(self._hidden())
        self.assertFalse(self._canonical())

    def test_an_accepted_friend_sees_it(self) -> None:
        self._set_visibility(VisibilityChoice.FRIENDS)
        Friendship.objects.create(from_profile=self.adder, to_profile=self.viewer, status=FriendshipStatus.ACCEPTED)

        self.assertFalse(self._hidden())
        self.assertTrue(self._canonical())

    def test_a_pending_request_does_not_reveal_the_location(self) -> None:
        """Divergence 1: the evaluator would allow this; the trip rule does not."""
        self._set_visibility(VisibilityChoice.FRIENDS)
        Friendship.objects.create(from_profile=self.adder, to_profile=self.viewer, status=FriendshipStatus.REQUESTED)

        self.assertTrue(self._canonical(), "precondition: the shared evaluator honours a pending request")
        self.assertTrue(self._hidden(), "trip visibility began honouring pending requests - this widens access")

    def test_common_pin_means_this_location_not_any_location(self) -> None:
        """Divergence 2: sharing a pin *elsewhere* is not enough here."""
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        elsewhere = baker.make(Location)
        baker.make(Pin, profile=self.adder, location=elsewhere)
        baker.make(Pin, profile=self.viewer, location=elsewhere)

        self.assertTrue(self._canonical(), "precondition: the shared evaluator accepts any common pin")
        self.assertTrue(self._hidden(), "trip visibility began accepting a pin elsewhere - this widens access")

    def test_a_pin_at_this_location_does_reveal_it(self) -> None:
        """The other half of divergence 2, so it can't be satisfied by hiding everything."""
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=self.viewer, location=self.location)

        self.assertFalse(self._hidden())

    def test_a_pin_on_a_different_location_sharing_this_ones_place_also_reveals_it(self) -> None:
        """"This location" means the real-world place, not the exact coordinate
        row - a pin fifty metres away on the same parcel must count, the same
        fix already applied to services.pins.common_pins and
        Profile._have_common_pin. This is *not* a re-widening of divergence 2
        above: `elsewhere` there shares no Place with self.location, so it is
        still correctly hidden - only a genuinely shared real-world place
        narrows the gap. See docs/GOALS_CODE_AUDIT.md
        ("Cross-pin aggregate comparison level")."""
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        self.location.place = place
        self.location.save(update_fields=["place"])
        boundary_mate = baker.make(Location, place=place)
        self.assertNotEqual(boundary_mate.pk, self.location.pk)
        baker.make(Pin, profile=self.viewer, location=boundary_mate)

        self.assertFalse(self._hidden())

    def test_elsewhere_still_means_a_different_place_not_just_a_different_row(self) -> None:
        """Regression guard for divergence 2 itself: giving self.location and
        `elsewhere` unrelated Places (rather than leaving place unset, which
        the other divergence-2 test relies on implicitly) must still hide it."""
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        self.location.place = baker.make(Place, kind=PlaceKind.PARCEL)
        self.location.save(update_fields=["place"])
        elsewhere = baker.make(Location, place=baker.make(Place, kind=PlaceKind.PARCEL))
        baker.make(Pin, profile=self.adder, location=elsewhere)
        baker.make(Pin, profile=self.viewer, location=elsewhere)

        self.assertTrue(self._canonical(), "precondition: the shared evaluator accepts any common pin")
        self.assertTrue(self._hidden(), "a pin at an unrelated place must not reveal this one")

    def test_a_deleted_adder_is_treated_as_most_restrictive(self) -> None:
        self._set_visibility(VisibilityChoice.ANYONE)
        self.activity.added_by = None
        self.activity.save(update_fields=["added_by"])

        self.assertTrue(self._hidden())
