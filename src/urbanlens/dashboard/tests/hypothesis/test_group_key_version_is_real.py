"""A group message's `key_version` has to name a key this group has, held only by its current members.

Every envelope row records someone who can open that version. Sending under a version one of them no longer
belongs to would make the message readable to a person the group removed, so the server refuses it."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.e2ee import GroupKey, GroupKeyEnvelope
from urbanlens.dashboard.models.group_chats.model import GroupMessage
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.group_chats import (
    StaleKeyVersionError,
    UnknownKeyVersionError,
    add_group_members,
    create_group_chat,
    create_group_message,
    remove_group_member,
)


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


def _key(group, version: int, holders: list[Profile]) -> GroupKey:
    key = GroupKey.objects.create(group=group, version=version)
    GroupKeyEnvelope.objects.bulk_create(
        [GroupKeyEnvelope(key=key, profile=holder, wrapped_key="c2VhbGVk") for holder in holders]
    )
    return key


class GroupKeyVersionIsRealTests(TestCase):
    """The server checks that the named key version exists for this group."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.group = create_group_chat(self.creator, "Crew", [self.member])
        _key(self.group, 1, [self.creator, self.member])

    def _send(self, key_version: int, sender: Profile | None = None) -> GroupMessage:
        return create_group_message(
            sender or self.creator,
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


class AVersionHeldByAFormerMemberIsRefusedTests(TestCase):
    """Rotation excludes a removed member from later versions; sending under an earlier one would undo that."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.ejected = _profile()
        self.group = create_group_chat(self.creator, "Crew", [self.member, self.ejected])
        _key(self.group, 1, [self.creator, self.member, self.ejected])

    def _send(self, key_version: int) -> GroupMessage:
        return create_group_message(
            self.creator, self.group, "", ciphertext="c2VhbGVk", nonce="bm9uY2U=", key_version=key_version
        )

    def test_a_removed_members_version_is_refused(self) -> None:
        """The exploit: a remaining member picks the pre-removal version so the ejected member can read it."""
        remove_group_member(self.group, self.creator, self.ejected)
        _key(self.group, 2, [self.creator, self.member])

        with self.assertRaises(StaleKeyVersionError):
            self._send(1)

    def test_it_is_refused_before_anyone_has_rotated(self) -> None:
        """Rejecting only non-latest versions would not hold: until a rotation, the latest is the leaky one."""
        remove_group_member(self.group, self.creator, self.ejected)

        with self.assertRaises(StaleKeyVersionError):
            self._send(1)

    def test_leaving_voluntarily_counts_the_same(self) -> None:
        remove_group_member(self.group, self.ejected, self.ejected)

        with self.assertRaises(StaleKeyVersionError):
            self._send(1)

    def test_a_deleted_account_still_counts_as_a_former_holder(self) -> None:
        """Deletion cascades the membership away, so the envelope row is the only record left that they held it."""
        self.ejected.user.delete()

        with self.assertRaises(StaleKeyVersionError):
            self._send(1)
        self.assertEqual(
            GroupKeyEnvelope.objects.filter(key__group=self.group, key__version=1, profile__isnull=True).count(), 1
        )

    def test_the_version_rotated_in_after_the_removal_is_accepted(self) -> None:
        remove_group_member(self.group, self.creator, self.ejected)
        _key(self.group, 2, [self.creator, self.member])

        self.assertEqual(self._send(2).key_version, 2)

    def test_re_adding_the_member_makes_their_version_safe_again(self) -> None:
        """They are a member now, so a message they can open is one they are entitled to."""
        remove_group_member(self.group, self.creator, self.ejected)
        add_group_members(self.group, self.creator, [self.ejected])

        self.assertEqual(self._send(1).key_version, 1)

    def test_an_older_version_is_accepted_while_all_its_holders_remain(self) -> None:
        """A version predating an *addition* exposes nothing: the newcomer simply cannot read it."""
        newcomer = _profile()
        add_group_members(self.group, self.creator, [newcomer])
        _key(self.group, 2, [self.creator, self.member, self.ejected, newcomer])

        self.assertEqual(self._send(1).key_version, 1)

    def test_nothing_is_stored_when_refused(self) -> None:
        remove_group_member(self.group, self.creator, self.ejected)

        with self.assertRaises(StaleKeyVersionError):
            self._send(1)
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())
