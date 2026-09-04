"""Tests for UL-228: linking a "child trip" to a trip activity.

A TripActivity can optionally link a whole separate Trip (child_trip);
that trip's own activities then render as read-only "ghost markers" on
the parent trip's map, labeled "[child trip name] activity title".

TripChildTripSearchView (the autocomplete backing the picker) already
scoped suggestions to Trip.objects.filter(profiles=profile) - trips the
linking user actually belongs to. But TripActivitiesView.post/
TripActivityUpdateView.post resolved child_trip_uuid with no such scoping
at all, so any authenticated trip member could link an arbitrary trip
(one they have no access to) by crafting the POST directly, and that
trip's activity titles/coordinates/schedule would then render for every
member of the trip they *do* have access to. Separately,
TripMapDataView's ghost-marker loop never checked the child activity's
own location_hidden flag, unlike the identical check already applied to
the parent trip's own activities a few lines above it - so a
legitimately-linked child trip's "Secret Location" activities leaked
their real coordinates anyway.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership


class ChildTripLinkingScopeTests(TestCase):
    """child_trip_uuid must only resolve to a trip the acting user belongs to."""

    def setUp(self) -> None:
        super().setUp()
        _, self.profile = self._make_user()
        self.trip = Trip.objects.create(name="My Trip", creator=self.profile)
        TripMembership.objects.create(trip=self.trip, profile=self.profile)
        self.client.force_login(self.profile.user)

    @staticmethod
    def _make_user():
        user = baker.make("auth.User")
        return user, user.profile

    def test_cannot_link_a_trip_the_user_is_not_a_member_of(self) -> None:
        _, other_profile = self._make_user()
        victim_trip = Trip.objects.create(name="Someone Else's Trip", creator=other_profile)
        TripMembership.objects.create(trip=victim_trip, profile=other_profile)

        response = self.client.post(
            reverse("trips.activities", args=[self.trip.slug]),
            data={"title": "Linked activity", "child_trip_uuid": str(victim_trip.uuid)},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        activity = TripActivity.objects.get(trip=self.trip, title="Linked activity")
        self.assertIsNone(activity.child_trip_id)

    def test_can_link_a_trip_the_user_is_a_member_of(self) -> None:
        own_other_trip = Trip.objects.create(name="My Other Trip", creator=self.profile)
        TripMembership.objects.create(trip=own_other_trip, profile=self.profile)

        response = self.client.post(
            reverse("trips.activities", args=[self.trip.slug]),
            data={"title": "Linked activity", "child_trip_uuid": str(own_other_trip.uuid)},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        activity = TripActivity.objects.get(trip=self.trip, title="Linked activity")
        self.assertEqual(activity.child_trip_id, own_other_trip.id)

    def test_edit_cannot_link_a_trip_the_user_is_not_a_member_of(self) -> None:
        activity = TripActivity.objects.create(trip=self.trip, added_by=self.profile, title="Existing")
        _, other_profile = self._make_user()
        victim_trip = Trip.objects.create(name="Someone Else's Trip", creator=other_profile)
        TripMembership.objects.create(trip=victim_trip, profile=other_profile)

        self.client.post(
            reverse("trips.activity.edit", args=[self.trip.slug, activity.id]),
            data={"title": "Existing", "child_trip_uuid": str(victim_trip.uuid)},
            content_type="application/json",
        )

        activity.refresh_from_db()
        self.assertIsNone(activity.child_trip_id)


class ChildTripGhostMarkerVisibilityTests(TestCase):
    """TripMapDataView's child-trip ghost markers must respect location_hidden."""

    def setUp(self) -> None:
        super().setUp()
        user = baker.make("auth.User")
        self.profile = user.profile
        self.client.force_login(user)
        self.trip = Trip.objects.create(name="Parent Trip", creator=self.profile)
        TripMembership.objects.create(trip=self.trip, profile=self.profile)
        self.child_trip = Trip.objects.create(name="Child Trip", creator=self.profile)
        TripMembership.objects.create(trip=self.child_trip, profile=self.profile)

        self.linking_location = baker.make(Location, official_name="Linking Spot", latitude=40.0, longitude=-74.0)
        self.linking_activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.profile,
            title="Link to child trip",
            location=self.linking_location,
            child_trip=self.child_trip,
        )

    def _child_activity(self, *, location_hidden: bool, name: str) -> TripActivity:
        location = baker.make(Location, official_name=name, latitude=41.0, longitude=-75.0)
        return TripActivity.objects.create(
            trip=self.child_trip,
            added_by=self.profile,
            title=name,
            location=location,
            location_hidden=location_hidden,
        )

    def test_visible_child_activity_appears_as_a_ghost_marker(self) -> None:
        self._child_activity(location_hidden=False, name="Visible Child Stop")

        response = self.client.get(reverse("trips.map_data", args=[self.trip.slug]))

        self.assertEqual(response.status_code, 200)
        labels = [p["label"] for p in response.json()["points"]]
        self.assertTrue(any("Visible Child Stop" in label for label in labels))

    def test_hidden_child_activity_is_excluded_from_ghost_markers(self) -> None:
        self._child_activity(location_hidden=True, name="Secret Child Stop")

        response = self.client.get(reverse("trips.map_data", args=[self.trip.slug]))

        self.assertEqual(response.status_code, 200)
        labels = [p["label"] for p in response.json()["points"]]
        self.assertFalse(any("Secret Child Stop" in label for label in labels))

    def test_child_activity_hidden_only_by_adders_privacy_setting_is_also_excluded(self) -> None:
        """Ghost markers must respect trip_pin_location_visibility, not just location_hidden.

        Regression for build_trip_map_points checking only location_hidden for child-trip
        activities - the same viewer-aware gate the parent trip's own activities already get
        (via viewer_hidden_activity_ids) was missing here, so an activity the adder never
        flagged location_hidden, but whose visibility setting excludes this viewer, still
        rendered a real marker with real coordinates. See docs/audits/GOALS_CODE_AUDIT.md
        ("Trip activities sourcing").
        """
        other_user = baker.make("auth.User")
        other_profile = other_user.profile
        other_profile.trip_pin_location_visibility = VisibilityChoice.NO_ONE
        other_profile.save(update_fields=["trip_pin_location_visibility"])
        TripMembership.objects.create(trip=self.child_trip, profile=other_profile)
        location = baker.make(Location, official_name="Privacy-Restricted Stop", latitude=41.5, longitude=-75.5)
        TripActivity.objects.create(
            trip=self.child_trip,
            added_by=other_profile,
            title="Privacy-Restricted Stop",
            location=location,
            location_hidden=False,
        )

        response = self.client.get(reverse("trips.map_data", args=[self.trip.slug]))

        self.assertEqual(response.status_code, 200)
        labels = [p["label"] for p in response.json()["points"]]
        self.assertFalse(any("Privacy-Restricted Stop" in label for label in labels))

    def test_child_trip_activities_are_excluded_when_viewer_is_not_a_child_trip_member(self) -> None:
        """A parent-trip viewer with no membership in the linked child trip must
        not see any of its ghost markers, regardless of the adder's own
        trip_pin_location_visibility setting.

        Regression: build_trip_map_points only ever checked
        viewer_hidden_activity_ids (the *adder's* privacy setting) for child
        activities - never whether this viewer belongs to the child trip at
        all. So linking a private child trip into a broadly-joined parent trip
        exposed every one of its real coordinates/titles to every parent-trip
        member, none of whom the child trip's own members ever agreed to share
        with.
        """
        outsider_user = baker.make("auth.User")
        outsider = outsider_user.profile
        TripMembership.objects.create(trip=self.trip, profile=outsider)
        # Deliberately NOT a member of self.child_trip.
        self._child_activity(location_hidden=False, name="Private Child Stop")

        self.client.force_login(outsider_user)
        response = self.client.get(reverse("trips.map_data", args=[self.trip.slug]))

        # Still 200 - the outsider IS a legitimate viewer of the *parent* trip.
        self.assertEqual(response.status_code, 200)
        labels = [p["label"] for p in response.json()["points"]]
        self.assertFalse(any("Private Child Stop" in label for label in labels))
