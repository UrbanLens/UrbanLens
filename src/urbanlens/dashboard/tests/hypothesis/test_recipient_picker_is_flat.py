"""The recipient picker must cost the same whoever the search turns up.

``RecipientSearchView`` asked ``can_direct_message`` and ``can_view_profile``
once per candidate, for up to ``RECIPIENT_SEARCH_LIMIT * 4`` candidates, on
every keystroke past the second character. Both reach
``Profile.visibility_permits``, and at ``COMMON_PIN`` or ``ANYTHING_IN_COMMON``
that reads *both* accounts' entire ``Pin`` tables into Python - so one person
typing in the picker paid a full scan of their own pins per candidate, and the
candidates are whoever else happens to match the substring.

``Profile.visible_profile_pks`` is the batch form of the second check and has
existed since the 2026-08-17 audit; the picker never used it. The first check
had no batch form at all, so ``messageable_profile_pks`` is new and gets the
same treatment the identity batch got: held to the per-pair function rather
than to hand-written expectations, because a batch permission check that
disagrees with the single one shows a real name to someone with no standing
right to see it.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.query_scaling import queries_that_grew
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.messaging.direct_messages import can_direct_message, messageable_profile_pks

_FIRST_BATCH = 3
_SECOND_BATCH = 24
#: Two runs of one view legitimately differ by a query or so (a session write,
#: a count that only appears once there is a second page). Anything above this
#: is slope, and slope is the defect.
_TOLERANCE = 2


def _profile(**kwargs) -> Profile:
    profile = baker.make(User).profile
    if kwargs:
        Profile.objects.filter(pk=profile.pk).update(**kwargs)
        profile.refresh_from_db()
    return profile


class MessageablePksAgreementTests(TestCase):
    """``messageable_profile_pks`` must answer exactly what ``can_direct_message`` does."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.sender = _profile()

    def _assert_agrees(self, recipients: list[Profile]) -> None:
        batch = messageable_profile_pks(self.sender, recipients)
        assert_agrees(
            lambda recipient: can_direct_message(self.sender, recipient),
            lambda recipient: recipient.pk in batch,
            recipients,
            describe=lambda recipient: f"direct_message_visibility={recipient.direct_message_visibility!r}",
            label="messageable_profile_pks",
        )

    def _assert_both_outcomes(self, recipients: list[Profile]) -> None:
        """A set where everyone agrees on 'no' would agree with a stub returning nothing."""
        answers = {can_direct_message(self.sender, recipient) for recipient in recipients}
        self.assertEqual(answers, {True, False}, "this scenario proves nothing unless it contains both outcomes")

    def _befriend(self, other: Profile) -> None:
        Friendship.objects.create(
            from_profile=self.sender,
            to_profile=other,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )

    def test_every_visibility_with_no_relationship(self) -> None:
        recipients = [_profile(direct_message_visibility=value) for value in VisibilityChoice.values]

        self._assert_both_outcomes(recipients)
        self._assert_agrees(recipients)

    def test_every_visibility_with_an_accepted_friendship(self) -> None:
        recipients = [_profile(direct_message_visibility=value) for value in VisibilityChoice.values]
        for recipient in recipients:
            self._befriend(recipient)

        self._assert_both_outcomes(recipients)
        self._assert_agrees(recipients)

    def test_every_visibility_with_a_pin_in_common(self) -> None:
        location = baker.make(Location)
        baker.make(Pin, profile=self.sender, location=location)
        recipients = [_profile(direct_message_visibility=value) for value in VisibilityChoice.values]
        for recipient in recipients:
            baker.make(Pin, profile=recipient, location=location)

        self._assert_both_outcomes(recipients)
        self._assert_agrees(recipients)

    def test_a_block_vetoes_every_visibility(self) -> None:
        """Blocking must beat the settings, in both directions, including the reply exception."""
        recipients = [_profile(direct_message_visibility=value) for value in VisibilityChoice.values]
        for index, recipient in enumerate(recipients):
            DirectMessage.objects.create(sender=recipient, recipient=self.sender, body="earlier")
            # Alternate who did the blocking: it is an absolute veto either way.
            ends = (self.sender, recipient) if index % 2 else (recipient, self.sender)
            Friendship.objects.create(from_profile=ends[0], to_profile=ends[1], status=FriendshipStatus.BLOCKED)

        self.assertEqual({can_direct_message(self.sender, r) for r in recipients}, {False})
        self._assert_agrees(recipients)

    def test_a_prior_message_permits_a_reply_whatever_the_setting_says(self) -> None:
        recipients = [_profile(direct_message_visibility=value) for value in VisibilityChoice.values]
        for recipient in recipients:
            DirectMessage.objects.create(sender=recipient, recipient=self.sender, body="earlier")

        self.assertEqual({can_direct_message(self.sender, r) for r in recipients}, {True})
        self._assert_agrees(recipients)

    def test_community_disabled_on_either_side_refuses(self) -> None:
        recipients = [_profile(direct_message_visibility=VisibilityChoice.ANYONE) for _ in range(3)]
        Profile.objects.filter(pk=recipients[0].pk).update(community_enabled=False)
        recipients[0].refresh_from_db()

        self._assert_both_outcomes(recipients)
        self._assert_agrees(recipients)

        Profile.objects.filter(pk=self.sender.pk).update(community_enabled=False)
        self.sender.refresh_from_db()
        self.assertEqual({can_direct_message(self.sender, r) for r in recipients}, {False})
        self._assert_agrees(recipients)

    def test_the_sender_is_never_messageable_by_themselves(self) -> None:
        recipients = [self.sender, _profile(direct_message_visibility=VisibilityChoice.ANYONE)]

        self._assert_both_outcomes(recipients)
        self._assert_agrees(recipients)

    def test_a_mixed_list_does_not_smear_one_answer_across_its_neighbours(self) -> None:
        """The failure a batch is most likely to have: one row's verdict leaking sideways."""
        location = baker.make(Location)
        baker.make(Pin, profile=self.sender, location=location)
        recipients = []
        for value in VisibilityChoice.values:
            sharing = _profile(direct_message_visibility=value)
            baker.make(Pin, profile=sharing, location=location)
            recipients.append(sharing)
            recipients.append(_profile(direct_message_visibility=value))

        self._assert_both_outcomes(recipients)
        self._assert_agrees(recipients)


