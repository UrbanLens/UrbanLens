"""A group message's `key_version` has to name a key this group actually has.

`create_group_message` validated `key_version < 1` and nothing else, so the
value a client sent was stored verbatim on all four send paths - the WebSocket
consumer, the external API, the web controller, and share-a-pin-in-a-group.
Any positive integer was accepted: a version this group has never had, or one
belonging to a *different* group.

`models/e2ee/group_key.py` states the design claim it breaks: "Versioning is what
enforces membership boundaries **cryptographically**". A version field nothing
checks is a claim about a number the server never looks at.

This is the half of P26/P46 that can be fixed without deciding a product
question. Rejecting a *stale but real* version is the other half and is
deliberately not done here: rotation needs every member enrolled and returns 409
when one is not, so refusing stale sends would let one un-enrolled member stop
the whole group from sending - trading a confidentiality gap for an availability
one. The last test below pins that boundary so the distinction stays deliberate.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.e2ee import GroupKey
from urbanlens.dashboard.models.group_chats.model import GroupMessage
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.group_chats import (
    UnknownKeyVersionError,
    create_group_chat,
    create_group_message,
)


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


class GroupKeyVersionIsRealTests(TestCase):
    """The server checks that the named key version exists for this group."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.group = create_group_chat(self.creator, "Crew", [self.member])
        GroupKey.objects.create(group=self.group, version=1)

    def _send(self, key_version: int) -> GroupMessage:
        return create_group_message(
            self.creator,
            self.group,
            "",
            ciphertext="c2VhbGVk",
            nonce="bm9uY2U=",
            key_version=key_version,
        )

    def test_a_version_this_group_has_never_had_is_rejected(self) -> None:
        with self.assertRaises(UnknownKeyVersionError):
            self._send(999)

    def test_a_version_belonging_to_another_group_is_rejected(self) -> None:
        """The check has to be scoped to the group, not just to existence."""
        other = create_group_chat(_profile(), "Elsewhere", [_profile()])
        GroupKey.objects.create(group=other, version=7)

        with self.assertRaises(UnknownKeyVersionError):
            self._send(7)

    def test_the_current_version_is_accepted(self) -> None:
        """Guard against 'fixing' this by refusing every encrypted send."""
        message = self._send(1)

        self.assertEqual(message.key_version, 1)

    def test_a_stale_but_real_version_is_still_accepted(self) -> None:
        """Pinned, not endorsed - see the module docstring.

        Rotating leaves earlier versions real. Refusing them is the product
        decision this change deliberately does not make, so the behaviour is
        recorded here rather than left to be discovered.
        """
        GroupKey.objects.create(group=self.group, version=2)

        message = self._send(1)

        self.assertEqual(message.key_version, 1)
