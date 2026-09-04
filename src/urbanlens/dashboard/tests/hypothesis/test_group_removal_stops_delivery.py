"""A removed group member must stop receiving messages over the live channel.

`broadcast_group_message` resolves ``active_memberships()`` at send time and
addresses each member's own per-profile channel group, rather than pushing one
payload to a shared per-group channel. That is what makes removal effective
immediately: a member removed while their socket is open is simply not in the
recipient list any more, so nothing further is addressed to them.

It is also the expensive option, and the docstring says so - the payload is built
once per member so a masked display name is resolved through each viewer's own
visibility. The obvious optimisation is a single shared payload to one group
channel, which would silently reintroduce delivery to anyone still connected.
This is exactly the bug found in the safety check-in chat, where a revoked
emergency contact kept receiving over an already-open socket.

So this pins the recipient set rather than the mechanism: whatever the transport
becomes, a removed member must not be addressed.
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.messaging.direct_messages import direct_message_group_name
from urbanlens.dashboard.services.messaging.group_chats import (
    create_group_chat,
    create_group_message,
    remove_group_member,
)


class GroupRemovalStopsDeliveryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.creator = Profile.objects.get(user=baker.make("auth.User"))
        self.stayer = Profile.objects.get(user=baker.make("auth.User"))
        self.leaver = Profile.objects.get(user=baker.make("auth.User"))
        # Group creation enforces each member's direct_message_visibility; these
        # profiles are strangers, so open it up rather than wiring friendships.
        Profile.objects.filter(pk__in=[self.creator.pk, self.stayer.pk, self.leaver.pk]).update(
            direct_message_visibility=VisibilityChoice.ANYONE,
        )
        for profile in (self.creator, self.stayer, self.leaver):
            profile.refresh_from_db()
        self.group = create_group_chat(self.creator, "Quarry crew", [self.stayer, self.leaver])

    def _recipients(self) -> set[str]:
        """Channel groups addressed when the creator posts a message."""
        with (
            patch("urbanlens.dashboard.services.messaging.group_chats.send_group_message") as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            create_group_message(self.creator, self.group, "anyone there?")
        return {call.args[0] for call in send.call_args_list}

    def test_every_active_member_is_addressed(self) -> None:
        """Anchors the rest: delivery works before anyone is removed."""
        recipients = self._recipients()

        self.assertIn(direct_message_group_name(self.stayer.pk), recipients)
        self.assertIn(direct_message_group_name(self.leaver.pk), recipients)

    def test_a_removed_member_is_no_longer_addressed(self) -> None:
        remove_group_member(self.group, self.creator, self.leaver)

        recipients = self._recipients()

        self.assertNotIn(
            direct_message_group_name(self.leaver.pk),
            recipients,
            "a removed member was still sent group messages over the live channel",
        )

    def test_the_remaining_members_still_receive(self) -> None:
        """The removal must not silence the group for everyone else."""
        remove_group_member(self.group, self.creator, self.leaver)

        recipients = self._recipients()

        self.assertIn(direct_message_group_name(self.stayer.pk), recipients)

    def test_a_member_who_leaves_voluntarily_is_also_dropped(self) -> None:
        """Leaving and being removed end the same membership row."""
        remove_group_member(self.group, self.leaver, self.leaver)

        self.assertNotIn(direct_message_group_name(self.leaver.pk), self._recipients())
