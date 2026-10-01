"""Adding a person by username must refuse someone the adder may not see exactly as it refuses no one."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.social.friendship import block_profile
from urbanlens.dashboard.services.trips.trip_membership import add_member_by_username
from urbanlens.dashboard.services.visits.safety import invite_checkin_partner

NOBODY = "nobody_holds_this"


def _profile(username: str, *, active: bool = True, visibility: str = VisibilityChoice.ANYONE) -> Profile:
    user = baker.make(User, username=username, is_active=active)
    Profile.objects.filter(user=user).update(profile_visibility=visibility)
    return Profile.objects.get(user=user)


def _refusal(call, username: str) -> type[Exception] | None:
    """The exception adding ``username`` raises; callers build the user-facing text from its type and the typed name."""
    try:
        call(username)
    except Exception as exc:  # noqa: BLE001  # the type is what is compared
        return type(exc)
    return None


class AddByUsernameHidesProfilesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is promoted to site admin
        self.adder = _profile("the_adder")
        blocker = _profile("the_blocker")
        block_profile(blocker, self.adder)
        self.hidden = {
            "not visible to the adder": _profile("friends_only", visibility=VisibilityChoice.FRIENDS).username,
            "has blocked the adder": blocker.username,
            "inactive account": _profile("pending_signup", active=False).username,
        }
        self.visible = _profile("open_book")

    def _assert_refused_like_nobody(self, call) -> None:
        expected = _refusal(call, NOBODY)
        self.assertIsNotNone(expected)
        for case, username in self.hidden.items():
            with self.subTest(case=case):
                self.assertEqual(_refusal(call, username), expected)

    def test_trip_member_add(self) -> None:
        trip = Trip.objects.create(name="Ramble", creator=self.adder)
        TripMembership.objects.get_or_create(
            trip=trip, profile=self.adder, defaults={"status": TripMembership.STATUS_JOINED}
        )

        self._assert_refused_like_nobody(lambda username: add_member_by_username(trip, self.adder, username))
        self.assertTrue(add_member_by_username(trip, self.adder, self.visible.username)[1])

    def test_safety_partner_invite(self) -> None:
        def invite(username: str):
            checkin = baker.make(
                "dashboard.SafetyCheckin",
                profile=self.adder,
                title="Hike",
                checkin_by=timezone.now() + datetime.timedelta(hours=2),
                grace_period=datetime.timedelta(hours=1),
            )
            return invite_checkin_partner(checkin, inviter=self.adder, username=username)

        self._assert_refused_like_nobody(invite)
        self.assertEqual(invite(self.visible.username).profile, self.visible)
