"""Tests for the DM `@pin`/`@friend` share fixes: deferred broadcast ordering,
`PinShare.resulting_pin`, and the in-thread (non-redirecting) respond views.

Covers:
- create_direct_message(defer_broadcast=True) doesn't broadcast until
  broadcast_direct_message is called explicitly
- share_pin_in_message / recommend_friend_in_message only broadcast once the
  DirectMessageShare row already exists, so serialize_direct_message's
  has_share flag is correct on the wire (the live share-card/blurred-image bug)
- PinShare.resulting_pin for both the "brand new pin" and "already pinned"
  accept paths
- MessageShareRespondPinView / MessageShareRespondFriendView respond in place
  (200 + re-rendered card), never redirect the recipient out of the thread
"""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.direct_message_shares import recommend_friend_in_message, share_pin_in_message
from urbanlens.dashboard.services.direct_messages import create_direct_message, serialize_direct_message


def _profile() -> Profile:
    return baker.make("auth.User").profile


def _make_accepted_friendship(a: Profile, b: Profile) -> Friendship:
    return Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


def _set_dm_visibility(profile: Profile, visibility: str) -> None:
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=visibility)
    profile.refresh_from_db()


class DeferBroadcastTests(TestCase):
    """create_direct_message(defer_broadcast=True) postpones the live push."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    @patch("urbanlens.dashboard.services.direct_messages._broadcast_direct_message")
    def test_defer_broadcast_skips_the_push(self, mock_broadcast) -> None:
        create_direct_message(self.sender, self.recipient, "hi", defer_broadcast=True)
        mock_broadcast.assert_not_called()

    @patch("urbanlens.dashboard.services.direct_messages._broadcast_direct_message")
    def test_non_deferred_still_broadcasts(self, mock_broadcast) -> None:
        message = create_direct_message(self.sender, self.recipient, "hi")
        mock_broadcast.assert_called_once_with(message)


class SharePinBroadcastOrderingTests(TestCase):
    """share_pin_in_message only broadcasts after the DirectMessageShare exists.

    Broadcasting the plain message first (as the code did before this fix)
    means the WS payload's `has_share` flag is always False - the live
    share card never renders for the recipient without a manual refresh.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None)

    @patch("urbanlens.dashboard.services.direct_message_shares.broadcast_direct_message")
    def test_broadcast_happens_after_share_is_attached(self, mock_broadcast) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "check this out")
        mock_broadcast.assert_called_once()
        broadcast_arg = mock_broadcast.call_args[0][0]
        self.assertEqual(broadcast_arg.pk, message.pk)
        self.assertIsNotNone(getattr(broadcast_arg, "share", None))
        self.assertTrue(serialize_direct_message(broadcast_arg)["has_share"])

    def test_message_without_share_has_no_has_share_flag(self) -> None:
        message = create_direct_message(self.sender, self.recipient, "hi")
        self.assertFalse(serialize_direct_message(message)["has_share"])