class PickerQueryScalingTests(TestCase):
    """The pickers' cost must not track how many people match the substring.

    Both pickers run the identical comprehension over the identical queryset -
    the group one says so in its own comment - so a fix to one and not the
    other leaves the cheaper door open.
    """

    #: Set by each subclass. None here so this base class contributes no cases.
    url_name: str | None = None

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.me: Profile = self.user.profile
        self.client.force_login(self.user)
        # The viewer's own pins are what the per-candidate path re-reads, once
        # for the message gate and again for the identity gate.
        self.shared_locations = [baker.make(Location) for _ in range(12)]
        for location in self.shared_locations:
            baker.make(Pin, profile=self.me, location=location)

    def _seed(self, count: int, *, offset: int) -> None:
        """Candidates whose settings force the expensive branch of both gates.

        Each shares a place with the viewer, so both gates *pass* - a candidate
        the picker refuses renders nothing, and a body that does not grow means
        the measurement never reached the code under test.
        """
        for index in range(count):
            user = baker.make(User, username=f"pickerzz{offset + index:03d}")
            Profile.objects.filter(pk=user.profile.pk).update(
                direct_message_visibility=VisibilityChoice.ANYTHING_IN_COMMON,
                profile_visibility=VisibilityChoice.ANYTHING_IN_COMMON,
            )
            baker.make(Pin, profile=user.profile, location=self.shared_locations[index % len(self.shared_locations)])

    def _measure(self) -> tuple[int, int, list[dict]]:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse(self.url_name), {"q": "pickerzz"})
        self.assertEqual(response.status_code, 200)
        return len(captured), len(response.content), list(captured)

    def test_query_count_does_not_grow_with_the_number_of_candidates(self) -> None:
        if self.url_name is None:
            self.skipTest("base class - the subclasses name the two pickers")
        self._seed(_FIRST_BATCH, offset=0)
        first_queries, first_bytes, first_sql = self._measure()

        self._seed(_SECOND_BATCH - _FIRST_BATCH, offset=_FIRST_BATCH)
        second_queries, second_bytes, second_sql = self._measure()

        self.assertGreater(
            second_bytes,
            first_bytes,
            "the seed does not exercise this endpoint - the picker rendered the same list twice",
        )
        growth = second_queries - first_queries
        self.assertLessEqual(
            growth,
            _TOLERANCE,
            f"{first_queries} queries for {_FIRST_BATCH} candidates, {second_queries} for {_SECOND_BATCH}."
            f"\nWhat multiplied:\n"
            + "\n".join(f"  {b} -> {a}  {sql}" for b, a, sql in queries_that_grew(first_sql, second_sql)),
        )


class RecipientPickerQueryScalingTests(PickerQueryScalingTests):
    """The new-message recipient picker."""

    url_name = "messages.recipients"


class GroupMemberPickerQueryScalingTests(PickerQueryScalingTests):
    """The group-chat member picker, which is the same code behind another URL."""

    url_name = "messages.group.member_search"
