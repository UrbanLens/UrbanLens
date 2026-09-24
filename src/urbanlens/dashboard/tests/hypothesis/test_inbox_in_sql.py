"""The inbox is ordered and cut in SQL, and group membership has a per-profile ceiling."""

from __future__ import annotations

import datetime
import re

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.direct_messages import SIDEBAR_CONVERSATION_STEP
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership, GroupMessage
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.messaging.group_chats import (
    MemberInTooManyGroupsError,
    TooManyGroupMembersError,
    add_group_members,
    create_group_chat,
    group_conversations_for,
    group_inbox_rows,
)
from urbanlens.dashboard.services.messaging.inbox import InboxFeed


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    return profile


def _shapes(ctx: CaptureQueriesContext) -> list[str]:
    return [re.sub(r"\d+", "N", query["sql"]) for query in ctx.captured_queries]


class _InboxCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")
        self.me = _profile()
        self.base = timezone.now() - datetime.timedelta(days=1)
        self.tick = 0

    def _at(self) -> datetime.datetime:
        self.tick += 1
        return self.base + datetime.timedelta(minutes=self.tick)

    def dm_from(self, partner: Profile, *, read: bool = False) -> DirectMessage:
        message = baker.make(
            DirectMessage, sender=partner, recipient=self.me, body="hi", read_at=timezone.now() if read else None
        )
        DirectMessage.objects.filter(pk=message.pk).update(created=self._at())
        return message

    def group_with_message(self) -> GroupChat:
        group = GroupChat.objects.create(name=f"G{self.tick}", creator=self.me)
        joined = self._at()
        other = _profile()
        for profile in (self.me, other):
            membership = GroupChatMembership.objects.create(group=group, profile=profile)
            GroupChatMembership.objects.filter(pk=membership.pk).update(created=joined)
        message = GroupMessage.objects.create(group=group, sender=other, body="yo")
        GroupMessage.objects.filter(pk=message.pk).update(created=self._at())
        return group


class InboxOrderingTests(_InboxCase):
    def test_dm_and_group_rows_interleave_newest_first_and_slice_in_sql(self) -> None:
        first_dm = self.dm_from(_profile())
        group = self.group_with_message()
        last_dm = self.dm_from(_profile())

        feed = InboxFeed(self.me)
        everything = feed[:]
        top_two = feed[:2]

        self.assertEqual(len(feed), 3)
        self.assertEqual([c["kind"] for c in everything], ["dm", "group", "dm"])
        self.assertEqual(everything[0]["last_message"].pk, last_dm.pk)
        self.assertEqual(everything[1]["group"].pk, group.pk)
        self.assertEqual(everything[2]["last_message"].pk, first_dm.pk)
        self.assertEqual([c["kind"] for c in top_two], ["dm", "group"])

    def test_only_unread_drops_read_conversations(self) -> None:
        self.dm_from(_profile(), read=True)
        unread = self.dm_from(_profile())

        rows = InboxFeed(self.me, only_unread=True)[:]

        self.assertEqual([row["last_message"].pk for row in rows], [unread.pk])

    def test_a_page_costs_the_same_however_many_conversations_exist(self) -> None:
        for _ in range(2):
            self.dm_from(_profile())
            self.group_with_message()
        with CaptureQueriesContext(connection) as small:
            InboxFeed(self.me)[:3]
        for _ in range(10):
            self.dm_from(_profile())
            self.group_with_message()
        with CaptureQueriesContext(connection) as large:
            page = InboxFeed(self.me)[:3]

        self.assertEqual(len(page), 3)
        self.assertEqual(len(large.captured_queries), len(small.captured_queries))

    def test_group_summary_is_one_statement_whatever_the_group_count(self) -> None:
        self.group_with_message()
        with CaptureQueriesContext(connection) as small:
            list(group_inbox_rows(self.me))
        for _ in range(8):
            self.group_with_message()
        with CaptureQueriesContext(connection) as large:
            rows = list(group_inbox_rows(self.me))

        self.assertEqual(len(rows), 9)
        self.assertEqual(_shapes(large), _shapes(small))
        self.assertEqual(len(large.captured_queries), 1)

    def test_unread_ignores_own_messages_and_what_was_read(self) -> None:
        group = self.group_with_message()
        GroupMessage.objects.create(group=group, sender=self.me, body="mine")
        [row] = group_conversations_for(self.me)
        self.assertEqual(row["unread_count"], 1)

        GroupChatMembership.objects.filter(group=group, profile=self.me).update(last_read_at=timezone.now())
        [row] = group_conversations_for(self.me)
        self.assertEqual(row["unread_count"], 0)


class SidebarLimitTests(_InboxCase):
    def test_the_sidebar_lists_one_step_and_offers_more(self) -> None:
        for _ in range(SIDEBAR_CONVERSATION_STEP + 2):
            self.dm_from(_profile())
        self.client.force_login(self.me.user)

        first = self.client.get(reverse("messages.list"))
        more = self.client.get(reverse("messages.list"), {"limit": first.context["more_limit"]})

        self.assertEqual(len(first.context["conversations"]), SIDEBAR_CONVERSATION_STEP)
        self.assertEqual(first.context["more_limit"], SIDEBAR_CONVERSATION_STEP * 2)
        self.assertEqual(len(more.context["conversations"]), SIDEBAR_CONVERSATION_STEP + 2)
        self.assertIsNone(more.context["more_limit"])


class GroupMembershipCeilingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")
        self.creator = _profile()
        self.target = _profile()
        self.settings = SiteSettings.get_current()

    def _set(self, **values) -> None:
        SiteSettings.objects.filter(pk=self.settings.pk).update(**values)

    def test_a_profile_at_the_ceiling_cannot_be_added_to_another_group(self) -> None:
        self._set(max_group_chats_per_user=2)
        create_group_chat(_profile(), "One", [self.target])
        create_group_chat(_profile(), "Two", [self.target])
        room_left = create_group_chat(self.creator, "Three", [_profile()])

        with self.assertRaises(MemberInTooManyGroupsError):
            create_group_chat(_profile(), "Four", [self.target])
        with self.assertRaises(MemberInTooManyGroupsError):
            add_group_members(room_left, self.creator, [self.target])

        self.assertEqual(GroupChatMembership.objects.active().filter(profile=self.target).count(), 2)

    def test_zero_means_no_ceiling(self) -> None:
        self._set(max_group_chats_per_user=0)
        for index in range(3):
            create_group_chat(self.creator, f"G{index}", [self.target])

        self.assertEqual(GroupChatMembership.objects.active().filter(profile=self.target).count(), 3)

    def test_the_admin_group_size_setting_is_the_one_enforced(self) -> None:
        self._set(max_group_chat_members=2)

        with self.assertRaises(TooManyGroupMembersError) as caught:
            create_group_chat(self.creator, "Big", [self.target, _profile()])

        self.assertEqual(caught.exception.limit, 2)