class RecommendFriendBroadcastOrderingTests(TestCase):
    """recommend_friend_in_message has the same ordering fix as pin shares."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        self.recommended = _profile()
        _make_accepted_friendship(self.sender, self.recipient)
        _make_accepted_friendship(self.sender, self.recommended)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    @patch("urbanlens.dashboard.services.direct_message_shares.broadcast_direct_message")
    def test_broadcast_happens_after_share_is_attached(self, mock_broadcast) -> None:
        message = recommend_friend_in_message(self.sender, self.recipient, self.recommended, "meet my friend")
        mock_broadcast.assert_called_once()
        broadcast_arg = mock_broadcast.call_args[0][0]
        self.assertEqual(broadcast_arg.pk, message.pk)
        self.assertTrue(serialize_direct_message(broadcast_arg)["has_share"])


class ResultingPinTests(TestCase):
    """PinShare.resulting_pin resolves the recipient-side pin once accepted."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None)

    def test_pending_share_has_no_resulting_pin(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self.assertIsNone(message.share.pin_share.resulting_pin)

    def test_accepted_share_resolves_new_pin(self) -> None:
        from urbanlens.dashboard.controllers.pin_sharing import apply_pin_share_response

        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        pin_share = message.share.pin_share
        target_pin, _msg = apply_pin_share_response(pin_share, "accept")
        pin_share.refresh_from_db()
        self.assertEqual(pin_share.resulting_pin, target_pin)
        self.assertEqual(pin_share.resulting_pin.profile, self.recipient)

    def test_already_pinned_dedup_resolves_existing_pin(self) -> None:
        from urbanlens.dashboard.controllers.pin_sharing import apply_pin_share_response

        existing = baker.make(Pin, profile=self.recipient, parent_pin=None, location=self.pin.location)
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        pin_share = message.share.pin_share
        pin_share.status = PinShareStatus.PENDING  # share_pin_in_message may have marked it already_pinned
        pin_share.save(update_fields=["status"])
        apply_pin_share_response(pin_share, "accept")
        pin_share.refresh_from_db()
        self.assertEqual(pin_share.resulting_pin, existing)


class MessageShareRespondViewTests(TestCase):
    """Accept/reject/friend-request from the DM share card stays in the thread."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        self.sender.ensure_slug()
        self.recipient.ensure_slug()
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None)

    def test_accept_returns_200_not_a_redirect(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self.client.force_login(self.recipient.user)
        response = self.client.post(
            reverse("messages.share.pin.respond", kwargs={"profile_slug": self.sender.slug, "message_id": message.pk}),
            {"action": "accept"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("showToast", response.headers.get("HX-Trigger", ""))
        message.share.pin_share.refresh_from_db()
        self.assertEqual(message.share.pin_share.status, PinShareStatus.ACCEPTED)

    def test_only_recipient_can_respond(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self.client.force_login(self.sender.user)
        response = self.client.post(
            reverse("messages.share.pin.respond", kwargs={"profile_slug": self.recipient.slug, "message_id": message.pk}),
            {"action": "accept"},
        )
        self.assertEqual(response.status_code, 404)

    def test_friend_respond_sends_request_without_redirect(self) -> None:
        recommended = _profile()
        _make_accepted_friendship(self.sender, recommended)
        message = recommend_friend_in_message(self.sender, self.recipient, recommended, "meet them")
        self.client.force_login(self.recipient.user)
        response = self.client.post(
            reverse("messages.share.friend.respond", kwargs={"profile_slug": self.sender.slug, "message_id": message.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Friendship.objects.filter(from_profile=self.recipient, to_profile=recommended).exists()
            or Friendship.objects.filter(from_profile=recommended, to_profile=self.recipient).exists(),
        )


class MessageShareTripScopingTests(TestCase):
    """The trip-invite POST resolves the trip through the caller's own memberships.

    The service layer already refuses to invite anyone to a trip the sender
    isn't a member of (403), but the view used to look the trip up unscoped
    first - so a non-member could distinguish "trip slug exists" (403) from
    "doesn't exist" (404), a slug-probing existence oracle. Both cases must
    be an identical 404 now.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        self.sender.ensure_slug()
        self.recipient.ensure_slug()
        self.client.force_login(self.sender.user)

    def _invite(self, trip_slug: str):
        return self.client.post(
            reverse("messages.share.trip", kwargs={"profile_slug": self.recipient.slug}),
            {"trip_slug": trip_slug},
            HTTP_HX_REQUEST="true",
        )

    def test_member_can_invite_partner_to_their_trip(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership

        trip = Trip.objects.create(name="My Trip", creator=self.sender)
        TripMembership.objects.create(trip=trip, profile=self.sender)

        response = self._invite(trip.slug)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=self.recipient, status=TripMembership.STATUS_INVITED).exists())

    def test_someone_elses_trip_and_a_nonexistent_trip_are_indistinguishable(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership

        outsider = _profile()
        foreign_trip = Trip.objects.create(name="Foreign Trip", creator=outsider)
        TripMembership.objects.create(trip=foreign_trip, profile=outsider)

        foreign_response = self._invite(foreign_trip.slug)
        missing_response = self._invite("no-such-trip-slug")

        self.assertEqual(foreign_response.status_code, 404)
        self.assertEqual(missing_response.status_code, 404)
        self.assertFalse(TripMembership.objects.filter(trip=foreign_trip, profile=self.recipient).exists())


class ShareStatusNotLeakedToSharerTests(TestCase):
    """The sharer must never see the recipient's accept/reject decision in the
    DM thread itself - that's the recipient's own choice to disclose or not.
    _message_share_card.html already gates this on viewer_id == recipient_id;
    this locks the guarantee in with a regression test (there wasn't one)."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        self.sender.ensure_slug()
        self.recipient.ensure_slug()
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None)

    def _accept_as_recipient(self, message: DirectMessage) -> None:
        self.client.force_login(self.recipient.user)
        self.client.post(
            reverse("messages.share.pin.respond", kwargs={"profile_slug": self.sender.slug, "message_id": message.pk}),
            {"action": "accept"},
            HTTP_HX_REQUEST="true",
        )
        self.client.logout()

    def _reject_as_recipient(self, message: DirectMessage) -> None:
        self.client.force_login(self.recipient.user)
        self.client.post(
            reverse("messages.share.pin.respond", kwargs={"profile_slug": self.sender.slug, "message_id": message.pk}),
            {"action": "reject"},
            HTTP_HX_REQUEST="true",
        )
        self.client.logout()

    def test_sharer_does_not_see_accepted_status(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self._accept_as_recipient(message)
        self.client.force_login(self.sender.user)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.recipient.slug}))
        content = response.content.decode()
        self.assertNotIn("Added to your map", content)
        self.assertIn("Pin shared.", content)

    def test_sharer_does_not_see_rejected_status(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self._reject_as_recipient(message)
        self.client.force_login(self.sender.user)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.recipient.slug}))
        content = response.content.decode()
        self.assertNotIn("Declined", content)
        self.assertIn("Pin shared.", content)

    def test_sharer_does_not_see_pending_accept_reject_buttons(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self.client.force_login(self.sender.user)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.recipient.slug}))
        content = response.content.decode()
        self.assertNotContains(response, reverse("messages.share.pin.respond", kwargs={"profile_slug": self.sender.slug, "message_id": message.pk}))
        self.assertIn("Pin shared.", content)

    def test_recipient_does_see_their_own_status(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "hi")
        self._accept_as_recipient(message)
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.sender.slug}))
        self.assertContains(response, "Added to your map")


class ThreadImagePermissionSerializationTests(TestCase):
    """serialize_direct_message carries images_revealed for the live consent path."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    def test_images_revealed_defaults_false(self) -> None:
        message = create_direct_message(self.sender, self.recipient, "hi")
        self.assertFalse(serialize_direct_message(message)["images_revealed"])

    def test_images_revealed_reflected_when_set(self) -> None:
        message = create_direct_message(self.sender, self.recipient, "hi")
        DirectMessage.objects.filter(pk=message.pk).update(images_revealed=True)
        message.refresh_from_db()
        self.assertTrue(serialize_direct_message(message)["images_revealed"])
