"""A group's member list must not cost a privacy check per member.

``resolve_visible_identities`` exists precisely to render several people
together, and it was resolving them one at a time: a loop over
``resolve_visible_identity``, each call reaching ``can_view_profile`` and
re-deriving the *viewer's* own friend, trip and pinned-location sets. A group
member list, a group message list's distinct senders, and (via
``mask_profile_references``) a trip's participants all paid that per person.

The members dialog is the endpoint measured here because its row count is
exactly the member count. The fix is inside ``resolve_visible_identities``, so
the other callers get it too.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.messaging.group_chats import create_group_chat

_FIRST_BATCH = 2
_SECOND_BATCH = 10


def _profile() -> Profile:
    """A profile that accepts messages - group creation refuses members who don't."""
    from urbanlens.dashboard.models.profile.meta import VisibilityChoice

    profile = baker.make(User).profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    return profile


class GroupMemberListQueryScalingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer_user = baker.make(User)
        self.viewer: Profile = self.viewer_user.profile
        self.client.force_login(self.viewer_user)
        self.group = create_group_chat(self.viewer, "Weekend crew", [_profile()])

    def _add_members(self, count: int) -> None:
        from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

        for _ in range(count):
            GroupChatMembership.objects.create(group=self.group, profile=_profile())

    def _count(self, url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
        return len(ctx.captured_queries)

    def test_member_dialog_does_not_scale_with_member_count(self) -> None:
        url = reverse("messages.group.members", kwargs={"group_uuid": self.group.uuid})

        self._add_members(_FIRST_BATCH)
        small = self._count(url)
        self._add_members(_SECOND_BATCH)
        large = self._count(url)

        self.assertLessEqual(
            large,
            small + 2,
            f"the member dialog ran {small} queries for {_FIRST_BATCH} extra members and {large} for "
            f"{_FIRST_BATCH + _SECOND_BATCH} - it is running a privacy check per member.",
        )

    def test_members_are_still_named_or_masked_correctly(self) -> None:
        """The complement: batching must not change who is identifiable.

        A member with no relationship to the viewer and a friends-only profile
        must still come back masked, and a public one must still be named.
        """
        from urbanlens.dashboard.models.profile.meta import VisibilityChoice
        from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identities

        public = _profile()
        Profile.objects.filter(pk=public.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        private = _profile()
        Profile.objects.filter(pk=private.pk).update(profile_visibility=VisibilityChoice.FRIENDS)
        public.refresh_from_db()
        private.refresh_from_db()

        resolved = resolve_visible_identities(self.viewer, [public, private])

        self.assertFalse(resolved[public.pk]["is_masked"], "a public profile was masked")
        self.assertEqual(resolved[public.pk]["display_name"], public.username)
        self.assertTrue(resolved[private.pk]["is_masked"], "a friends-only stranger was named")
        self.assertEqual(resolved[private.pk]["display_name"], "Member 1")
