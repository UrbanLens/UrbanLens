"""What the photo-visibility gate answers today, pinned before its internals move.

`ImageQuerySet.visible_to` is the gate behind every photo the application serves,
and N21 H44 is a reason to change how it resolves: authorizing one media file
costs 15 queries, and a gallery tile is its own HTTP request, so a 30-photo
gallery is ~450. The narrowing that fixes that is safe in principle - for one
image there is exactly one uploader, so most of the viewer's scope is resolved
and then unused - but it is a rewrite of a gate's internals, and the failure mode
of getting it wrong is someone seeing a photo they should not.

So this pins the answer first. Every expectation here was **measured** against the
current implementation rather than predicted from reading it, then checked for
whether it makes sense; two of them were not what reading suggested.

The interaction worth knowing, because it dominates the table: the container gate
and the settings are independent, and the container gate comes first. A photo is
reachable only when the viewer can reach the wiki it was shared into, which is
earned by pinning that place. With no reach, **no** combination of settings makes
a photo visible - the whole 7x7 table is False. That is why `no_reach` is here as
its own scenario rather than folded in as a special case.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership

ANYONE = VisibilityChoice.ANYONE.value
IN_COMMON = VisibilityChoice.ANYTHING_IN_COMMON.value
COMMON_PIN = VisibilityChoice.COMMON_PIN.value
COMMON_TRIP = VisibilityChoice.COMMON_TRIP.value
NO_ONE = VisibilityChoice.NO_ONE.value

_ALL = [choice.value for choice in VisibilityChoice]
_EXCEPT_NO_ONE = [value for value in _ALL if value != NO_ONE]


def _pairs(uploader_settings: list[str], viewer_settings: list[str]) -> set[tuple[str, str]]:
    """Every (uploader setting, viewer filter) pair drawn from the two lists."""
    return {(uploader, viewer) for uploader in uploader_settings for viewer in viewer_settings}


class _MatrixCase(TestCase):
    """One uploader, one viewer, one photo on a wiki; relationship varies per subclass."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile
        self.uploader = baker.make(User).profile
        self.location = baker.make_recipe("dashboard.location", latitude=Decimal("40.0"), longitude=Decimal("-74.0"))
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        self.image = baker.make(
            Image, profile=self.uploader, wiki=self.wiki, image="pin_images/a.jpg", pending_scan=False
        )

    def _grant_reach(self) -> None:
        """Pinning the place is what earns access to its wiki."""
        baker.make_recipe("dashboard.pin", profile=self.viewer, location=self.location)

    def _visible_pairs(self) -> set[tuple[str, str]]:
        """Every settings pair under which the viewer can currently see the photo."""
        visible: set[tuple[str, str]] = set()
        for uploader_setting in _ALL:
            for viewer_setting in _ALL:
                Profile.objects.filter(pk=self.uploader.pk).update(photo_upload_visibility=uploader_setting)
                Profile.objects.filter(pk=self.viewer.pk).update(viewer_photo_filter=viewer_setting)
                # Re-read so no per-instance memo from a previous pair leaks in.
                viewer = Profile.objects.get(pk=self.viewer.pk)
                if Image.objects.filter(pk=self.image.pk).visible_to(viewer).exists():
                    visible.add((uploader_setting, viewer_setting))
        return visible

    def assertMatrix(self, expected: set[tuple[str, str]]) -> None:
        actual = self._visible_pairs()
        self.assertEqual(
            actual,
            expected,
            f"visibility matrix moved: {len(actual - expected)} newly visible, {len(expected - actual)} newly hidden",
        )


class WithoutReachNothingIsVisibleTests(_MatrixCase):
    def test_no_setting_grants_a_photo_the_viewer_cannot_reach(self) -> None:
        """The container gate is first and absolute - all 49 combinations are False."""
        self.assertMatrix(set())


class WithReachButNoRelationshipTests(_MatrixCase):
    """The viewer pinned the place; the uploader has no pins, so nothing is "in common"."""

    def test_only_anyone_on_both_sides_grants_it(self) -> None:
        self._grant_reach()

        self.assertMatrix({(ANYONE, ANYONE)})


class WithACommonPinTests(_MatrixCase):
    def test_the_three_settings_a_common_pin_satisfies_on_each_side(self) -> None:
        self._grant_reach()
        baker.make_recipe("dashboard.pin", profile=self.uploader, location=self.location)

        self.assertMatrix(_pairs([ANYONE, IN_COMMON, COMMON_PIN], [ANYONE, IN_COMMON, COMMON_PIN]))


class WithACommonTripTests(_MatrixCase):
    def test_the_three_settings_a_common_trip_satisfies_on_each_side(self) -> None:
        self._grant_reach()
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer, status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=trip, profile=self.uploader, status=TripMembership.STATUS_JOINED)

        self.assertMatrix(_pairs([ANYONE, IN_COMMON, COMMON_TRIP], [ANYONE, IN_COMMON, COMMON_TRIP]))


class BetweenFriendsTests(_MatrixCase):
    def test_friendship_satisfies_every_setting_except_no_one(self) -> None:
        """ "Accepted friends qualify for every option except NO_ONE" - asserted, not assumed."""
        self._grant_reach()
        Friendship.objects.create(from_profile=self.viewer, to_profile=self.uploader, status=FriendshipStatus.ACCEPTED)

        self.assertMatrix(_pairs(_EXCEPT_NO_ONE, _EXCEPT_NO_ONE))


class TheUploaderAlwaysSeesTheirOwnTests(_MatrixCase):
    def test_own_photos_ignore_both_settings_entirely(self) -> None:
        """Including NO_ONE on both sides: the setting is about other people."""
        self._grant_reach()
        Image.objects.filter(pk=self.image.pk).update(profile=self.viewer)

        self.assertMatrix(_pairs(_ALL, _ALL))


class ScanningStillGatesEverythingTests(_MatrixCase):
    """`pending_scan` is orthogonal to the matrix and must stay that way."""

    def test_an_unscanned_photo_is_hidden_from_a_friend_under_every_setting(self) -> None:
        self._grant_reach()
        Friendship.objects.create(from_profile=self.viewer, to_profile=self.uploader, status=FriendshipStatus.ACCEPTED)
        Image.objects.filter(pk=self.image.pk).update(pending_scan=True)

        self.assertMatrix(set())

    def test_but_the_uploader_still_sees_their_own_while_it_scans(self) -> None:
        """The owner watches their own upload go from "processing" to visible."""
        self._grant_reach()
        Image.objects.filter(pk=self.image.pk).update(profile=self.viewer, pending_scan=True)

        self.assertMatrix(_pairs(_ALL, _ALL))
