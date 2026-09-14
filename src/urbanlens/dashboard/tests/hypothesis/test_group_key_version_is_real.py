"""A group message's `key_version` has to name a key this group actually has."""

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

        Rotating leaves earlier versions real."""
        GroupKey.objects.create(group=self.group, version=2)

        message = self._send(1)

        self.assertEqual(message.key_version, 1)
