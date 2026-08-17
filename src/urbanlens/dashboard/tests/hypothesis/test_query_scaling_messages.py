"""A conversation must cost a constant number of queries however many shares it holds.

Companion to ``test_query_scaling``, which covers the map pin panel, trips
overview, trips calendar and the external photo list. Message threads were not
covered, and they render the one model property that had no annotation or
prefetch behind it.

``PinShare.resulting_pin`` is read by ``_message_share_card.html`` and
``_group_share_card.html`` for every accepted share in the thread. It queries
twice: ``pins_created.first()``, and - only in the "recipient already had this
place pinned" dedup case - a lookup by location. Both thread querysets already
prefetch ``pin_share``, but neither reached ``pins_created``, so each accepted
share card cost its own query.

The dedup fallback is still per-card and cannot be prefetched away; it only runs
for shares that produced no new pin, so it does not scale with the ordinary case.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile

_FIRST_BATCH = 2
_SECOND_BATCH = 10


class ConversationQueryScalingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = baker.make(User)
        self.recipient: Profile = self.viewer.profile
        self.sender: Profile = baker.make(User).profile
        Profile.objects.filter(pk=self.recipient.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
        self.recipient.refresh_from_db()
        # Pins can only be shared between connected friends.
        Friendship.objects.create(
            from_profile=self.sender,
            to_profile=self.recipient,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        self.client.force_login(self.viewer)

    def _seed_accepted_shares(self, count: int) -> None:
        from urbanlens.dashboard.controllers.pin_sharing import apply_pin_share_response
        from urbanlens.dashboard.services.messaging.direct_message_shares import share_pin_in_message

        for _ in range(count):
            pin = baker.make(Pin, profile=self.sender, parent_pin=None)
            message = share_pin_in_message(self.sender, self.recipient, pin, "take a look")
            apply_pin_share_response(message.share.pin_share, "accept")

    def _count(self, url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
        return len(ctx.captured_queries)

    def test_conversation_does_not_scale_with_share_count(self) -> None:
        url = reverse("messages.conversation", kwargs={"profile_slug": self.sender.slug})

        self._seed_accepted_shares(_FIRST_BATCH)
        small = self._count(url)
        self._seed_accepted_shares(_SECOND_BATCH)
        large = self._count(url)

        self.assertLessEqual(
            large,
            small + 2,
            f"the conversation ran {small} queries for {_FIRST_BATCH} shares and {large} for "
            f"{_FIRST_BATCH + _SECOND_BATCH} - it is querying per share card.",
        )

    def test_the_card_still_resolves_its_pin(self) -> None:
        """The complement: making the lookup prefetchable must not stop it finding the pin."""
        self._seed_accepted_shares(1)

        from urbanlens.dashboard.models.pin_share.model import PinShare

        share = PinShare.objects.filter(to_profile=self.recipient).first()
        self.assertIsNotNone(share)
        self.assertIsNotNone(share.resulting_pin, "an accepted share must still resolve its recipient-side pin")
        self.assertEqual(share.resulting_pin.profile, self.recipient)


class ConversationListQueryScalingTests(TestCase):
    """The sidebar conversation list must not query per conversation.

    Closes a gap left open by the listing survey: ``messages.list`` measured
    "flat" there only because the seed grew pins and labels, not conversations,
    so the list it rendered never changed size. A scaling assertion is only
    worth anything when the seed grows the rows the endpoint lists.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = baker.make(User)
        self.profile: Profile = self.viewer.profile
        self.client.force_login(self.viewer)

    def _seed_conversations(self, count: int) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        for _ in range(count):
            partner = baker.make(User).profile
            DirectMessage.objects.create(sender=partner, recipient=self.profile, body="hello there")
            DirectMessage.objects.create(sender=self.profile, recipient=partner, body="hello back")

    def _count(self, url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
        return len(ctx.captured_queries)

    def test_conversation_list_does_not_scale_with_conversation_count(self) -> None:
        """Was about eleven queries per conversation - 45 for 2, 155 for 12.

        Each row renders ``conv.display_name``/``display_avatar_url``, which call
        ``display_identity_for`` -> ``resolve_visible_identity`` per partner, and
        that re-evaluated the viewer's friendships, trip memberships,
        pins-in-common and temporary DM access every single time.

        ``conversations_for`` now resolves the whole list once through
        ``Profile.visible_profile_pks``. That is a reimplementation of a privacy
        decision, so it is held to the original's answers by
        ``test_identity_visibility_batch`` rather than trusted.
        """
        url = reverse("messages.list")

        self._seed_conversations(_FIRST_BATCH)
        small = self._count(url)
        self._seed_conversations(_SECOND_BATCH)
        large = self._count(url)

        self.assertLessEqual(
            large,
            small + 2,
            f"the conversation list ran {small} queries for {_FIRST_BATCH} conversations and {large} for "
            f"{_FIRST_BATCH + _SECOND_BATCH} - it is querying per conversation.",
        )
